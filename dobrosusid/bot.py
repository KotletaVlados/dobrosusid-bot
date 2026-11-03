"""Telegram-частина: обробники оновлень, команди, надсилання відповідей."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import anthropic
from telegram import (
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    MessageEntity,
    ReactionTypeEmoji,
    ReplyParameters,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import mention_html

from . import prompts
from .config import Settings
from .moderator import Decision, Moderator, render_transcript, should_intervene
from .safety import clean_intro, unsafe_reason
from .scheduler import Debouncer
from .storage import PendingIntro, Storage, StoredMessage

log = logging.getLogger(__name__)

GROUPS = filters.ChatType.GROUPS
IAM_RE = re.compile(
    r"^(?P<name>.+?)[\s,;]+(?:кв\.?|квартира|kv\.?|apt\.?|№)?\s*(?P<apt>\d{1,4}[a-zа-яіїє]?)\s*$",
    re.IGNORECASE,
)
MAX_REMINDER_NAMES = 5
MAX_GREETINGS_PER_JOIN = 5
INTRO_REACTION = "🤝"
INTRO_PAYLOAD = "intro_"
GATE_CHECK_SECONDS = 600


def parse_iam(raw: str) -> tuple[str, str] | None:
    match = IAM_RE.match(raw.strip())
    if not match:
        return None
    return clean_intro(match["name"].strip(" ,;-—"), match["apt"])


def apartment_key(apartment: str) -> tuple[int, str]:
    digits = re.match(r"\d+", apartment)
    return (int(digits[0]) if digits else 10**6, apartment)


class DobrosusidBot:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tz = ZoneInfo(settings.timezone)
        self.store = Storage(settings.db_path)
        self.moderator = Moderator(
            anthropic.AsyncAnthropic(timeout=60.0), settings.model, settings.persona_name
        )
        self.debouncer = Debouncer(
            self.analyze, settings.debounce_seconds, settings.max_wait_seconds
        )
        self.app: Application = (
            ApplicationBuilder().token(settings.telegram_token).post_init(self._post_init).build()
        )
        self._background: set[asyncio.Task] = set()
        self._register_handlers()

    def run(self) -> None:
        self.app.run_polling(allowed_updates=Update.ALL_TYPES)

    # --- налаштування -------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self.app
        app.add_handler(ChatMemberHandler(self.on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
        app.add_handler(MessageHandler(filters.StatusUpdate.MIGRATE, self.on_migrate))
        app.add_handler(MessageHandler(GROUPS & filters.StatusUpdate.NEW_CHAT_MEMBERS, self.on_new_members))
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("rules", self.cmd_rules))
        app.add_handler(CommandHandler("iam", self.cmd_iam, filters=GROUPS))
        app.add_handler(CommandHandler("who", self.cmd_who, filters=GROUPS))
        app.add_handler(CommandHandler("forgetme", self.cmd_forgetme, filters=GROUPS))
        app.add_handler(CommandHandler("intro", self.cmd_intro, filters=GROUPS))
        app.add_handler(CommandHandler("pause", self.cmd_pause, filters=GROUPS))
        app.add_handler(CommandHandler("resume", self.cmd_resume, filters=GROUPS))
        app.add_handler(CommandHandler("letin", self.cmd_letin, filters=GROUPS))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(
            MessageHandler(
                GROUPS & filters.UpdateType.MESSAGE & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
                self.on_group_message,
            )
        )
        app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, self.on_private))

    async def _post_init(self, app: Application) -> None:
        member_commands = [
            BotCommand("rules", "Правила чату"),
            BotCommand("iam", "Представитися: ім'я, квартира"),
            BotCommand("who", "Хто вже представився"),
            BotCommand("forgetme", "Видалити моє представлення"),
            BotCommand("help", "Що я вмію"),
        ]
        await app.bot.set_my_commands(member_commands, scope=BotCommandScopeAllGroupChats())
        # Адмінські команди видно в меню лише адміністраторам; права все одно перевіряє _is_admin.
        await app.bot.set_my_commands(
            member_commands
            + [
                BotCommand("status", "Стан бота в цьому чаті"),
                BotCommand("letin", "Пустити новачка (відповіддю на привітання)"),
                BotCommand("intro", "Оголошення-знайомство"),
                BotCommand("pause", "Поставити бота на паузу"),
                BotCommand("resume", "Зняти з паузи"),
            ],
            scope=BotCommandScopeAllChatAdministrators(),
        )
        await app.bot.set_my_commands(
            [BotCommand("start", "Що це за бот"), BotCommand("rules", "Правила чату")],
            scope=BotCommandScopeAllPrivateChats(),
        )
        task = asyncio.create_task(self._gate_watchdog())
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        log.info("бот @%s запущено, модель %s", app.bot.username, self.settings.model)

    # --- доступ ---------------------------------------------------------------

    def _chat_allowed(self, chat_id: int) -> bool:
        if chat_id in self.settings.allowed_chat_ids:
            if self.store.get_chat(chat_id) is None:
                self.store.upsert_chat(chat_id, None, None)
            return True
        chat = self.store.get_chat(chat_id)
        return bool(chat and chat.active and chat.added_by in self.settings.owner_ids)

    async def _is_admin(self, update: Update) -> bool:
        user = update.effective_user
        if user is None:
            return False
        if user.id in self.settings.owner_ids:
            return True
        chat = update.effective_chat
        if chat is None or chat.type == ChatType.PRIVATE:
            return False
        member = await chat.get_member(user.id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)

    def _day_start(self) -> float:
        return datetime.now(self.tz).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # --- службові оновлення ----------------------------------------------------

    async def on_my_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        change = update.my_chat_member
        chat = change.chat
        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return
        status = change.new_chat_member.status
        if status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
            log.info("мене прибрали з чату %s (%s)", chat.id, chat.title)
            self.store.deactivate_chat(chat.id)
            return
        if status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
            return

        existing = self.store.get_chat(chat.id)
        adder = change.from_user.id
        known = bool(existing and existing.active and existing.added_by in self.settings.owner_ids)
        if not (known or adder in self.settings.owner_ids or chat.id in self.settings.allowed_chat_ids):
            log.warning("чужий чат %s (%s), додав %s — виходжу", chat.id, chat.title, adder)
            await context.bot.leave_chat(chat.id)
            return

        first_time = existing is None or not existing.active
        self.store.upsert_chat(chat.id, chat.title, adder if adder in self.settings.owner_ids else None)
        log.info("чат %s (%s): статус %s, додав %s", chat.id, chat.title, status, adder)
        if first_time:
            await self._announce(chat.id)

    async def on_migrate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg.migrate_to_chat_id:
            log.warning("чат %s став супергрупою %s", msg.chat_id, msg.migrate_to_chat_id)
            self.store.migrate_chat(msg.chat_id, msg.migrate_to_chat_id)

    async def on_new_members(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not self._chat_allowed(msg.chat_id):
            return
        residents = self.store.residents(msg.chat_id)
        newcomers = [u for u in msg.new_chat_members if not u.is_bot and u.id not in residents]
        if not newcomers or self.store.get_chat(msg.chat_id).paused:
            return
        for user in newcomers[:MAX_GREETINGS_PER_JOIN]:
            # Новачок пише в чат лише після знайомства; кнопка веде в особисті з ботом.
            muted = self.settings.gate_new_members and await self._set_muted(msg.chat_id, user.id, True)
            template = prompts.GREETING_GATED if muted else prompts.GREETING
            sent = await self._send(
                msg.chat_id,
                template.format(name=mention_html(user.id, user.first_name)),
                html_mode=True,
                reply_to=msg.message_id,
                markup=self._intro_markup(msg.chat_id),
            )
            self.store.add_pending(msg.chat_id, user.id, sent.message_id, muted=muted)
            self.store.mark_reminded(msg.chat_id, [user.id])
            log.info("чат %s: новачок %s, без права писати: %s", msg.chat_id, user.id, muted)
        self.store.add_event(msg.chat_id, "greeting")

    # --- звичайні повідомлення --------------------------------------------------

    async def on_group_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or not self._chat_allowed(msg.chat_id):
            return
        text = msg.text or msg.caption
        if not text:
            return
        if msg.sender_chat is not None:
            user_id, author = None, msg.sender_chat.title or "Канал"
        elif msg.from_user is not None and not msg.from_user.is_bot:
            user_id, author = msg.from_user.id, msg.from_user.full_name
        else:
            return
        if msg.forward_origin is not None:
            text = "[переслано] " + text
        addressed = self._is_addressed(msg) and user_id is not None and self._may_address(msg.chat_id, user_id)
        self.store.add_message(
            msg.chat_id,
            msg.message_id,
            user_id,
            author,
            text,
            msg.reply_to_message.message_id if msg.reply_to_message else None,
            msg.date.timestamp(),
            addressed=addressed,
        )
        self.debouncer.poke(msg.chat_id, urgent=addressed)

    def _is_addressed(self, msg: Message) -> bool:
        bot = self.app.bot
        replied = msg.reply_to_message
        if replied and replied.from_user and replied.from_user.id == bot.id:
            return True
        entities = msg.parse_entities() if msg.text else msg.parse_caption_entities()
        for entity, value in entities.items():
            if entity.type == MessageEntity.MENTION and value.lower() == f"@{bot.username.lower()}":
                return True
            if entity.type == MessageEntity.TEXT_MENTION and entity.user and entity.user.id == bot.id:
                return True
        return self.settings.persona_name.lower() in (msg.text or msg.caption or "").lower()

    def _may_address(self, chat_id: int, user_id: int) -> bool:
        """Ліміт звернень на людину: зверх нього звернення читається як звичайне повідомлення.

        Так один тролль не змусить бота відповідати щохвилини і не спалить денний бюджет за всіх.
        """
        now = time.time()
        if (
            self.store.count_user_events(user_id, "addressed", now - 600) >= self.settings.address_limit_per_10min
            or self.store.count_user_events(user_id, "addressed", self._day_start())
            >= self.settings.address_limit_per_day
        ):
            log.info("чат %s: %s вичерпав ліміт звернень", chat_id, user_id)
            return False
        self.store.add_event(chat_id, "addressed", now, user_id=user_id)
        return True

    # --- знайомство в особистих -------------------------------------------------

    def _intro_markup(self, chat_id: int) -> InlineKeyboardMarkup:
        link = f"https://t.me/{self.app.bot.username}?start={INTRO_PAYLOAD}{chat_id}"
        return InlineKeyboardMarkup([[InlineKeyboardButton(prompts.INTRO_BUTTON, url=link)]])

    async def _start_intro(self, update: Update, chat_id: int) -> None:
        msg, user = update.effective_message, update.effective_user
        if not self._chat_allowed(chat_id):
            await msg.reply_text(prompts.DM_IDLE)
            return
        try:
            member = await self.app.bot.get_chat_member(chat_id, user.id)
            is_member = member.status in (
                ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER
            ) or (member.status == ChatMemberStatus.RESTRICTED and member.is_member)
        except TelegramError:
            is_member = False
        if not is_member:
            await msg.reply_text(prompts.DM_NOT_MEMBER)
            return
        self.store.add_pending(chat_id, user.id, None, muted=False)
        title = self.store.get_chat(chat_id).title or "сусідів"
        await msg.reply_text(prompts.DM_ASK.format(persona=self.settings.persona_name, chat=title))

    async def on_private(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        pending = self.store.pending_for_user(user.id) if user else None
        if pending is None or not msg.text:
            await msg.reply_text(prompts.DM_IDLE)
            return
        day = self._day_start()
        llm_allowed = (
            self.store.count_events("llm", day) < self.settings.max_llm_calls_per_day
            and self.store.count_user_events(user.id, "dm_llm", day) < self.settings.dm_llm_limit_per_day
        )
        # Модель краще розуміє вільну відповідь («мене звати Оля, живу в 12-й»), регулярка — запасний шлях.
        intro = None
        if llm_allowed:
            self.store.add_event(pending.chat_id, "llm")
            self.store.add_event(pending.chat_id, "dm_llm", user_id=user.id)
            try:
                extracted = await self.moderator.extract_intro(msg.text)
                # Модель лише витягує, а що вважати іменем, вирішує код.
                intro = clean_intro(*extracted) if extracted else None
            except anthropic.APIError:
                log.exception("помилка Claude API під час знайомства")
                intro = parse_iam(msg.text)
        else:
            intro = parse_iam(msg.text)
        if intro is None:
            await msg.reply_text(prompts.DM_RETRY)
            return
        name, apartment = intro
        unmuted = await self._register_resident(pending.chat_id, user.id, name, apartment)
        template = prompts.DM_DONE_UNMUTED if unmuted else prompts.DM_DONE
        await msg.reply_text(template.format(name=name, apartment=apartment))

    async def _register_resident(self, chat_id: int, user_id: int, name: str, apartment: str) -> bool:
        """Записує сусіда; якщо він чекав знайомства — відкриває чат і оновлює привітання. True, якщо зняли обмеження."""
        self.store.set_resident(chat_id, user_id, name, apartment)
        log.info("чат %s: представився %s, кв. %s", chat_id, name, apartment)
        pending: PendingIntro | None = self.store.get_pending(chat_id, user_id)
        if pending is None:
            return False
        self.store.drop_pending(chat_id, user_id)
        unmuted = pending.muted and await self._set_muted(chat_id, user_id, False)
        if pending.greeting_message_id:
            try:
                await self.app.bot.edit_message_text(
                    prompts.INTRODUCED_IN_CHAT.format(name=name, apartment=apartment),
                    chat_id,
                    pending.greeting_message_id,
                )
            except TelegramError as exc:
                log.info("не вдалося оновити привітання: %s", exc)
        return unmuted

    async def _let_in(self, pending: PendingIntro) -> bool:
        """Відкриває чат новачку без знайомства (адмін пустив або минув таймаут)."""
        if not await self._set_muted(pending.chat_id, pending.user_id, False):
            return False
        # Запис «чекає знайомства» не прибираємо: кнопка «Представитися» лишається робочою.
        self.store.unmark_muted(pending.chat_id, pending.user_id)
        return True

    async def _gate_watchdog(self) -> None:
        """Новачок не повинен сидіти без права писати вічно, навіть якщо бот лежав чи модель збоїла."""
        while True:
            try:
                cutoff = time.time() - self.settings.gate_timeout_hours * 3600
                for pending in self.store.muted_before(cutoff):
                    if await self._let_in(pending):
                        log.info("чат %s: %s не представився за %s год — відкрив чат", pending.chat_id,
                                 pending.user_id, self.settings.gate_timeout_hours)
            except Exception:
                log.exception("сторож новачків упав")
            await asyncio.sleep(GATE_CHECK_SECONDS)

    async def _set_muted(self, chat_id: int, user_id: int, muted: bool) -> bool:
        permissions = ChatPermissions.no_permissions() if muted else ChatPermissions.all_permissions()
        try:
            await self.app.bot.restrict_chat_member(chat_id, user_id, permissions)
            return True
        except TelegramError as exc:
            # Немає права «Блокувати користувачів» або це звичайна група, а не супергрупа.
            log.warning("чат %s: не вдалося %s %s: %s", chat_id, "обмежити" if muted else "відкрити", user_id, exc)
            return False

    # --- аналіз ------------------------------------------------------------------

    async def analyze(self, chat_id: int) -> None:
        chat = self.store.get_chat(chat_id)
        if chat is None or not chat.active:
            return
        new = self.store.messages_after(chat_id, chat.last_analyzed_id)
        if not new:
            return
        self.store.set_last_analyzed(chat_id, new[-1].message_id)
        if chat.paused:
            return
        if self.store.count_events("llm", self._day_start()) >= self.settings.max_llm_calls_per_day:
            log.warning("денний ліміт викликів моделі вичерпано, пропускаю чат %s", chat_id)
            return

        residents = self.store.residents(chat_id)
        context_messages = self.store.recent_messages(chat_id, self.settings.context_messages)
        transcript = render_transcript(context_messages, residents, chat.last_analyzed_id, self.tz)
        addressed_ids = [m.message_id for m in new if m.addressed]
        strangers = frozenset(m.message_id for m in new if m.addressed and m.user_id not in residents)
        self.store.add_event(chat_id, "llm")
        try:
            decision = await self.moderator.decide(transcript, addressed_ids, strangers)
        except anthropic.APIError:
            log.exception("помилка Claude API для чату %s", chat_id)
            return
        if decision is None:
            return
        log.info(
            "чат %s: %d нових, напруга=%s, дія=%s, представлень=%d",
            chat_id, len(new), decision.tension, decision.action, len(decision.introductions),
        )

        await self._save_introductions(chat_id, decision, new)
        spoke = await self._maybe_intervene(chat_id, decision, addressed_ids, context_messages)
        if not spoke and self.settings.intro_reminders:
            await self._maybe_remind(chat_id, new)
        self.store.prune_messages()

    async def _save_introductions(self, chat_id: int, decision: Decision, new: list[StoredMessage]) -> None:
        by_id = {m.message_id: m for m in new if m.user_id}
        for intro in decision.introductions:
            # Автора визначаємо за номером повідомлення з цієї пачки — так «представити» сусіда не вийде.
            msg = by_id.get(intro.message_id)
            cleaned = clean_intro(intro.name, intro.apartment)
            if msg is None or cleaned is None:
                continue
            await self._register_resident(chat_id, msg.user_id, *cleaned)
            await self._react(chat_id, msg.message_id)

    async def _maybe_intervene(
        self,
        chat_id: int,
        decision: Decision,
        addressed_ids: list[int],
        context_messages: list[StoredMessage],
    ) -> bool:
        addressed = bool(addressed_ids)
        allowed = should_intervene(
            decision,
            addressed=addressed,
            last_intervention_ts=self.store.last_event(chat_id, "intervention"),
            interventions_today=self.store.count_events("intervention", self._day_start(), chat_id),
            now=time.time(),
            cooldown_minutes=self.settings.cooldown_minutes,
            max_per_day=self.settings.max_interventions_per_day,
            last_emergency_ts=self.store.last_event(chat_id, "emergency"),
        )
        if not allowed:
            return False
        text = decision.text.strip()
        reason = unsafe_reason(text, self.settings.allowed_link_domains)
        if reason:
            # Від імені бота посилання, картки чи телефони звучать «офіційно» — це шлях для шахраїв.
            log.warning("чат %s: відповідь не надіслано (%s): %r", chat_id, reason, text)
            return False
        known_ids = {m.message_id for m in context_messages}
        reply_to = decision.reply_to_message_id if decision.reply_to_message_id in known_ids else None
        if reply_to is None and addressed:
            reply_to = addressed_ids[-1]
        await self._send(chat_id, text, reply_to=reply_to)
        if decision.emergency:
            self.store.add_event(chat_id, "emergency")
        self.store.add_event(chat_id, "answer" if addressed else "intervention")
        return True

    async def _maybe_remind(self, chat_id: int, new: list[StoredMessage]) -> None:
        last = self.store.last_event(chat_id, "reminder")
        if last and time.time() - last < self.settings.intro_reminder_interval_minutes * 60:
            return
        residents = self.store.residents(chat_id)
        now = time.time()
        pending: dict[int, str] = {}
        for m in new:
            if (
                m.user_id
                and m.user_id not in residents
                and m.user_id not in pending
                and self.store.needs_reminder(
                    chat_id,
                    m.user_id,
                    now=now,
                    repeat_seconds=self.settings.intro_reminder_repeat_days * 86400,
                    max_count=self.settings.intro_reminder_max,
                )
            ):
                pending[m.user_id] = m.author
        if not pending:
            return
        chosen = list(pending.items())[:MAX_REMINDER_NAMES]
        names = ", ".join(mention_html(uid, name) for uid, name in chosen)
        await self._send(
            chat_id, prompts.INTRO_REMINDER.format(names=names), html_mode=True, markup=self._intro_markup(chat_id)
        )
        self.store.mark_reminded(chat_id, [uid for uid, _ in chosen], now)
        self.store.add_event(chat_id, "reminder")

    # --- надсилання ----------------------------------------------------------------

    async def _send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        html_mode: bool = False,
        markup: InlineKeyboardMarkup | None = None,
    ) -> Message:
        sent = await self.app.bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML if html_mode else None,
            reply_parameters=(
                ReplyParameters(message_id=reply_to, allow_sending_without_reply=True) if reply_to else None
            ),
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
        # Власні повідомлення бота Telegram не присилає, тож пишемо їх у контекст самі.
        self.store.add_message(
            chat_id, sent.message_id, self.app.bot.id, self.settings.persona_name,
            sent.text or text, reply_to, sent.date.timestamp(), is_bot=True,
        )
        return sent

    async def _announce(self, chat_id: int) -> None:
        await self._send(
            chat_id,
            prompts.ANNOUNCEMENT.format(persona=self.settings.persona_name),
            markup=self._intro_markup(chat_id),
        )

    async def _react(self, chat_id: int, message_id: int) -> bool:
        try:
            await self.app.bot.set_message_reaction(chat_id, message_id, ReactionTypeEmoji(INTRO_REACTION))
            return True
        except TelegramError as exc:
            log.info("не вдалося поставити реакцію: %s", exc)
            return False

    # --- команди ----------------------------------------------------------------------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        payload = context.args[0] if context.args else ""
        if update.effective_chat.type == ChatType.PRIVATE and payload.startswith(INTRO_PAYLOAD):
            try:
                chat_id = int(payload.removeprefix(INTRO_PAYLOAD))
            except ValueError:
                chat_id = None
            if chat_id is not None:
                await self._start_intro(update, chat_id)
                return
        await self.cmd_help(update, context)

    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = prompts.HELP.format(persona=self.settings.persona_name)
        if update.effective_chat.type == ChatType.PRIVATE:
            user = update.effective_user
            owner = "так ✅" if user.id in self.settings.owner_ids else "ні"
            text += f"\nВаш Telegram ID: {user.id}\nВласник бота: {owner}"
        await update.effective_message.reply_text(text)

    async def cmd_rules(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_text(prompts.RULES, parse_mode=ParseMode.HTML)

    async def cmd_iam(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if not self._chat_allowed(msg.chat_id) or user is None or msg.sender_chat is not None:
            return
        parsed = parse_iam(" ".join(context.args))
        if parsed is None:
            await msg.reply_text("Напишіть ім'я та номер квартири, наприклад: /iam Олена, 45")
            return
        name, apartment = parsed
        await self._register_resident(msg.chat_id, user.id, name, apartment)
        if not await self._react(msg.chat_id, msg.message_id):
            await msg.reply_text(f"Приємно познайомитися, {name}! 🤝")

    async def cmd_who(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if not self._chat_allowed(msg.chat_id):
            return
        residents = sorted(self.store.residents(msg.chat_id).values(), key=lambda r: apartment_key(r.apartment))
        if not residents:
            await msg.reply_text("Поки ніхто не представився. Почніть першим: /iam Ім'я, квартира")
            return
        lines = [f"кв. {html.escape(r.apartment)} — {html.escape(r.name)}" for r in residents]
        await msg.reply_text(
            f"<b>Сусіди, що представилися ({len(lines)}):</b>\n" + "\n".join(lines),
            parse_mode=ParseMode.HTML,
        )

    async def cmd_forgetme(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg, user = update.effective_message, update.effective_user
        if not self._chat_allowed(msg.chat_id) or user is None:
            return
        if self.store.forget_resident(msg.chat_id, user.id):
            await msg.reply_text("Готово, я забув ваше представлення.")
        else:
            await msg.reply_text("Ви ще не представлялися — забувати нічого.")

    async def cmd_intro(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._chat_allowed(update.effective_chat.id) and await self._is_admin(update):
            await self._announce(update.effective_chat.id)

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._chat_allowed(update.effective_chat.id) and await self._is_admin(update):
            self.store.set_paused(update.effective_chat.id, True)
            await update.effective_message.reply_text("Беру паузу 🤐 Повернуся командою /resume.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._chat_allowed(update.effective_chat.id) and await self._is_admin(update):
            self.store.set_paused(update.effective_chat.id, False)
            await update.effective_message.reply_text("Я знову з вами 🙂")

    async def cmd_letin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/letin відповіддю на привітання новачка — пустити його в чат без знайомства."""
        msg = update.effective_message
        if not self._chat_allowed(msg.chat_id) or not await self._is_admin(update):
            return
        target = msg.reply_to_message
        pending = self.store.pending_by_greeting(msg.chat_id, target.message_id) if target else None
        if pending is None:
            await msg.reply_text("Дайте цю команду відповіддю на моє привітання новачка.")
        elif not pending.muted:
            await msg.reply_text("Ця людина вже може писати в чаті 🙂")
        elif await self._let_in(pending):
            await msg.reply_text("Готово, відкрив чат 👍 Познайомитися можна й пізніше — кнопкою в привітанні.")
        else:
            await msg.reply_text("Не вийшло — перевірте, чи є в мене право «Блокувати користувачів».")

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        if not await self._is_admin(update):
            return
        if chat.type == ChatType.PRIVATE:
            chats = self.store.list_chats()
            body = "\n\n".join(await self._chat_status(c.chat_id) for c in chats) or "Я поки не в жодному чаті."
            await update.effective_message.reply_text(body)
        elif self._chat_allowed(chat.id):
            await update.effective_message.reply_text(await self._chat_status(chat.id))

    async def _chat_status(self, chat_id: int) -> str:
        chat = self.store.get_chat(chat_id)
        bot = self.app.bot
        try:
            me = await bot.get_chat_member(chat_id, bot.id)
            is_admin = me.status == ChatMemberStatus.ADMINISTRATOR
            sees_all = is_admin or bot.can_read_all_group_messages
            can_restrict = is_admin and me.can_restrict_members
        except TelegramError:
            sees_all = can_restrict = False
        if not self.settings.gate_new_members:
            gate = "Новачки без знайомства: можуть писати (шлюз вимкнено)"
        elif can_restrict:
            gate = f"Новачки без знайомства: не можуть писати; чекають знайомства — {self.store.count_muted(chat_id)}"
        else:
            gate = "Новачки без знайомства: можуть писати — дайте мені право «Блокувати користувачів»"
        since = self._day_start()
        counts = {
            kind: self.store.count_events(kind, since, chat_id)
            for kind in ("llm", "intervention", "answer", "reminder", "greeting")
        }
        return "\n".join(
            [
                f"Чат: {chat.title or chat_id}",
                "Стан: на паузі" if chat.paused else "Стан: працюю",
                "Бачу всі повідомлення: так"
                if sees_all
                else "Бачу всі повідомлення: НІ — зробіть мене адміністратором чату",
                gate,
                f"Сьогодні: аналізів {counts['llm']}, втручань {counts['intervention']}, "
                f"відповідей {counts['answer']}, нагадувань {counts['reminder']}, привітань {counts['greeting']}",
                f"Представилися: {len(self.store.residents(chat_id))}",
            ]
        )


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    for noisy in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    DobrosusidBot(Settings.from_env()).run()
