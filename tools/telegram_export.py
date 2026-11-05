"""Read Telegram Desktop HTML pages without following links or opening attachments."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path


@dataclass(frozen=True)
class ExportMessage:
    message_id: int
    date: datetime | None
    author: str
    text: str
    reply_to: int | None
    service: bool
    forwarded: bool

    @property
    def user_id(self) -> int:
        # Independent of page order, filters and Python's randomized hash seed.
        return int.from_bytes(hashlib.sha256(self.author.encode()).digest()[:8], "big") & ((1 << 63) - 1) or 1


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node | str] = field(default_factory=list)

    def has(self, cls: str) -> bool:
        return cls in self.attrs.get("class", "").split()

    def descendants(self, *, skip_forwarded: bool = False):
        for child in self.children:
            if isinstance(child, _Node):
                if skip_forwarded and child.has("forwarded"):
                    continue
                yield child
                yield from child.descendants(skip_forwarded=skip_forwarded)

    def text(self) -> str:
        return "".join(
            child if isinstance(child, str) else "\n" if child.tag == "br" else child.text()
            for child in self.children
        )


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


def read_export(directory: Path):
    """Yield in page order, including service/date separators (which may lack time).

    Only messages*.html are opened. Relative links, scripts and media are inert.
    Missing timestamps/authors on ordinary messages are errors, not silent loss.
    """
    pages = []
    for path in directory.glob("messages*.html"):
        match = re.fullmatch(r"messages(\d*)\.html", path.name)
        if match:
            pages.append((int(match[1] or 1), path))
    if not pages:
        raise ValueError("No Telegram HTML pages found")
    previous_author = ""
    last_id = 0
    last_date = None
    for _, path in sorted(pages):
        parser = _Page()
        with path.open(encoding="utf-8-sig") as source:
            while chunk := source.read(65536):
                parser.feed(chunk)
        parser.close()
        for node in parser.root.descendants():
            if node.tag != "div" or not node.has("message"):
                continue
            match = re.fullmatch(r"message(-?\d+)", node.attrs.get("id", ""))
            if not match:
                raise ValueError("Invalid Telegram message id")
            message_id = int(match[1])
            service = node.has("service")
            own = list(node.descendants(skip_forwarded=True))
            full = list(node.descendants())
            stamp = next((n.attrs["title"] for n in own if n.has("date") and "title" in n.attrs), None)
            try:
                date = datetime.strptime(stamp, "%d.%m.%Y %H:%M:%S UTC%z") if stamp else None
            except ValueError:
                raise ValueError("Invalid Telegram message timestamp") from None
            if date is None and not service:
                raise ValueError("Missing Telegram message timestamp")
            author = next((n.text().strip() for n in own if n.has("from_name")), "")
            if not service:
                if not author and node.has("joined"):
                    author = previous_author
                if not author:
                    raise ValueError("Missing Telegram message author")
                previous_author = author
                if message_id <= last_id or (last_date is not None and date < last_date):
                    raise ValueError("Telegram messages must have increasing ids and chronological timestamps")
                last_id, last_date = message_id, date
            text = "\n".join(n.text().strip() for n in full if n.has("text"))
            if service and not text:
                text = "\n".join(n.text().strip() for n in own if n.has("body"))
            reply = next((n for n in own if n.has("reply_to")), None)
            reply_id = None
            if reply:
                for link in reply.descendants():
                    target = re.search(r"GoToMessage\((\d+)\)", link.attrs.get("onclick", "") + link.attrs.get("href", ""))
                    if target:
                        reply_id = int(target[1])
                        break
            yield ExportMessage(message_id, date, author, text, reply_id, service, any(n.has("forwarded") for n in full))
