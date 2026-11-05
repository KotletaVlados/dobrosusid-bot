"""Read-only Telegram web polling and date-authoritative outage schedules."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
KYIV = ZoneInfo("Europe/Kyiv")
MAX_HTML_BYTES = 1_000_000
MAX_POSTS = 50
STALE_SECONDS = 2 * 3600
SCHEDULE_GAP_SECONDS = 30 * 60
HEADER = re.compile(
    r"на\s+(сьогодні|завтра),?\s*(\d{2}\.\d{2}\.\d{4})"
    r"[^\n:]*?для\s+черги\s+([0-9]+(?:\.[0-9]+)*)\s*:", re.I,
)
BLOCK_START = re.compile(r"на\s+(?:сьогодні|завтра)\b[^\n]{0,80}?\d{2}\.\d{2}\.\d{4}", re.I)
INTERVAL = re.compile(r"(?<![\d:])(\d{2}:\d{2})\s*[-–—]\s*(\d{2}:\d{2})(?![\d:])")
CLOCK = re.compile(r"\b\d{1,2}:\d{2}\b")
MODEL_CLOCK = re.compile(r"\b\d{1,2}[.:]\d{2}\b")
MODEL_TIME_RANGE = re.compile(
    r"\b(?:з|із|с)?\s*\d{1,2}(?:[.:]\d{2})?\s*(?:год(?:ина|ини|ин)?|ч(?:ас(?:а|ов)?)?)?\s*"
    r"(?:до|[-–—])\s*\d{1,2}(?:[.:]\d{2})?\s*"
    r"(?:год(?:ина|ини|ин)?|ч(?:ас(?:а|ов)?)?)?\b", re.I,
)
QUESTION = re.compile(
    r"(?:де|где|який|какой|покаж\w*|скин\w*|дайте|є|есть)\s+(?:\w+\s+){0,2}граф[іи]к"
    r"|граф[іи]к\w*[^\n]{0,50}(?:світл|свет|відключ|отключ|сьогодні|сегодня|завтра|\?)"
    r"|коли[^\n]{0,40}(?:світл|вимкн|увімкн|відключ)"
    r"|когда[^\n]{0,40}(?:свет|отключ|включ)", re.I,
)


@dataclass(frozen=True)
class Block:
    kind: str
    day: date
    queue: str
    # None means a recognized block whose times could not be validated.
    intervals: tuple[tuple[str, str], ...] | None


@dataclass(frozen=True)
class Post:
    number: int
    published: datetime
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Channel:
    name: str
    posts: tuple[Post, ...]

    def latest(self, day: date, queue: str) -> tuple[Post, Block] | None:
        for post in sorted(self.posts, key=lambda p: (p.published, p.number), reverse=True):
            for block in post.blocks:
                if block.day == day and block.queue == queue:
                    return post, block
        return None


class _Page(HTMLParser):
    """Track nesting, so inline emoji/bold/link tags preserve their text."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str | None]] = []
        self.name: list[str] = []
        self.posts: list[tuple[str, str, str]] = []
        self.post: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        role = None
        if "tgme_widget_message" in classes:
            if not attrs.get("data-post"):
                raise ValueError("outage post without identity")
            if len(self.posts) >= MAX_POSTS:
                raise ValueError("too many outage posts")
            self.post = {"id": attrs["data-post"], "published": "", "text": []}
            role = "post"
        elif "tgme_widget_message_text" in classes:
            role = "text"
        elif "tgme_channel_info_header_title" in classes:
            role = "name"
        if tag == "time" and self.post is not None:
            self.post["published"] = attrs.get("datetime") or ""
        if tag == "br":
            self.handle_data("\n")
        if tag not in {"br", "img", "meta", "link", "hr", "input", "source", "wbr"}:
            self.stack.append((tag, role))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                closed = self.stack[i:]
                del self.stack[i:]
                if any(role == "post" for _, role in closed) and self.post is not None:
                    p = self.post
                    self.posts.append((p["id"], p["published"], "".join(p["text"])))
                    self.post = None
                return

    def handle_data(self, data):
        if any(role == "name" for _, role in self.stack):
            self.name.append(data)
        if self.post is not None and any(role == "text" for _, role in self.stack):
            self.post["text"].append(data)


def _minutes(value: str) -> int:
    hour, minute = map(int, value.split(":"))
    if hour > 24 or minute > 59 or (hour == 24 and minute != 0):
        raise ValueError("invalid outage time")
    return hour * 60 + minute


def _intervals(text: str) -> tuple[tuple[str, str], ...] | None:
    pairs = tuple(INTERVAL.findall(text))
    if len(pairs) > 48:
        return None
    try:
        if len(CLOCK.findall(text)) != 2 * len(pairs):
            return None
        previous_end = -1
        for start, end in pairs:
            a, b = _minutes(start), _minutes(end)
            if not previous_end <= a < b:
                return None
            previous_end = b
    except ValueError:
        return None
    if pairs:
        return pairs
    if re.search(r"відключень(?:\s+за\s+графіком)?\s+(?:немає|не\s+(?:буде|передбачено|заплановано))", text, re.I):
        return ()
    return None


def parse_channel(html: str) -> Channel:
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("outage page too large")
    page = _Page()
    try:
        page.feed(html)
    except AssertionError as exc:
        raise ValueError("invalid outage markup") from exc
    name = " ".join("".join(page.name).split())
    if not name or len(name) > 160 or not page.posts or page.post is not None:
        raise ValueError("unrecognized outage page")
    posts = []
    for identity, published, text in page.posts:
        stamp = datetime.fromisoformat(published)
        if stamp.tzinfo is None:
            raise ValueError("outage post without timezone")
        blocks = []
        boundaries = list(BLOCK_START.finditer(text))
        for i, boundary in enumerate(boundaries):
            header = HEADER.match(text, boundary.start())
            if header is None:
                # A malformed or wrapped date header still ends the prior block.
                continue
            kind, day, queue = header.groups()
            body = text[header.end():boundaries[i + 1].start() if i + 1 < len(boundaries) else len(text)]
            blocks.append(Block(kind.lower(), datetime.strptime(day, "%d.%m.%Y").date(), queue, _intervals(body)))
            if blocks[-1].intervals is None:
                log.warning("outage parse: invalid intervals in recognized block")
        posts.append(Post(int(identity.rsplit("/", 1)[-1]), stamp, tuple(blocks)))
    return Channel(name, tuple(posts))


class OutageSource:
    """One poll task owns one bounded snapshot; requests never wait on the network."""

    def __init__(self, channel: str, poll_minutes: float):
        self.url = f"https://t.me/s/{channel}"
        self.period = poll_minutes * 60
        self.snapshot: Channel | None = None
        self.fetched_at: float | None = None
        self.failures = 0

    async def poll_once(self, client: httpx.AsyncClient) -> float:
        try:
            async with asyncio.timeout(15):
                async with client.stream("GET", self.url) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_HTML_BYTES:
                            raise ValueError("outage page too large")
                parsed = parse_channel(body.decode("utf-8"))
            self.snapshot, self.fetched_at = parsed, time.time()
            self.failures = 0
            log.info("outage poll: ok, posts=%d", len(parsed.posts))
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            self.failures = min(self.failures + 1, 8)
            # No response bodies or arbitrary source text in diagnostics.
            log.warning("outage poll: failed (%s)", type(exc).__name__)
        return min(self.period * 2 ** self.failures, max(self.period, 3600))

    async def run(self) -> None:
        async with httpx.AsyncClient(
            timeout=10, headers={"User-Agent": "DobrosusidBot outage schedule reader"},
            follow_redirects=False,
        ) as client:
            while True:
                delay = await self.poll_once(client)
                await asyncio.sleep(delay)

    def answer(self, queue: str, now: float) -> str:
        today = datetime.fromtimestamp(now, KYIV).date()
        current = self.snapshot.latest(today, queue) if self.snapshot else None
        if (self.fetched_at is None or now - self.fetched_at > STALE_SECONDS
                or current is None or current[1].intervals is None):
            return f"Актуального графіка на сьогодні ({today:%d.%m.%Y}) для черги {queue} у мене зараз немає."
        lines = [f"Графік відключень для черги {queue}:"]
        for label, day in (("сьогодні", today), ("завтра", today + timedelta(days=1))):
            found = self.snapshot.latest(day, queue)
            if found is None or found[1].intervals is None:
                continue
            post, block = found
            times = ", ".join(f"{a}–{b}" for a, b in block.intervals) or "відключень немає"
            updated = post.published.astimezone(KYIV).strftime("%H:%M")
            lines.append(f"На {label}, {day:%d.%m.%Y}: {times}; оновлено о {updated}.")
        lines.append(f"Джерело: {self.snapshot.name}. Графік може змінитися.")
        return "\n".join(lines)
