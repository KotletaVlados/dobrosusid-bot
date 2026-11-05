"""Only invented exports and transports: no real chat, key, or network."""

import asyncio
import io
import json
import logging
import os
import socket
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import anthropic
import pytest

from dobrosusid.config import Settings
from dobrosusid.moderator import Decision
from tools import replay as tool
from tools.telegram_export import ExportMessage, read_export

FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_export"
BASE = datetime.fromisoformat("2026-11-03T18:00:00+02:00")
SETTINGS = Settings("", frozenset(), frozenset(), intro_reminders=False)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("Network forbidden in replay tests")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(os, "environ", os.environ.copy())
    for key in list(os.environ):
        if key in {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"}:
            monkeypatch.delenv(key)


def invented(offsets, text="вигадана сварка"):
    return [ExportMessage(10 + i, BASE + timedelta(seconds=seconds), "Синтетик А", text, None, False, False)
            for i, seconds in enumerate(offsets)]


def decision(**kwargs):
    values = dict(tension="conflict", emergency=False, action="reply", reply_to_message_id=None,
                  text="Повернімося до теми.", introductions=[])
    return Decision(**(values | kwargs))


class Transport(tool.StubMessages):
    def __init__(self, decisions=None, stop="end_turn", error=None, tokens=(0, 0)):
        self.prompts = []
        self.decisions = iter(decisions) if decisions is not None else None
        self.stop, self.error = stop, error
        self.tokens = tokens

    async def parse(self, **kwargs):
        self.prompts.append(kwargs)
        if self.error:
            raise self.error
        result = await super().parse(**kwargs)
        if self.decisions is not None:
            result.parsed_output = next(self.decisions)
        result.stop_reason = self.stop
        result.usage = SimpleNamespace(input_tokens=self.tokens[0], output_tokens=self.tokens[1])
        return result


def run(messages, *, settings=SETTINGS, max_calls=50, transport=None, start=None, end=None):
    report = io.StringIO()
    transport = transport or Transport()
    meter = tool.MeteredMessages(transport)
    summary = asyncio.run(tool.replay(iter(messages), report, settings, meter, max_calls, start, end))
    return [json.loads(line) for line in report.getvalue().splitlines()], summary, transport


def test_parser_desktop_shape():
    header, first, continued, joined, forwarded, reply = list(read_export(FIXTURE))
    assert header.service and header.date is None and header.message_id == -1
    assert first.message_id == 10 and first.author == "Синтетик А"
    assert first.date.isoformat() == "2026-11-03T18:06:00+02:00"
    assert first.text == "Вигадане привітання.\nДругий рядок & крапка."
    assert continued.author == first.author and continued.reply_to == 10
    assert joined.service and joined.message_id == 12 and joined.date is not None
    assert forwarded.forwarded and forwarded.author == first.author
    assert forwarded.date.isoformat() == "2026-11-03T18:06:20+02:00"
    assert forwarded.text == "Вигадана пересилка.\nБез реальних даних."
    assert reply.reply_to == 13 and reply.author == "Синтетик Б"
    assert first.user_id == forwarded.user_id != reply.user_id
    assert first.user_id == list(read_export(FIXTURE))[1].user_id


def html(message_id=1, stamp="03.11.2026 18:00:00 UTC+02:00", author="Синтетик А", cls=""):
    return (f'<div class="message {cls}" id="message{message_id}"><div class="body">'
            f'<div class="date" title="{stamp}"></div><div class="from_name">{author}</div>'
            '<div class="text">Вигаданий текст</div></div></div>')


@pytest.mark.parametrize("content", [html(stamp="invalid"), html(stamp=""), html(author=""),
                                     html(author="", cls="joined"), html() + html(),
                                     html(2) + html(1)])
def test_parser_rejects_malformed_or_reordered_messages(tmp_path, content):
    (tmp_path / "messages.html").write_text(content)
    with pytest.raises(ValueError):
        list(read_export(tmp_path))


def test_parser_pages_sorted_numerically_and_missing_export(tmp_path):
    with pytest.raises(ValueError, match="No Telegram HTML"):
        list(read_export(tmp_path))
    for page in (10, 2, 1):
        (tmp_path / ("messages.html" if page == 1 else f"messages{page}.html")).write_text(html(page))
    assert [m.message_id for m in read_export(tmp_path)] == [1, 2, 10]


def test_batch_debounce_and_service_exclusion():
    rows, summary, transport = run(read_export(FIXTURE))
    assert [r["message_ids"] for r in rows] == [[10, 11, 13], [14]]
    assert [r["analyzed_at"] for r in rows] == ["2026-11-03T18:06:40+02:00", "2026-11-03T18:07:20+02:00"]
    assert rows[0]["forwarded_ids"] == [13]
    assert summary["batches"] == summary["calls"] == 2
    assert summary["would_intervene"] == 1 and summary["service_messages_skipped"] == 2
    prompt = transport.prompts[0]["messages"][0]["content"]
    assert "[переслано] Вигадана пересилка." in prompt
    assert "РАНІШЕ" in transport.prompts[1]["messages"][0]["content"]


@pytest.mark.parametrize("offsets,groups,fire", [
    ([0, 19, 38, 57, 60], [[10, 11, 12, 13], [14]], [60, 80]),
    ([0, 20], [[10], [11]], [20, 40]),
    ([0, 0, 19], [[10, 11, 12]], [39]),
])
def test_actual_debouncer_boundaries(offsets, groups, fire):
    rows, _, _ = run(invented(offsets))
    assert [r["message_ids"] for r in rows] == groups
    assert [datetime.fromisoformat(r["analyzed_at"]).timestamp() - BASE.timestamp() for r in rows] == fire


def test_context_window_is_production_window_and_pruning_uses_message_time():
    messages = invented([i * 30 for i in range(43)])
    rows, _, transport = run(messages)
    last_prompt = transport.prompts[-1]["messages"][0]["content"]
    chat_rows = [json.loads(line) for line in last_prompt.splitlines() if line.startswith("{")]
    assert [r["n"] for r in chat_rows] == list(range(13, 53))
    assert rows[-1]["message_ids"] == [52]
    # At the next successful analyze production prunes historical context.
    _, _, old = run(invented([0, 4 * 86400, 4 * 86400 + 30]))
    assert '"n": 10,' not in old.prompts[-1]["messages"][0]["content"]


def test_production_cooldown_hostility_and_midnight_daily_reset():
    rows, _, _ = run(invented([0, 100, 300, 600]), transport=Transport([
        decision(), decision(), decision(tension="hostile"), decision(),
    ]))
    assert [r["would_send"] for r in rows] == [True, False, True, False]
    assert rows[1]["reason"] == rows[3]["reason"] == "cooldown"
    rows, _, _ = run(invented([0, 700, 86400]), settings=replace(SETTINGS, max_interventions_per_day=1))
    assert [r["would_send"] for r in rows] == [True, False, True]
    assert rows[1]["reason"] == "daily_intervention_limit"


def test_emergency_safety_and_addressing_use_production_rules():
    rows, _, _ = run(invented([0, 100, 600]), transport=Transport([
        decision(tension="calm", emergency=True), decision(tension="calm", emergency=True),
        decision(tension="calm", emergency=True),
    ]), settings=replace(SETTINGS, max_interventions_per_day=0))
    assert [r["would_send"] for r in rows] == [True, False, True]
    rows, _, _ = run(invented([0]), transport=Transport([decision(text="https://example.invalid")]))
    assert not rows[0]["would_send"] and rows[0]["reason"] == "unsafe_output"
    addressed = invented([0, 10, 20, 30], text="Добросусіде, вигадане звернення")
    rows, _, transport = run(addressed, transport=Transport([decision(tension="calm")] * 4))
    assert [r["would_send"] for r in rows] == [True, True, True, False]
    assert rows[0]["analyzed_at"] == "2026-11-03T18:00:02+02:00"
    assert "автор не представився" in transport.prompts[0]["messages"][0]["content"]
    assert "До тебе звернулися" not in transport.prompts[-1]["messages"][0]["content"]


@pytest.mark.parametrize("cap,expected", [(0, 0), (1, 1), (2, 2)])
def test_call_caps_and_daily_call_limit(cap, expected):
    rows, summary, transport = run(invented([0, 30, 60]), max_calls=cap)
    assert summary["calls"] == len(transport.prompts) == expected
    assert len(rows) == 3
    assert all(r["reason"] == "max_calls" for r in rows[cap:])
    rows, summary, _ = run(invented([0, 30, 86400]), settings=replace(SETTINGS, max_llm_calls_per_day=1))
    assert summary["calls"] == 2 and rows[1]["reason"] == "daily_call_limit"
    assert rows[2]["calls"] == 1


def test_refusal_and_transport_error_are_private_rows_and_consume_budget():
    rows, summary, _ = run(invented([0, 30]), transport=Transport(stop="max_tokens"), max_calls=1)
    assert summary["calls"] == 1 and rows[0]["decision"] is None
    assert rows[0]["stop_reason"] == "max_tokens" and rows[1]["reason"] == "max_calls"
    # A schema failure at the transport boundary is not retried or printed.
    rows, summary, _ = run(invented([0, 30]), transport=Transport(error=ValueError("private sentinel")), max_calls=1)
    assert summary["calls"] == 1 and rows[0]["reason"] == "model_error"
    assert "private sentinel" not in json.dumps(rows)


def test_empty_export_selection_and_dates():
    rows, summary, _ = run(read_export(FIXTURE), start=BASE + timedelta(days=1))
    assert rows == [] and summary["calls"] == summary["batches"] == 0
    rows, _, _ = run(invented([0, 86400]), start=BASE, end=BASE + timedelta(days=1))
    assert [r["message_ids"] for r in rows] == [[10]]


def test_write_failure_aborts_restores_clocks_and_leaves_other_tasks_alone():
    class BrokenWriter:
        def write(self, text):
            raise OSError("private failure detail")

    async def scenario():
        original_time = tool.bot_module.time
        other = asyncio.create_task(asyncio.Event().wait())
        try:
            with pytest.raises(OSError):
                await tool.replay(iter(invented([0])), BrokenWriter(), SETTINGS,
                                  tool.MeteredMessages(Transport()), 50)
            assert not other.done()
            assert tool.bot_module.time is original_time
        finally:
            other.cancel()
            await asyncio.gather(other, return_exceptions=True)

    asyncio.run(scenario())


def test_output_guard_all_worktrees_symlinks_and_existing_directory(tmp_path):
    for target in (tool.REPO, tool.REPO / "new-output"):
        with pytest.raises(ValueError, match="outside"):
            tool.prepare_output(target)
    alias = tmp_path / "alias"
    alias.symlink_to(tool.REPO, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        tool.prepare_output(alias / "new-output")
    result = subprocess.run(["git", "-C", str(tool.REPO), "worktree", "list", "--porcelain"], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            with pytest.raises(ValueError, match="outside"):
                tool.prepare_output(Path(line[9:]) / "new-output")
    outside = tool.prepare_output(tmp_path / "private")
    assert outside.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        tool.prepare_output(outside)
    # An unrelated repository is a worktree too.
    repository = tmp_path / "other"
    repository.mkdir()
    subprocess.run(["git", "init", "--quiet", str(repository)], check=True)
    with pytest.raises(ValueError, match="outside"):
        tool.prepare_output(repository / "new-output")
    outward = repository / "outward"
    outward.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="outside"):
        tool.prepare_output(outward / "new-output")


def cli(tmp_path, name, *args):
    # New interpreter on each invocation: budget and directory guards are tested
    # across real process restarts, not only through an in-process loop.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run([
        sys.executable, "-m", "tools.replay", "--export", str(FIXTURE), "--out", str(tmp_path / name), *args,
    ], cwd=tool.REPO, env=env, capture_output=True, text=True, timeout=30)


def test_cli_stub_end_to_end_restart_and_no_key(tmp_path):
    for name in ("first", "second"):
        result = cli(tmp_path, name, "--stub", "--max-calls", "1")
        assert result.returncode == 0, result.stderr
        summary = json.loads(result.stdout)
        assert summary["calls"] == 1 and summary["batches"] == 2
        assert summary["would_intervene"] == 1 and summary["estimated_cost_usd"] == 0
        assert json.loads((tmp_path / name / "summary.json").read_text()) == summary
        rows = [json.loads(line) for line in (tmp_path / name / "batches.jsonl").read_text().splitlines()]
        assert rows[0]["would_send"] and rows[1]["reason"] == "max_calls"
        assert (tmp_path / name).stat().st_mode & 0o777 == 0o700
    retry = cli(tmp_path, "first", "--stub")
    assert retry.returncode == 1 and "FileExistsError" in retry.stderr
    missing = cli(tmp_path, "no-key")
    assert missing.returncode == 1 and "ANTHROPIC_API_KEY is required" in missing.stderr
    assert not (tmp_path / "no-key").exists()


@pytest.mark.parametrize("args", [("--max-calls", "-1"), ("--max-calls", "nan"), ("--max-calls", "9" * 5000),
                                  ("--input-usd-per-million", "nan"), ("--output-usd-per-million", "inf"),
                                  ("--from", "invalid"), ("--from", "2026-11-04", "--to", "2026-11-03")])
def test_cli_invalid_values_fail_before_any_output(tmp_path, args):
    result = cli(tmp_path, "invalid", "--stub", *args)
    assert result.returncode != 0 and not (tmp_path / "invalid").exists()


def test_env_file_production_config_precedence_and_secret_not_printed(tmp_path, monkeypatch, capsys):
    env_file = tmp_path / "settings.env"
    env_file.write_text('export ANTHROPIC_API_KEY="synthetic"\nMAX_LLM_CALLS_PER_DAY=1\n')
    monkeypatch.setenv("ANTHROPIC_API_KEY", "environment sentinel")
    transport = Transport(tokens=(1000, 100))
    closed = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs == {"timeout": 60.0, "max_retries": 0}
            assert os.environ["ANTHROPIC_API_KEY"] == "environment sentinel"
            logging.getLogger("synthetic.sdk").critical("environment sentinel")
            self.messages = transport

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(anthropic, "AsyncAnthropic", Client)
    assert tool.main(["--export", str(FIXTURE), "--out", str(tmp_path / "real-mode"), "--env-file", str(env_file)]) == 0
    captured = capsys.readouterr()
    assert "environment sentinel" not in captured.out + captured.err
    summary = json.loads(captured.out)
    assert summary["calls"] == 1 and closed == [True]
    assert summary["input_tokens"] == 1000 and summary["output_tokens"] == 100
    assert summary["estimated_cost_usd"] == pytest.approx(0.00015)
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    tool.load_env_file(env_file)
    assert os.environ["ANTHROPIC_API_KEY"] == "synthetic"


def test_cli_output_guard_in_both_modes(tmp_path):
    env_file = tmp_path / "settings.env"
    env_file.write_text("ANTHROPIC_API_KEY=synthetic\n")
    for mode in (["--stub"], ["--env-file", str(env_file)]):
        result = cli(tool.REPO, "forbidden-output", *mode)
        assert result.returncode == 1 and "outside every git worktree" in result.stderr
        assert not (tool.REPO / "forbidden-output").exists()


@pytest.mark.parametrize("key,value", [("DEBOUNCE_SECONDS", "nan"), ("MAX_WAIT_SECONDS", "inf"),
                                      ("COOLDOWN_MINUTES", "-1"), ("CONTEXT_MESSAGES", "0"),
                                      ("MAX_LLM_CALLS_PER_DAY", "-1"), ("TIMEZONE", "invalid")])
def test_invalid_settings_fail_without_sensitive_diagnostics(tmp_path, key, value, monkeypatch, capsys):
    monkeypatch.setenv(key, value)
    assert tool.main(["--export", str(FIXTURE), "--out", str(tmp_path / "out"), "--stub"]) == 1
    assert not (tmp_path / "out").exists()
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize("content", ["not an assignment", 'ANTHROPIC_API_KEY="unclosed'])
def test_env_file_errors_never_echo_values(tmp_path, content, capsys):
    env_file = tmp_path / "invalid.env"
    env_file.write_text(content)
    assert tool.main(["--export", str(FIXTURE), "--out", str(tmp_path / "out"), "--env-file", str(env_file), "--stub"]) == 1
    assert content not in capsys.readouterr().err
