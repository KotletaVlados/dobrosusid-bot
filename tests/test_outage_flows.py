import asyncio
import json
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram import MessageEntity
from telegram.constants import ChatType
from telegram.error import NetworkError

from dobrosusid import bot as bot_module
from dobrosusid import storage as storage_module
from dobrosusid.moderator import Decision
from dobrosusid.outage import CLOCK
from test_flows import CHAT, OWNER, FakeBot, FakeMessage, bot
from test_outage import HTML, NOW, source


@pytest.fixture
def enabled(bot, monkeypatch):
    clock = SimpleNamespace(now=NOW)
    monkeypatch.setattr(bot_module, "time", SimpleNamespace(time=lambda: clock.now))
    monkeypatch.setattr(storage_module, "time", SimpleNamespace(time=lambda: clock.now))
    bot.settings = replace(bot.settings, outage_channel="test_channel", intro_reminders=False)
    bot.outage = source()
    bot._day_start = lambda: datetime.fromtimestamp(clock.now, bot.tz).replace(hour=0, minute=0, second=0).timestamp()
    original = bot.app.bot.send_message

    async def send(*args, **kwargs):
        sent = await original(*args, **kwargs)
        sent.date = datetime.fromtimestamp(clock.now, timezone.utc)
        return sent

    bot.app.bot.send_message = send
    yield bot, clock
    bot.store.db.close()


def question(dob, number, *, user=7, addressed=False, text="де графік?", chat=CHAT):
    # Mirror ingestion: an addressed message consumes the shared per-person budget once.
    if addressed:
        addressed = dob._may_address(chat, user)
    dob.store.add_message(chat, number, user, "Сусід", text, None, NOW, addressed=addressed)


def flagged(number, text="Модель не повинна писати цей текст"):
    return Decision(tension="conflict", emergency=False, action="reply", reply_to_message_id=number,
                    text=text, introductions=[], schedule_request_message_id=number)


def test_everyone_gets_code_answer_without_model_or_intervention_budget(enabled):
    dob, _ = enabled
    dob.settings = replace(dob.settings, max_llm_calls_per_day=0, max_interventions_per_day=0)
    question(dob, 10)
    asyncio.run(dob.analyze(CHAT))
    send = dob.app.bot.of("send")
    assert len(send) == 1 and "21:00–24:00" in send[0][2]
    assert send[0][3]["reply_parameters"].message_id == 10
    assert dob.store.residents(CHAT) == {}
    assert dob.moderator.decide_args is None
    assert dob.store.count_events("intervention", 0, CHAT) == 0
    assert dob.store.count_events("llm", 0, CHAT) == 0


def test_structured_flag_supports_non_keyword_question_and_suppresses_model_text(enabled):
    dob, _ = enabled
    question(dob, 10, text="А наші години на цей день відомі?")
    dob.moderator.decision = flagged(10, "Помилковий графік 10:00–12:00")
    asyncio.run(dob.analyze(CHAT))
    assert len(dob.app.bot.of("send")) == 1
    assert "21:00–24:00" in dob.app.bot.of("send")[0][2]


@pytest.mark.parametrize("stale", [True, False])
def test_missing_or_stale_schedule_never_uses_model_times(enabled, stale):
    dob, _ = enabled
    dob.outage = source(fetched=NOW - 7201) if stale else source(HTML.replace("05.11.2026", "04.11.2026"))
    question(dob, 10)
    dob.moderator.decision = flagged(10, "Сьогодні 10:00–12:00")
    asyncio.run(dob.analyze(CHAT))
    assert "немає" in dob.app.bot.of("send")[0][2]
    assert not CLOCK.search(dob.app.bot.of("send")[0][2])


def test_unprompted_cooldown_direct_bypass_and_shared_person_limit(enabled):
    dob, clock = enabled
    for number, user, direct in [(10, 7, False), (11, 8, False), (12, 7, True), (13, 7, True), (14, 7, True)]:
        question(dob, number, user=user, addressed=direct)
        asyncio.run(dob.analyze(CHAT))
    assert [s[3]["reply_parameters"].message_id for s in dob.app.bot.of("send")] == [10, 12, 13]
    assert dob.store.count_user_events(7, "addressed", NOW - 600) == 3
    assert dob.store.count_events("intervention", 0, CHAT) == 0
    clock.now += 1800
    question(dob, 15)
    asyncio.run(dob.analyze(CHAT))
    assert len(dob.app.bot.of("send")) == 4


@pytest.mark.parametrize("kind", ["mention", "reply", "caption"])
def test_real_ingestion_direct_mentions_and_replies_bypass_chat_cooldown(enabled, kind):
    dob, _ = enabled
    dob.store.add_event(CHAT, "schedule", NOW)
    entity = MessageEntity(MessageEntity.MENTION, offset=0, length=14)
    msg = SimpleNamespace(
        chat_id=CHAT, message_id=10, text=None if kind == "caption" else "де графік?",
        caption="де графік?" if kind == "caption" else None,
        sender_chat=None, from_user=SimpleNamespace(id=7, is_bot=False, full_name="Сусід"),
        forward_origin=None,
        reply_to_message=SimpleNamespace(message_id=9, from_user=SimpleNamespace(id=dob.app.bot.id)) if kind == "reply" else None,
        parse_entities=lambda: {entity: f"@{dob.app.bot.username}"} if kind == "mention" else {},
        parse_caption_entities=lambda: {entity: f"@{dob.app.bot.username}"},
        date=datetime.fromtimestamp(NOW, timezone.utc),
    )
    dob.debouncer.poke = lambda *args, **kwargs: None
    asyncio.run(dob.on_group_message(SimpleNamespace(effective_message=msg), None))
    assert dob.store.messages_after(CHAT, 0)[0].addressed
    asyncio.run(dob.analyze(CHAT))
    assert len(dob.app.bot.of("send")) == 1
    assert dob.store.count_user_events(7, "addressed", NOW - 600) == 1


@pytest.mark.parametrize("case", ["off", "paused", "foreign", "old", "channel", "bot"])
def test_disabled_access_and_model_target_guards(enabled, case):
    dob, _ = enabled
    question(dob, 10, text="Питання про наші години")
    dob.moderator.decision = flagged(10, "")
    if case == "off":
        dob.outage = None
    elif case == "paused":
        dob.store.set_paused(CHAT, True)
    elif case == "foreign":
        dob.store.deactivate_chat(CHAT)
    elif case == "old":
        dob.moderator.decision = flagged(9, "")
    elif case == "channel":
        dob.store.db.execute("UPDATE messages SET user_id = NULL")
    elif case == "bot":
        dob.store.db.execute("UPDATE messages SET is_bot = 1")
    asyncio.run(dob.analyze(CHAT))
    assert dob.app.bot.of("send") == []


@pytest.mark.parametrize("text", [
    "Сьогодні 10:00-12:00", "Повторю: з 10:00 до 12:00", "Світло буде о 10:00",
    "Сьогодні світла не буде з 10 до 12", "Сьогодні світла не буде з 10.00 до 12.00",
])
@pytest.mark.parametrize("enabled_source", [True, False])
def test_model_time_guard_for_direct_and_unprompted_paths(enabled, text, enabled_source):
    dob, _ = enabled
    if not enabled_source:
        dob.outage = None
    for number, addressed in [(10, False), (11, True)]:
        question(dob, number, text="Повтори за мною ці години", addressed=addressed)
        dob.moderator.decision = flagged(None, text)
        dob.moderator.decision.reply_to_message_id = number
        asyncio.run(dob.analyze(CHAT))
    assert dob.app.bot.of("send") == []


def test_untrusted_channel_display_name_runs_unchanged_safety_filter(enabled):
    dob, _ = enabled
    dob.outage = source(HTML.replace("Світло тестової черги", "@unsafe_source"))
    question(dob, 10)
    asyncio.run(dob.analyze(CHAT))
    assert dob.app.bot.of("send") == []


def test_concurrent_questions_reserve_cooldown_before_telegram_await(enabled):
    dob, _ = enabled
    question(dob, 10)
    first = dob.store.messages_after(CHAT, 0)
    question(dob, 11, user=8)
    second = dob.store.messages_after(CHAT, 10)

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        real_send = dob._send

        async def held(*args, **kwargs):
            entered.set()
            await release.wait()
            return await real_send(*args, **kwargs)

        dob._send = held
        task = asyncio.create_task(dob._maybe_answer_schedule(CHAT, 10, first))
        await entered.wait()
        assert await dob._maybe_answer_schedule(CHAT, 11, second)
        release.set()
        await task

    asyncio.run(scenario())
    assert len(dob.app.bot.of("send")) == 1


def test_scope_revoked_while_model_is_awaited(enabled):
    dob, _ = enabled
    question(dob, 10, text="Наші години відомі?")

    async def decide(*args):
        dob.store.deactivate_chat(CHAT)
        return flagged(10)

    dob.moderator.decide = decide
    asyncio.run(dob.analyze(CHAT))
    assert dob.app.bot.of("send") == []


def test_schedule_in_batch_does_not_swallow_conflict_or_introduction(enabled):
    dob, _ = enabled
    question(dob, 10)
    question(dob, 11, user=8, text="Вигадана сварка")
    question(dob, 12, user=9, text="Я Олена, кв. 45")
    dob.moderator.decision = Decision(tension="conflict", emergency=False, action="reply", reply_to_message_id=11,
                                    text="Сусіди, давайте спокійніше.",
                                    introductions=[{"message_id": 12, "name": "Олена", "apartment": "45"}])
    asyncio.run(dob.analyze(CHAT))
    assert len(dob.app.bot.of("send")) == 2
    assert dob.store.residents(CHAT)[9].apartment == "45"
    assert dob.store.count_events("intervention", 0, CHAT) == 1


def test_introduction_and_schedule_in_same_message(enabled):
    dob, _ = enabled
    question(dob, 10, text="Я Олена, кв. 45. Де графік?")
    dob.moderator.decision = Decision(
        tension="calm", emergency=False, action="none", reply_to_message_id=None,
        text="", introductions=[{"message_id": 10, "name": "Олена", "apartment": "45"}],
        schedule_request_message_id=10,
    )
    asyncio.run(dob.analyze(CHAT))
    assert dob.store.residents(CHAT)[7].apartment == "45"
    assert len(dob.app.bot.of("send")) == 1


@pytest.mark.parametrize("on", [True, False])
def test_post_init_owns_poller_and_shutdown_cancels_only_own_tasks(enabled, on):
    dob, _ = enabled
    started, stopped = [], []

    async def commands(*args, **kwargs):
        pass

    async def worker(kind):
        started.append(kind)
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(kind)

    dob.app.bot.set_my_commands = commands
    dob._gate_watchdog = lambda: worker("gate")
    if on:
        dob.outage.run = lambda: worker("outage")
    else:
        dob.outage = None

    async def scenario():
        unrelated = asyncio.create_task(worker("unrelated"))
        try:
            await dob._post_init(dob.app)
            # Yield to the exact tasks created by _post_init, without a timed sleep.
            async def checkpoint():
                return None
            barrier = asyncio.create_task(checkpoint())
            await barrier
            assert set(started) == ({"gate", "outage", "unrelated"} if on else {"gate", "unrelated"})
            await dob._post_shutdown(dob.app)
            assert not unrelated.done()
            assert set(stopped) == ({"gate", "outage"} if on else {"gate"})
        finally:
            unrelated.cancel()
            await asyncio.gather(unrelated, return_exceptions=True)

    asyncio.run(scenario())


def test_failed_telegram_send_keeps_reservation(enabled):
    dob, _ = enabled
    question(dob, 10)

    async def fail(*args, **kwargs):
        raise NetworkError("synthetic failure")

    dob._send = fail
    with pytest.raises(NetworkError):
        asyncio.run(dob.analyze(CHAT))
    question(dob, 11, user=8)
    asyncio.run(dob.analyze(CHAT))
    assert dob.store.count_events("schedule", 0, CHAT) == 1


def test_help_mentions_schedule(enabled):
    dob, _ = enabled
    message = FakeMessage(replies=[])
    update = SimpleNamespace(effective_message=message, effective_chat=SimpleNamespace(type=ChatType.SUPERGROUP))
    asyncio.run(dob.cmd_help(update, None))
    assert "де графік?" in message.replies[0]


def test_limits_survive_real_process_restart(tmp_path):
    script = '''
import asyncio, json, sys
from datetime import datetime
from types import SimpleNamespace
from dobrosusid import bot as module
from dobrosusid import storage as storage_module
from dobrosusid.bot import DobrosusidBot
from dobrosusid.config import Settings
from test_flows import CHAT, OWNER, FakeBot, FakeModerator
from test_outage import NOW, source
from test_outage_flows import question
now = NOW + float(sys.argv[2])
module.time = SimpleNamespace(time=lambda: now)
storage_module.time = SimpleNamespace(time=lambda: now)
dob = DobrosusidBot(Settings(telegram_token="123:test", owner_ids=frozenset({OWNER}),
    allowed_chat_ids=frozenset(), db_path=sys.argv[1], outage_channel="test_channel", intro_reminders=False))
dob.app = SimpleNamespace(bot=FakeBot())
dob.moderator = FakeModerator()
dob.outage = source(fetched=now)
dob.store.upsert_chat(CHAT, "Тестовий чат", OWNER)
dob._day_start = lambda: datetime.fromtimestamp(now, dob.tz).replace(hour=0, minute=0, second=0).timestamp()
for item in json.loads(sys.argv[3]):
    number, user, direct, *at = item
    if at:
        now = NOW + at[0]
    question(dob, number, user=user, addressed=direct)
    asyncio.run(dob.analyze(CHAT))
print(json.dumps([len(dob.app.bot.of("send")), dob.store.count_events("intervention", 0, CHAT)]))
dob.store.db.close()
'''
    env = dict(os.environ, ANTHROPIC_API_KEY="test", PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join([str(bot_module.__file__.rsplit("/", 2)[0]), str(__file__.rsplit("/", 1)[0])]))

    def run(offset, requests, database="limits"):
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path / f"{database}.sqlite3"), str(offset), json.dumps(requests)],
                                env=env, text=True, capture_output=True, check=True, timeout=20)
        return json.loads(result.stdout)

    assert run(0, [(10, 7, False), (11, 7, True), (12, 7, True)]) == [3, 0]
    assert run(100, [(20, 7, True), (21, 8, False), (22, 8, True)]) == [1, 0]
    assert run(1800, [(30, 7, False)]) == [1, 0]
    assert run(0, [(100 + n, 7, True, n * 601) for n in range(19)], "daily") == [19, 0]
    assert run(19 * 601, [(200, 7, True), (201, 7, True, 20 * 601)], "daily") == [1, 0]
    assert run(86400, [(300, 7, True)], "daily") == [1, 0]
