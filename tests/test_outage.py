import asyncio
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from dobrosusid import outage
from dobrosusid.config import Settings
from dobrosusid.outage import OutageSource, parse_channel
from dobrosusid.safety import unsafe_reason

HTML = (Path(__file__).parent / "fixtures/outage_channel.html").read_text()
NOW = datetime(2026, 11, 5, 11, tzinfo=timezone.utc).timestamp()
TODAY = date(2026, 11, 5)


def source(html=HTML, fetched=NOW):
    result = OutageSource("test_channel", 15)
    result.snapshot, result.fetched_at = parse_channel(html), fetched
    return result


def test_parse_nested_tags_br_two_blocks_and_newest_wins():
    channel = parse_channel(HTML)
    assert channel.name == "Світло тестової черги"
    post, block = channel.latest(TODAY, "2.2")
    assert post.number == 11
    assert block.kind == "сьогодні"
    assert block.intervals == (("00:00", "01:00"), ("21:00", "24:00"))
    _, tomorrow = channel.latest(date(2026, 11, 6), "2.2")
    assert tomorrow.kind == "завтра" and tomorrow.intervals == (("03:00", "05:00"),)
    # Selection is independent of DOM order, and queue equality is exact.
    assert replace(channel, posts=tuple(reversed(channel.posts))).latest(TODAY, "2.2") == (post, block)
    assert channel.latest(TODAY, "12.2") is None
    assert channel.latest(TODAY, "2.21")[0].number == 12


def test_date_in_text_is_authoritative_even_across_utc_midnight():
    channel = replace(parse_channel(HTML), posts=parse_channel(HTML).posts[:1])
    post, block = channel.latest(TODAY, "2.2")
    assert post.published.date() == date(2026, 11, 4)
    assert block.day == TODAY


@pytest.mark.parametrize("old,new", [("21:00 - 24:00", "21:00 - 24:01"),
                                     ("21:00 - 24:00", "25:00 - 26:00"),
                                     ("21:00 - 24:00", "21:60 - 24:00"),
                                     ("21:00 - 24:00", "21:00 - 20:00"),
                                     ("21:00 - 24:00", "21:00 - невідомо"),
                                     ("21:00 - 24:00", "00:30 - 02:00")])
def test_invalid_newer_block_never_resurrects_older_times(old, new):
    src = source(HTML.replace(old, new))
    assert src.snapshot.latest(TODAY, "2.2")[1].intervals is None
    answer = src.answer("2.2", NOW)
    assert "немає" in answer and "08:00" not in answer


@pytest.mark.parametrize("html", ["", "<html>challenge</html>", HTML.replace('datetime="2026-11-05T10:10:00+00:00"', 'datetime="bad"'),
                                  HTML.replace("05.11.2026", "31.11.2026"),
                                  HTML.replace("2026-11-05T10:10:00+00:00", "2026-11-05T10:10:00"),
                                  HTML.replace('datetime="2026-11-05T10:10:00+00:00"', 'datetime'),
                                  HTML.replace('data-post="test_channel/11"', 'data-post'),
                                  HTML[:HTML.rfind("</time>")],
                                  "x" * (outage.MAX_HTML_BYTES + 1)])
def test_invalid_page_rejected(html):
    with pytest.raises(ValueError):
        parse_channel(html)


def test_answer_safety_source_times_and_missing_today():
    answer = source().answer("2.2", NOW)
    assert "На сьогодні, 05.11.2026: 00:00–01:00, 21:00–24:00; оновлено о 12:10" in answer
    assert "На завтра, 06.11.2026: 03:00–05:00" in answer
    assert "Джерело: Світло тестової черги" in answer
    assert unsafe_reason(answer) is None
    assert "http" not in answer and "@" not in answer
    for src, queue, now in [(source(fetched=NOW - 7201), "2.2", NOW),
                            (source(), "12.2", NOW),
                            (source(HTML.replace("05.11.2026", "04.11.2026")), "2.2", NOW),
                            (OutageSource("test_channel", 15), "2.2", NOW),
                            (source(), "2.2", NOW + 3 * 86400)]:
        text = src.answer(queue, now)
        assert "немає" in text and not outage.CLOCK.search(text)
        assert unsafe_reason(text) is None
    assert "21:00–24:00" in source(fetched=NOW - 7200).answer("2.2", NOW)


def test_kyiv_midnight_changes_date_and_today_label():
    src = source()
    before = datetime(2026, 11, 5, 21, 59, tzinfo=timezone.utc).timestamp()
    src.fetched_at = before
    assert "На сьогодні, 05.11.2026" in src.answer("2.2", before)
    text = src.answer("2.2", before + 60)
    assert "На сьогодні, 06.11.2026: 03:00–05:00" in text
    assert "21:00–24:00" not in text
    assert unsafe_reason(text) is None


@pytest.mark.parametrize("empty", ["Відключень немає", "✅ Відключень за графіком не заплановано"])
def test_explicit_no_outages_is_valid_and_safe(empty):
    src = source(HTML.replace("21:00 - 24:00 (~3 год)", "").replace("00:00 - 01:00 (~1 год)", empty))
    text = src.answer("2.2", NOW)
    assert "відключень немає; оновлено" in text and unsafe_reason(text) is None


@pytest.mark.parametrize("failure", ["timeout", "status", "format", "encoding", "oversize"])
def test_poll_failures_backoff_preserve_cache_then_recover(monkeypatch, caplog, failure):
    monkeypatch.setattr(outage, "time", SimpleNamespace(time=lambda: NOW + 200))
    src = source()
    original = src.snapshot
    calls = []

    def respond(request):
        calls.append(request)
        if len(calls) > 4:
            return httpx.Response(200, text=HTML)
        if failure == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        return httpx.Response(503 if failure == "status" else 200, content={
            "format": b"<html>broken</html>", "encoding": b"\xff",
            "oversize": b"x" * (outage.MAX_HTML_BYTES + 1),
        }.get(failure, b"unavailable"))

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert [await src.poll_once(client) for _ in range(4)] == [1800, 3600, 3600, 3600]
            assert src.snapshot is original and src.fetched_at == NOW
            assert await src.poll_once(client) == 900

    asyncio.run(scenario())
    assert len(calls) == 5 and src.failures == 0 and src.fetched_at == NOW + 200
    assert "outage poll: failed" in caplog.text


def test_poller_lifecycle_deadline_identity_and_cancellation(monkeypatch):
    real_client = httpx.AsyncClient
    started = asyncio.Event()
    closed = []
    opts = []

    async def hang(request):
        assert request.url == "https://t.me/s/test_channel"
        assert request.headers["User-Agent"] == "DobrosusidBot outage schedule reader"
        started.set()
        await asyncio.Event().wait()

    class Transport(httpx.MockTransport):
        async def aclose(self):
            closed.append(True)
            await super().aclose()

    def client(**kwargs):
        opts.append(kwargs)
        return real_client(transport=Transport(hang), **kwargs)

    monkeypatch.setattr(outage.httpx, "AsyncClient", client)

    async def scenario():
        task = asyncio.create_task(OutageSource("test_channel", 15).run())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert opts[0]["timeout"] == 10 and opts[0]["follow_redirects"] is False
    assert closed == [True]


def test_outer_deadline_and_loop_backoff_delays(monkeypatch):
    real_timeout = asyncio.timeout
    real_client = httpx.AsyncClient
    deadlines, delays, calls = [], [], []

    def deadline(seconds):
        deadlines.append(seconds)
        return real_timeout(0)

    async def hang(request):
        calls.append(request)
        await asyncio.Event().wait()

    async def sleep(seconds):
        delays.append(seconds)
        if len(delays) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(outage.asyncio, "timeout", deadline)
    monkeypatch.setattr(outage.asyncio, "sleep", sleep)
    monkeypatch.setattr(outage.httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(hang), **kwargs))

    async def scenario():
        with pytest.raises(asyncio.CancelledError):
            await OutageSource("test_channel", 15).run()

    asyncio.run(scenario())
    assert deadlines == [15, 15] and delays == [1800, 3600] and len(calls) == 2


def test_env_defaults_and_values(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test")
    for key in ("OUTAGE_CHANNEL", "OUTAGE_QUEUE", "OUTAGE_POLL_MINUTES"):
        monkeypatch.delenv(key, raising=False)
    settings = Settings.from_env()
    assert (settings.outage_channel, settings.outage_queue, settings.outage_poll_minutes) == ("", "2.2", 15)
    monkeypatch.setenv("OUTAGE_CHANNEL", "test_channel")
    monkeypatch.setenv("OUTAGE_QUEUE", "1.1")
    monkeypatch.setenv("OUTAGE_POLL_MINUTES", "7.5")
    settings = Settings.from_env()
    assert (settings.outage_channel, settings.outage_queue, settings.outage_poll_minutes) == ("test_channel", "1.1", 7.5)


@pytest.mark.parametrize("field,value", [("outage_channel", "../bad"), ("outage_channel", "x?before=1"),
                                       ("outage_queue", "2.2: 10:00–12:00"), ("outage_queue", "2.2\n"),
                                       ("outage_poll_minutes", float("nan")), ("outage_poll_minutes", float("inf")),
                                       ("outage_poll_minutes", 1e308),
                                       ("outage_poll_minutes", 0), ("outage_poll_minutes", -1)])
def test_invalid_settings_rejected(field, value):
    with pytest.raises(ValueError):
        Settings(telegram_token="", owner_ids=frozenset(), allowed_chat_ids=frozenset(), **{field: value})
