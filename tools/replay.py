"""Local, private replay of the production moderator over a Desktop HTML export.

This is a single-process offline adapter. Scoped module clocks let the actual
Debouncer, analyze(), storage counters and send policy run in message time.
Hypothetical replies are recorded, not inserted into the historical transcript.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import subprocess
import sys
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import anthropic

from dobrosusid import bot as bot_module, moderator as moderator_module, scheduler, storage
from dobrosusid.bot import DobrosusidBot
from dobrosusid.config import Settings
from dobrosusid.moderator import Decision, Moderator
from dobrosusid.safety import unsafe_reason
from dobrosusid.storage import Storage
from tools.telegram_export import ExportMessage, read_export

CHAT = 1
REPO = Path(__file__).resolve().parents[1]


@contextmanager
def private_logging():
    """SDK debug logs can contain request bodies; keep them private as well."""
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        yield
    finally:
        logging.disable(previous)


def prepare_output(path: Path) -> Path:
    """Reject lexical and symlink routes into any worktree before writing data.

    A fresh directory also prevents accidentally overwriting a previous run or
    following pre-planted output-file symlinks. Reports are created exclusively.
    """
    lexical = Path(os.path.abspath(path.expanduser()))
    path = lexical.resolve()
    roots = [REPO]
    result = subprocess.run(
        ["git", "-C", str(REPO), "worktree", "list", "--porcelain", "-z"],
        capture_output=True, check=True,
    )
    roots.extend(Path(os.fsdecode(part.removeprefix(b"worktree "))).resolve()
                 for part in result.stdout.split(b"\0") if part.startswith(b"worktree "))
    for candidate in (path, lexical):
        # Inspect lexical parents too: a symlink inside another repository may
        # point outside it, but that spelling must still be refused.
        for ancestor in (candidate, *candidate.parents):
            if not ancestor.is_dir():
                continue
            other = subprocess.run(
                ["git", "-C", str(ancestor), "rev-parse", "--show-toplevel"], capture_output=True,
            )
            if other.returncode == 0:
                roots.append(Path(os.fsdecode(other.stdout).strip()).resolve())
                break
    if any(candidate.is_relative_to(root) for root in roots for candidate in (path, lexical)):
        raise ValueError("Output must be outside every git worktree")
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    path.chmod(0o700)
    return path


def load_env_file(path: Path) -> None:
    """Small dotenv subset: KEY=value, optional export/quotes; never execute it.

    Existing environment values win. Only production settings and API credentials
    are read later; diagnostics never include a line, value, or private path.
    """
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not key.isidentifier():
            raise ValueError("Invalid env-file entry")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Invalid env-file quoting")
            value = value[1:-1]
        os.environ.setdefault(key, value)


def replay_settings() -> Settings:
    # Reuse every production default/env conversion without requiring Telegram
    # credentials or ever constructing a Telegram client or opening its database.
    with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "DB_PATH": ":memory:", "INTRO_REMINDERS": "false"}):
        settings = Settings.from_env()
    if (settings.context_messages < 1 or settings.max_llm_calls_per_day < 0
            or settings.max_interventions_per_day < 0
            or any(not math.isfinite(n) or n < 0 for n in (
                settings.debounce_seconds, settings.max_wait_seconds, settings.cooldown_minutes))):
        raise ValueError("Invalid replay settings")
    ZoneInfo(settings.timezone)
    return settings


class ReplayClock:
    def __init__(self):
        self.now = 0.0
        self.waiters = []

    def time(self):
        return self.now

    monotonic = time

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.now + delay, future, asyncio.current_task()))
        await future

    async def advance(self, target):
        while True:
            self.waiters = [w for w in self.waiters if not w[1].done()]
            due = sorted((w for w in self.waiters if w[0] <= target), key=lambda w: w[0])
            if not due:
                break
            deadline, future, task = due[0]
            self.now = deadline
            future.set_result(None)
            # Wait for the actual analysis/API response, not a fixed wall sleep.
            await task
        if math.isfinite(target):
            self.now = target

    @contextmanager
    def installed(self):
        clock = self

        class ReplayDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock.now, tz)

        with ExitStack() as stack:
            # Replace module bindings, never the process-wide time/asyncio APIs.
            for module in (bot_module, scheduler, storage):
                stack.enter_context(patch.object(module, "time", self))
            stack.enter_context(patch.object(bot_module, "datetime", ReplayDateTime))
            stack.enter_context(patch.object(scheduler, "asyncio", SimpleNamespace(
                sleep=self.sleep, create_task=asyncio.create_task, Lock=asyncio.Lock,
            )))
            # Production diagnostics can contain model output or API exceptions.
            for module in (bot_module, moderator_module, scheduler):
                stack.enter_context(patch.object(module.log, "disabled", True))
            yield


class StubMessages:
    """Deterministic fake transport; Moderator.decide still builds the real prompt.

    Synthetic trigger words are deliberately simple; this is a wiring check,
    never an alternative moderation model or a quality assessment.
    """
    async def parse(self, **kwargs):
        prompt = kwargs["messages"][0]["content"]
        new = prompt.split("НОВІ:\n", 1)[1].split("\n</chat>", 1)[0]
        rows = [json.loads(line) for line in new.splitlines() if line]
        conflict = any("сварка" in row["text"].lower() for row in rows)
        return SimpleNamespace(
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            parsed_output=Decision(
                tension="conflict" if conflict else "calm", emergency=False,
                action="reply" if conflict else "none", reply_to_message_id=None,
                text="Повернімося до спокійної розмови." if conflict else "",
                introductions=[],
            ),
        )


class MeteredMessages:
    def __init__(self, transport):
        self.transport = transport
        self.calls = 0
        self.input_tokens = self.output_tokens = 0
        self.cache_read_tokens = self.cache_creation_tokens = 0
        self.stop_reason = None
        self.error = False

    async def parse(self, **kwargs):
        self.calls += 1
        self.stop_reason, self.error = None, False
        try:
            response = await self.transport.parse(**kwargs)
        except Exception:
            self.error = True
            raise
        self.stop_reason = response.stop_reason
        usage = response.usage
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_creation_tokens += getattr(usage, "cache_creation_input_tokens", 0) or 0
        return response


class RecordedModerator(Moderator):
    decision = None

    async def decide(self, *args, **kwargs):
        self.decision = await super().decide(*args, **kwargs)
        return self.decision


class ReplayBot(DobrosusidBot):
    def __init__(self, settings, meter, clock, output, max_calls):
        # Deliberately skip the live constructor: no Telegram client, polling,
        # reminders, background tasks, credentials or production SQLite files.
        self.settings, self.meter, self.clock = settings, meter, clock
        self.tz = ZoneInfo(settings.timezone)
        self.store = Storage(":memory:")
        self.store.upsert_chat(CHAT, None, None)
        self.app = SimpleNamespace(bot=SimpleNamespace(id=0, username=""))
        self.moderator = RecordedModerator(SimpleNamespace(messages=meter), settings.model, settings.persona_name, settings.author)
        self.debouncer = scheduler.Debouncer(self._analyze_batch, settings.debounce_seconds, settings.max_wait_seconds)
        self.output, self.max_calls = output, max_calls
        self.batches = self.would_intervene = 0
        self.histogram = Counter()
        self.skipped = Counter()
        self.metadata = {}
        self.failure = None

    def _chat_allowed(self, chat_id):
        return chat_id == CHAT

    async def _send(self, chat_id, text, *, reply_to=None, **kwargs):
        self.sent = True
        self.reply_to = reply_to

    async def _react(self, *args):
        return True

    async def _analyze_batch(self, chat_id):
        try:
            await self.analyze(chat_id)
        except Exception as exc:
            # Debouncer deliberately logs/catches callback errors in production.
            # An offline write failure must instead abort with a nonzero exit.
            self.failure = exc

    async def _maybe_intervene(self, chat_id, decision, addressed_ids, context_messages):
        allowed = await super()._maybe_intervene(chat_id, decision, addressed_ids, context_messages)
        if not allowed:
            # Explanations only: the inherited production method owns the gate.
            if decision.action != "reply":
                self.reason = "no_reply"
            elif not decision.text.strip():
                self.reason = "empty_text"
            elif not addressed_ids and decision.tension not in {"conflict", "hostile"}:
                self.reason = "tension_or_emergency_cooldown"
            elif not addressed_ids and self.store.count_events("intervention", self._day_start(), chat_id) >= self.settings.max_interventions_per_day:
                self.reason = "daily_intervention_limit"
            else:
                self.reason = "cooldown"
            unsafe = unsafe_reason(decision.text.strip(), self.settings.allowed_link_domains)
            # Safety is checked after policy in production too.
            if moderator_module.should_intervene(
                decision, addressed=bool(addressed_ids),
                last_intervention_ts=self.store.last_event(chat_id, "intervention"),
                interventions_today=self.store.count_events("intervention", self._day_start(), chat_id),
                now=self.clock.now, cooldown_minutes=self.settings.cooldown_minutes,
                max_per_day=self.settings.max_interventions_per_day,
                last_emergency_ts=self.store.last_event(chat_id, "emergency"),
            ) and unsafe:
                # unsafe_reason may include a domain from the model's answer;
                # keep public console summaries free of that private value.
                self.reason = "unsafe_output"
        return allowed

    async def analyze(self, chat_id):
        new = self.store.messages_after(chat_id, self.store.get_chat(chat_id).last_analyzed_id)
        if not new:
            return
        self.sent, self.reply_to, self.reason = False, None, None
        self.moderator.decision = None
        calls_before = self.meter.calls
        if calls_before >= self.max_calls:
            self.store.set_last_analyzed(chat_id, new[-1].message_id)
            self.reason = "max_calls"
        else:
            try:
                await super().analyze(chat_id)
            except Exception as exc:
                # Transport/schema failures are recorded privately and consume
                # their attempted call. Unexpected adapter failures abort later.
                if not self.meter.error:
                    self.failure = exc
                self.reason = "model_error"
        decision = self.moderator.decision
        if self.reason is None and not self.sent:
            self.reason = ("daily_call_limit" if self.meter.calls == calls_before
                           else "model_error" if self.meter.error else "no_decision")
        if decision:
            self.histogram[decision.tension] += 1
        self.batches += 1
        self.would_intervene += self.sent
        if not self.sent:
            self.skipped[self.reason] += 1
        row = {
            "batch": self.batches,
            "from": self.metadata[new[0].message_id].date.isoformat(),
            "to": self.metadata[new[-1].message_id].date.isoformat(),
            "analyzed_at": datetime.fromtimestamp(self.clock.now, self.tz).isoformat(),
            "message_ids": [m.message_id for m in new],
            "forwarded_ids": [m.message_id for m in new if self.metadata[m.message_id].forwarded],
            "calls": self.meter.calls - calls_before,
            "decision": decision.model_dump() if decision else None,
            "stop_reason": self.meter.stop_reason if self.meter.calls > calls_before else None,
            "would_send": self.sent, "reason": "sent" if self.sent else self.reason,
            "reply_to": self.reply_to,
        }
        self.output.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.output.flush()
        for message in new:
            self.metadata.pop(message.message_id, None)
        if self.reason == "max_calls":
            self.store.prune_messages()

    async def ingest(self, message: ExportMessage):
        # Reuse production forwarding marker, addressing/rate limits and poke.
        self.metadata[message.message_id] = message
        incoming = SimpleNamespace(
            chat_id=CHAT, message_id=message.message_id, text=message.text, caption=None,
            sender_chat=None, from_user=SimpleNamespace(id=message.user_id, full_name=message.author, is_bot=False),
            forward_origin=True if message.forwarded else None,
            reply_to_message=SimpleNamespace(message_id=message.reply_to, from_user=None) if message.reply_to else None,
            date=message.date, parse_entities=lambda: {},
        )
        await self.on_group_message(SimpleNamespace(effective_message=incoming), None)
        # Start the newly created timer so it registers its virtual deadline.
        await asyncio.sleep(0)


async def replay(messages, output, settings, meter, max_calls, start=None, end=None):
    clock = ReplayClock()
    with clock.installed():
        bot = ReplayBot(settings, meter, clock, output, max_calls)
        services = 0
        try:
            for message in messages:
                if message.service:
                    services += 1
                    continue
                if not message.text or (start and message.date < start) or (end and message.date >= end):
                    continue
                await clock.advance(message.date.timestamp())
                if bot.failure:
                    raise bot.failure
                await bot.ingest(message)
            await clock.advance(math.inf)
            if bot.failure:
                raise bot.failure
            return {
                "batches": bot.batches, "calls": meter.calls,
                "tension_histogram": {key: bot.histogram[key] for key in ("calm", "tense", "conflict", "hostile")},
                "would_intervene": bot.would_intervene, "not_sent": dict(bot.skipped),
                "service_messages_skipped": services,
                "input_tokens": meter.input_tokens, "output_tokens": meter.output_tokens,
                "cache_read_tokens": meter.cache_read_tokens, "cache_creation_tokens": meter.cache_creation_tokens,
            }
        finally:
            owned = list(bot.debouncer._tasks)
            for task in owned:
                task.cancel()
            await asyncio.gather(*owned, return_exceptions=True)
            bot.store.db.close()


def nonnegative_int(raw):
    value = int(raw)
    if value < 0:
        raise ValueError("Must be nonnegative")
    return value


def nonnegative_float(raw):
    value = float(raw)
    if not math.isfinite(value) or value < 0:
        raise ValueError("Must be finite and nonnegative")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="New directory outside all git worktrees")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--from", dest="start", type=date.fromisoformat)
    parser.add_argument("--to", dest="end", type=date.fromisoformat)
    parser.add_argument("--max-calls", type=nonnegative_int, default=50)
    parser.add_argument("--stub", action="store_true")
    parser.add_argument("--input-usd-per-million", type=nonnegative_float, default=0.10)
    parser.add_argument("--output-usd-per-million", type=nonnegative_float, default=0.50)
    args = parser.parse_args(argv)
    client = None
    try:
        if args.start and args.end and args.start > args.end:
            raise ValueError("Invalid date range")
        if args.env_file:
            load_env_file(args.env_file)
        settings = replay_settings()
        if not args.stub and not os.environ.get("ANTHROPIC_API_KEY"):
            raise ValueError("ANTHROPIC_API_KEY is required in real mode")
        out = prepare_output(args.out)
        tz = ZoneInfo(settings.timezone)
        start = datetime.combine(args.start, time.min, tz) if args.start else None
        end = datetime.combine(args.end + timedelta(days=1), time.min, tz) if args.end else None

        async def run():
            nonlocal client
            if args.stub:
                transport = StubMessages()
            else:
                # One attempt per call: SDK retries would silently exceed spend.
                client = anthropic.AsyncAnthropic(timeout=60.0, max_retries=0)
                transport = client.messages
            meter = MeteredMessages(transport)
            try:
                with (out / "batches.jsonl").open("x", encoding="utf-8") as report:
                    result = await replay(read_export(args.export), report, settings, meter, args.max_calls, start, end)
                result.update(
                    stub=args.stub, model=settings.model, max_calls=args.max_calls,
                    input_usd_per_million=args.input_usd_per_million,
                    output_usd_per_million=args.output_usd_per_million,
                    estimated_cost_usd=round((meter.input_tokens * args.input_usd_per_million
                                              + meter.output_tokens * args.output_usd_per_million) / 1_000_000, 8),
                    cost_basis="Configured uncached rates; defaults from README, not live pricing. Stub tokens/cost are zero.",
                    context_basis="Historical messages only; hypothetical replies are not inserted. Display names share synthetic user ids; bot identities/mentions cannot be recovered from HTML.",
                )
                with (out / "summary.json").open("x", encoding="utf-8") as report:
                    json.dump(result, report, ensure_ascii=False, indent=2)
                    report.write("\n")
                return result
            finally:
                if client:
                    await client.close()

        with private_logging():
            result = asyncio.run(run())
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Never print exception strings: they can carry private text or secrets.
        safe = str(exc) if isinstance(exc, ValueError) and str(exc) in {
            "Output must be outside every git worktree", "ANTHROPIC_API_KEY is required in real mode",
            "Invalid date range", "No Telegram HTML pages found", "Invalid replay settings",
        } else type(exc).__name__
        print("Replay failed: " + safe, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
