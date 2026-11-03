"""Налаштування бота з змінних оточення (див. .env.example)."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _ids(raw: str) -> frozenset[int]:
    return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part)


def _bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    telegram_token: str
    # Бот працює лише в групах, куди його додав хтось із власників,
    # або в групах, явно перелічених у ALLOWED_CHAT_IDS.
    owner_ids: frozenset[int]
    allowed_chat_ids: frozenset[int]
    model: str = "claude-haiku-5-5"
    persona_name: str = "Котлетик-добросусід"
    # Хто створив бота: він згадує автора, коли його питають «хто ти?», і в /help.
    author: str = "Владислав «Котлета»"
    db_path: str = "dobrosusid.sqlite3"
    timezone: str = "Europe/Kyiv"
    # Аналіз запускається, коли в чаті на debounce секунд затихло,
    # але не пізніше max_wait секунд від першого неопрацьованого повідомлення.
    debounce_seconds: float = 20
    max_wait_seconds: float = 60
    context_messages: int = 40
    # Самовільні втручання (коли до бота ніхто не звертався).
    cooldown_minutes: float = 10
    max_interventions_per_day: int = 15
    # Запобіжник від неочікуваних витрат на API.
    max_llm_calls_per_day: int = 1500
    intro_reminders: bool = True
    # Не частіше одного повідомлення-нагадування в чат за цей час.
    intro_reminder_interval_minutes: float = 20
    # Кожній людині — до intro_reminder_max нагадувань з інтервалом intro_reminder_repeat_days.
    intro_reminder_repeat_days: float = 3
    intro_reminder_max: int = 3
    # Новачок не може писати в чат, поки не представиться боту (потрібне право обмежувати учасників).
    gate_new_members: bool = True
    # Якщо новачок так і не представився (чи бот лежав), обмеження знімається само через стільки годин.
    gate_timeout_hours: float = 24
    # Ліміти на одну людину: звернення до бота в чаті та спроби знайомства в особистих.
    address_limit_per_10min: int = 3
    address_limit_per_day: int = 20
    dm_llm_limit_per_day: int = 10
    # Домени, які бот може згадувати у відповідях (через кому). За замовчуванням — жодних посилань.
    allowed_link_domains: frozenset[str] = frozenset()

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            telegram_token=env["TELEGRAM_BOT_TOKEN"],
            owner_ids=_ids(env.get("OWNER_IDS", "")),
            allowed_chat_ids=_ids(env.get("ALLOWED_CHAT_IDS", "")),
            model=env.get("ANTHROPIC_MODEL", cls.model),
            persona_name=env.get("PERSONA_NAME", cls.persona_name),
            author=env.get("AUTHOR", cls.author),
            db_path=env.get("DB_PATH", cls.db_path),
            timezone=env.get("TIMEZONE", cls.timezone),
            debounce_seconds=float(env.get("DEBOUNCE_SECONDS", cls.debounce_seconds)),
            max_wait_seconds=float(env.get("MAX_WAIT_SECONDS", cls.max_wait_seconds)),
            context_messages=int(env.get("CONTEXT_MESSAGES", cls.context_messages)),
            cooldown_minutes=float(env.get("COOLDOWN_MINUTES", cls.cooldown_minutes)),
            max_interventions_per_day=int(
                env.get("MAX_INTERVENTIONS_PER_DAY", cls.max_interventions_per_day)
            ),
            max_llm_calls_per_day=int(env.get("MAX_LLM_CALLS_PER_DAY", cls.max_llm_calls_per_day)),
            intro_reminders=_bool(env.get("INTRO_REMINDERS", "true")),
            intro_reminder_interval_minutes=float(
                env.get("INTRO_REMINDER_INTERVAL_MINUTES", cls.intro_reminder_interval_minutes)
            ),
            intro_reminder_repeat_days=float(env.get("INTRO_REMINDER_REPEAT_DAYS", cls.intro_reminder_repeat_days)),
            intro_reminder_max=int(env.get("INTRO_REMINDER_MAX", cls.intro_reminder_max)),
            gate_new_members=_bool(env.get("GATE_NEW_MEMBERS", "true")),
            gate_timeout_hours=float(env.get("GATE_TIMEOUT_HOURS", cls.gate_timeout_hours)),
            address_limit_per_10min=int(env.get("ADDRESS_LIMIT_PER_10MIN", cls.address_limit_per_10min)),
            address_limit_per_day=int(env.get("ADDRESS_LIMIT_PER_DAY", cls.address_limit_per_day)),
            dm_llm_limit_per_day=int(env.get("DM_LLM_LIMIT_PER_DAY", cls.dm_llm_limit_per_day)),
            allowed_link_domains=frozenset(
                d.strip().lower() for d in env.get("ALLOWED_LINK_DOMAINS", "").split(",") if d.strip()
            ),
        )
