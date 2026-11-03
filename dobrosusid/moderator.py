"""Рішення «втрутитися чи промовчати»: транскрипт для моделі, виклик Claude і правила частоти."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

import anthropic
from pydantic import BaseModel

from .prompts import EXTRACT_SYSTEM, SYSTEM_PROMPT
from .storage import Resident, StoredMessage

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 1200
# Екстренну пам'ятку (101–104) повторюємо в чаті не частіше, ніж раз на 10 хвилин.
EMERGENCY_GAP_SECONDS = 600


class Introduction(BaseModel):
    # Номер повідомлення, а не user_id: автора за номером визначає код, тож «представити» сусіда не вийде.
    message_id: int
    name: str
    apartment: str


class Decision(BaseModel):
    tension: Literal["calm", "tense", "conflict", "hostile"]
    emergency: bool
    action: Literal["none", "reply"]
    reply_to_message_id: int | None
    text: str
    introductions: list[Introduction]


class ExtractedIntro(BaseModel):
    found: bool
    name: str
    apartment: str


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
    """Одне повідомлення — один JSON-рядок: перенос рядка чи нік у профілі не підробить чужу репліку."""
    resident = residents.get(msg.user_id) if msg.user_id and not msg.is_bot else None
    text = msg.text if len(msg.text) <= MAX_MESSAGE_CHARS else msg.text[:MAX_MESSAGE_CHARS] + "…[обрізано]"
    line = json.dumps(
        {
            "n": msg.message_id,
            "time": datetime.fromtimestamp(msg.ts, tz).strftime("%H:%M"),
            "bot": msg.is_bot,
            "author": None if msg.is_bot else msg.author,
            "uid": None if msg.is_bot else msg.user_id,
            "resident": f"{resident.name}, кв. {resident.apartment}" if resident else None,
            "reply_to": msg.reply_to,
            "text": text,
        },
        ensure_ascii=False,
    )
    # «<» екрануємо, щоб текст не міг закрити тег <chat>.
    return line.replace("<", "\\u003c")


def build_user_prompt(
    transcript: str, addressed_ids: list[int], strangers: frozenset[int] = frozenset()
) -> str:
    """strangers — номери звернень від тих, хто ще не представився."""
    prompt = f"Переписка чату (час за Києвом):\n<chat>\n{transcript}\n</chat>"
    if addressed_ids:
        ids = ", ".join(f"#{i}" + (" (автор не представився)" if i in strangers else "") for i in addressed_ids)
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
    last_emergency_ts: float | None = None,
) -> bool:
    """Модель пропонує, код вирішує: самовільно бот говорить лише при справжньому конфлікті і не частіше ліміту."""
    if decision.action != "reply" or not decision.text.strip():
        return False
    if addressed:
        return True
    if decision.emergency and (last_emergency_ts is None or now - last_emergency_ts >= EMERGENCY_GAP_SECONDS):
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

    async def decide(
        self, transcript: str, addressed_ids: list[int], strangers: frozenset[int] = frozenset()
    ) -> Decision | None:
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=4000,
            output_config={"effort": "medium"},
            system=self.system,
            messages=[{"role": "user", "content": build_user_prompt(transcript, addressed_ids, strangers)}],
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

    async def extract_intro(self, text: str) -> tuple[str, str] | None:
        """Ім'я та квартира з вільної відповіді в особистих («мене звати Оля, живу в 12-й»)."""
        response = await self.client.messages.parse(
            model=self.model,
            max_tokens=1000,
            output_config={"effort": "low"},
            system=EXTRACT_SYSTEM,
            messages=[{"role": "user", "content": f"<answer>\n{text[:MAX_MESSAGE_CHARS]}\n</answer>"}],
            output_format=ExtractedIntro,
        )
        intro = response.parsed_output if response.stop_reason == "end_turn" else None
        if intro is None or not intro.found or not intro.name.strip() or not intro.apartment.strip():
            return None
        return intro.name.strip()[:40], intro.apartment.strip()[:10]
