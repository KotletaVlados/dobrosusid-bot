"""Перевірки, які тримає код, а не модель: що можна записати як ім'я і що бот може опублікувати."""

from __future__ import annotations

import re

NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁёІіЇїЄєҐґ'’ʼ\- ]{2,40}$")
APARTMENT_RE = re.compile(r"^\d{1,4}[a-zа-яіїє]?$", re.IGNORECASE)
APARTMENT_PREFIX_RE = re.compile(r"^(?:кв\.?|квартира|kv\.?|apt\.?|№)\s*", re.IGNORECASE)
NAME_STOPWORDS = ("адмін", "admin", "осбб", "бот", "bot", "модератор", "керуюч", "добросусід", "котлетик")

DOMAIN_RE = re.compile(r"\b(?:[a-z0-9][a-z0-9-]{0,62}\.)+[a-z]{2,}\b", re.IGNORECASE)
USERNAME_RE = re.compile(r"@[A-Za-z0-9_]{4,}")
CARD_RE = re.compile(r"(?:\d[ \-]?){13,19}")
IBAN_RE = re.compile(r"\bUA\d{2}[\d ]{10,}", re.IGNORECASE)
PHONE_RE = re.compile(r"\+?\d[\d\s()\-]{8,}\d")


def clean_intro(name: str, apartment: str) -> tuple[str, str] | None:
    """Ім'я — лише літери, пробіл, дефіс, апостроф; квартира — номер. Інакше None."""
    name = " ".join(name.split())
    apartment = APARTMENT_PREFIX_RE.sub("", apartment.strip())
    if not NAME_RE.match(name) or not APARTMENT_RE.match(apartment):
        return None
    lowered = name.lower()
    if any(word in lowered for word in NAME_STOPWORDS):
        return None
    return name, apartment


def unsafe_reason(text: str, allowed_domains: frozenset[str] = frozenset()) -> str | None:
    """Чому цей текст не можна публікувати від імені бота (посилання, реквізити, контакти), або None."""
    for match in DOMAIN_RE.finditer(text):
        domain = match.group(0).lower()
        if not any(domain == d or domain.endswith("." + d) for d in allowed_domains):
            return f"посилання {domain}"
    if USERNAME_RE.search(text):
        return "@нік"
    if IBAN_RE.search(text):
        return "IBAN"
    if CARD_RE.search(text):
        return "номер картки"
    if PHONE_RE.search(text):
        return "телефон"
    return None
