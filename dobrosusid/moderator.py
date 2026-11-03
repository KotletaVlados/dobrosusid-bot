"""Рішення «втрутитися чи промовчати»: транскрипт для моделі, виклик Claude і правила частоти."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel

from .prompts import SYSTEM_PROMPT
from .storage import Resident, StoredMessage

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1200


class Introduction(BaseModel):
    user_id: int
    name: str
    apartment: str


class Decision(BaseModel):
    tension: Literal["calm", "tense", "conflict", "hostile"]
    emergency: bool
    action: Literal["none", "reply"]
    reply_to_message_id: int | None
    text: str
    introductions: list[Introduction]


def render_transcript(
    messages: list[StoredMessage],
    residents: dict[int, Resident],
    last_analyzed_id: int,
    tz: ZoneInfo,
) -> str:
    """Переписка у вигляді, зручному для моделі; нові повідомлення — окремим блоком."""
    old, new = [], []
    for msg in messages:
        line = _render_line(msg, residents, tz)
        (new if msg.message_id > last_analyzed_id and not msg.is_bot else old).append(line)
    parts = []
    if old:
        parts.append("РАНІШЕ (контекст, уже оцінено):\n" + "\n".join(old))
    parts.append("НОВІ:\n" + "\n".join(new))
    return "\n\n".join(parts)


def _render_line(msg: StoredMessage, residents: dict[int, Resident], tz: ZoneInfo) -> str:
    when = datetime.fromtimestamp(msg.ts, tz).strftime("%H:%M")
    if msg.is_bot:
        who = "БОТ"
    else:
        resident = residents.get(msg.user_id) if msg.user_id else None
        tag = f"[{resident.name}, кв. {resident.apartment}]" if resident else "[не представився]"
        who = f"{msg.author} {tag} (id {msg.user_id})"
    reply = f" ↩#{msg.reply_to}" if msg.reply_to else ""
    text = msg.text if len(msg.text) <= MAX_MESSAGE_CHARS else msg.text[:MAX_MESSAGE_CHARS] + "…[обрізано]"
    return f"#{msg.message_id} {when} {who}{reply}: {text}"


def build_user_prompt(transcript: str, addressed_ids: list[int]) -> str:
    prompt = f"Переписка чату (час за Києвом):\n<chat>\n{transcript}\n</chat>"
    if addressed_ids:
        ids = ", ".join(f"#{i}" for i in addressed_ids)
        prompt += f"\n\nДо тебе звернулися напряму в {ids} — обов'язково дай відповідь."
    return prompt


def should_intervene(
    decision: Decision,
    *,
    addressed: bool,
    last_intervention_ts: float | None,
    interventions_today: int,
    now: float,
    cooldown_minutes: float,
    max_per_day: int,
) -> bool:
    """Модель пропонує, код вирішує: самовільно бот говорить лише при справжньому конфлікті і не частіше ліміту."""
    if decision.action != "reply" or not decision.text.strip():
        return False
    if addressed or decision.emergency:
        return True
    if decision.tension not in ("conflict", "hostile"):
        return False
    if interventions_today >= max_per_day:
        return False
    if last_intervention_ts is None:
        return True
    # При образах і погрозах бот може озватися вдвічі раніше.
    cooldown = cooldown_minutes * (0.5 if decision.tension == "hostile" else 1)
    return now - last_intervention_ts >= cooldown * 60


class Moderator:
    def __init__(self, client: anthropic.AsyncAnthropic, model: str, persona: str):
        self.client = client
        self.model = model
        self.system = SYSTEM_PROMPT.format(persona=persona)

    async def decide(self, transcript: str, addressed_ids: list[int]) -> Decision | None:
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            output_config={"effort": "medium"},
            system=self.system,
            messages=[{"role": "user", "content": build_user_prompt(transcript, addressed_ids)}],
            output_format=Decision,
        )
        log.info(
            "llm stop=%s in=%s out=%s",
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )
        if response.stop_reason != "end_turn":
            # refusal або max_tokens — краще промовчати, ніж відповісти навмання.
            log.warning("llm stop_reason=%s, пропускаю", response.stop_reason)
            return None
        return response.parsed_output
