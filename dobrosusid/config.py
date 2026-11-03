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
    persona_name: str = "Добросусід"
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
    intro_reminder_interval_minutes: float = 20

    @classmethod
    def from_env(cls) -> Settings:
        env = os.environ
        return cls(
            telegram_token=env["TELEGRAM_BOT_TOKEN"],
            owner_ids=_ids(env.get("OWNER_IDS", "")),
            allowed_chat_ids=_ids(env.get("ALLOWED_CHAT_IDS", "")),
            model=env.get("ANTHROPIC_MODEL", cls.model),
            persona_name=env.get("PERSONA_NAME", cls.persona_name),
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
        )
