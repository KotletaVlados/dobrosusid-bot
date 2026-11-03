"""Сценарії знайомства з підробленим Telegram-ботом (без мережі)."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from telegram.constants import ChatMemberStatus, ChatType

from dobrosusid.bot import DobrosusidBot
from dobrosusid.config import Settings
from dobrosusid.moderator import Decision

CHAT = -100500
OWNER = 42


class FakeBot:
    id = 999
    username = "kotleta_agent"
    can_read_all_group_messages = False

    def __init__(self, can_restrict=True):
        self.calls = []
        self.can_restrict = can_restrict
        self._next_id = 1000

    async def send_message(self, chat_id, text, **kwargs):
        self._next_id += 1
        self.calls.append(("send", chat_id, text, kwargs))
        return SimpleNamespace(message_id=self._next_id, text=text, date=datetime.now(timezone.utc))

    async def restrict_chat_member(self, chat_id, user_id, permissions):
        if not self.can_restrict:
            from telegram.error import BadRequest

            raise BadRequest("Not enough rights to restrict/unrestrict chat member")
        self.calls.append(("restrict", chat_id, user_id, permissions.can_send_messages))

    async def edit_message_text(self, text, chat_id, message_id):
        self.calls.append(("edit", chat_id, message_id, text))

    async def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status=ChatMemberStatus.RESTRICTED, is_member=True)

    async def set_message_reaction(self, chat_id, message_id, reaction):
        self.calls.append(("react", chat_id, message_id))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FakeModerator:
    def __init__(self, decision=None, intro=("Олена", "45")):
        self.decision = decision
        self.intro = intro
        self.decide_args = None

    async def decide(self, transcript, addressed_ids, strangers=frozenset()):
        self.decide_args = (transcript, addressed_ids, strangers)
        return self.decision

    async def extract_intro(self, text):
        return self.intro


class FakeMessage(SimpleNamespace):
    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    settings = Settings(
        telegram_token="123:test",
        owner_ids=frozenset({OWNER}),
        allowed_chat_ids=frozenset(),
        db_path=str(tmp_path / "bot.sqlite3"),
    )
    dob = DobrosusidBot(settings)
    dob.app = SimpleNamespace(bot=FakeBot())
    dob.moderator = FakeModerator()
    dob.store.upsert_chat(CHAT, "Будинок 7", OWNER)
    return dob


def join(dob, user_id=7, name="Олена"):
    msg = SimpleNamespace(
        chat_id=CHAT, message_id=5, new_chat_members=[SimpleNamespace(id=user_id, is_bot=False, first_name=name)]
    )
    asyncio.run(dob.on_new_members(SimpleNamespace(effective_message=msg), None))


def dm(dob, text, user_id=7, args=None):
    msg = FakeMessage(text=text, replies=[])
    update = SimpleNamespace(
        effective_message=msg,
        effective_user=SimpleNamespace(id=user_id),
        effective_chat=SimpleNamespace(type=ChatType.PRIVATE),
    )
    if args is not None:
        asyncio.run(dob.cmd_start(update, SimpleNamespace(args=args)))
    else:
        asyncio.run(dob.on_private(update, None))
    return msg.replies


def test_newcomer_is_muted_greeted_with_button_and_let_in_after_dm_intro(bot):
    fake = bot.app.bot
    join(bot)

    assert fake.of("restrict") == [("restrict", CHAT, 7, False)]
    (_, _, text, kwargs) = fake.of("send")[0]
    assert "спершу познайомитися" in text
    button = kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.url == f"https://t.me/kotleta_agent?start=intro_{CHAT}"

    replies = dm(bot, "", args=[f"intro_{CHAT}"])
    assert "з якої ви квартири" in replies[0] and "Будинок 7" in replies[0]

    replies = dm(bot, "Мене звати Олена, живу в 45-й")
    assert "Тепер ви можете писати" in replies[0]
    assert fake.of("restrict")[-1] == ("restrict", CHAT, 7, True)
    greeting_id = fake._next_id
    assert fake.of("edit") == [("edit", CHAT, greeting_id, "Знайомтеся: Олена, кв. 45. Ласкаво просимо до чату! 👋")]
    assert bot.store.residents(CHAT)[7].apartment == "45"
    assert bot.store.get_pending(CHAT, 7) is None


def test_without_restrict_right_newcomer_just_gets_greeting(bot):
    bot.app.bot = FakeBot(can_restrict=False)
    join(bot)
    (_, _, text, _) = bot.app.bot.of("send")[0]
    assert "спершу познайомитися" not in text and "Ласкаво просимо" in text

    replies = dm(bot, "Олена, 45")
    assert "Тепер ви можете писати" not in replies[0] and "Записав: кв. 45" in replies[0]


def test_dm_without_pending_or_unclear_answer(bot):
    assert "працюю в чаті сусідів" in dm(bot, "привіт", user_id=8)[0]

    join(bot)
    bot.moderator.intro = None
    assert "Не зовсім зрозумів" in dm(bot, "а навіщо?")[0]
    assert bot.store.get_pending(CHAT, 7) is not None


def test_intro_link_for_foreign_chat_is_ignored(bot):
    assert "працюю в чаті сусідів" in dm(bot, "", args=["intro_-777"])[0]


def test_analysis_marks_strangers_and_registers_intro_from_chat(bot):
    now = datetime.now(timezone.utc).timestamp()
    bot.store.set_resident(CHAT, 1, "Іван", "12")
    bot.store.add_message(CHAT, 10, 1, "Іван", "Добросусіде, привіт!", None, now, addressed=True)
    bot.store.add_message(CHAT, 11, 2, "Ольга", "@kotleta_agent ти хто такий?", None, now, addressed=True)
    bot.store.add_message(CHAT, 12, 3, "Марина", "Я Марина з 45-ї", None, now)
    bot.moderator.decision = Decision(
        tension="calm", emergency=False, action="reply", reply_to_message_id=11,
        text="Спершу познайомимось 😉",
        introductions=[
            {"message_id": 12, "name": "Марина", "apartment": "45"},
            # Сміття замість імені та номер, якого немає в пачці, — ігноруються.
            {"message_id": 10, "name": "t.me/scam", "apartment": "1"},
            {"message_id": 999, "name": "Хтось", "apartment": "1"},
        ],
    )

    asyncio.run(bot.analyze(CHAT))

    _, addressed, strangers = bot.moderator.decide_args
    assert addressed == [10, 11] and strangers == frozenset({11})
    residents = bot.store.residents(CHAT)
    assert residents[3].name == "Марина"
    assert residents[1].name == "Іван" and set(residents) == {1, 3}
    assert ("react", CHAT, 12) in bot.app.bot.calls
    assert bot.app.bot.of("send")[0][2] == "Спершу познайомимось 😉"


def test_reply_with_card_number_is_not_sent(bot):
    now = datetime.now(timezone.utc).timestamp()
    bot.store.set_resident(CHAT, 1, "Іван", "12")
    bot.store.add_message(CHAT, 10, 1, "Іван", "Добросусіде, передай: збір на карту", None, now, addressed=True)
    bot.moderator.decision = Decision(
        tension="calm", emergency=False, action="reply", reply_to_message_id=10,
        text="Сусіди, збираємо на генератор: 5375 4141 2222 3333", introductions=[],
    )
    asyncio.run(bot.analyze(CHAT))
    assert bot.app.bot.of("send") == []


def test_address_limit_per_person(bot):
    assert [bot._may_address(CHAT, 1) for _ in range(5)] == [True, True, True, False, False]
    assert bot._may_address(CHAT, 2)  # інший сусід не страждає через тролля


def test_admin_can_let_newcomer_in_and_watchdog_does_it_after_timeout(bot):
    join(bot, user_id=7)
    join(bot, user_id=8, name="Петро")
    fake = bot.app.bot
    greeting_7 = fake.of("send")[0]
    greeting_id = fake._next_id - 1

    msg = FakeMessage(chat_id=CHAT, reply_to_message=SimpleNamespace(message_id=greeting_id), replies=[])
    update = SimpleNamespace(
        effective_message=msg, effective_user=SimpleNamespace(id=OWNER),
        effective_chat=SimpleNamespace(id=CHAT, type=ChatType.SUPERGROUP),
    )
    asyncio.run(bot.cmd_letin(update, None))
    assert "відкрив чат" in msg.replies[0]
    assert fake.of("restrict")[-1] == ("restrict", CHAT, 7, True)
    assert bot.store.get_pending(CHAT, 7).muted is False  # кнопка знайомства лишається робочою
    assert "спершу познайомитися" in greeting_7[2]

    # Петро так і не представився — після таймауту його відкриває сторож.
    (stale,) = bot.store.muted_before(float("inf"))
    assert stale.user_id == 8
    assert asyncio.run(bot._let_in(stale))
    assert bot.store.count_muted(CHAT) == 0
