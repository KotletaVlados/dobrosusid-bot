import asyncio

import pytest

from dobrosusid.bot import apartment_key, parse_iam
from dobrosusid.scheduler import Debouncer


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Олена, 45", ("Олена", "45")),
        ("Олена кв. 45", ("Олена", "45")),
        ("Іван Петрович, квартира 12а", ("Іван Петрович", "12а")),
        ("Ольга №7", ("Ольга", "7")),
        ("  Марія   101 ", ("Марія", "101")),
    ],
)
def test_parse_iam(raw, expected):
    assert parse_iam(raw) == expected


@pytest.mark.parametrize("raw", ["", "Олена", "45", "Олена сорок п'ять"])
def test_parse_iam_rejects_incomplete(raw):
    assert parse_iam(raw) is None


def test_apartment_sorting():
    apartments = ["101", "12а", "9", "сорок"]
    assert sorted(apartments, key=apartment_key) == ["9", "12а", "101", "сорок"]


def test_debouncer_batches_and_respects_max_wait():
    calls = []

    async def run(key):
        calls.append((key, asyncio.get_running_loop().time()))

    async def scenario():
        debouncer = Debouncer(run, debounce=0.05, max_wait=0.12)
        start = asyncio.get_running_loop().time()
        for _ in range(6):  # повідомлення що 30 мс — тиша так і не настає
            debouncer.poke(1)
            await asyncio.sleep(0.03)
        await asyncio.sleep(0.15)
        return start

    start = asyncio.run(scenario())
    assert len(calls) == 2  # спрацювало за max_wait, потім ще раз після тиші
    assert calls[0][1] - start == pytest.approx(0.12, abs=0.04)


def test_debouncer_urgent_fires_fast():
    calls = []

    async def run(key):
        calls.append(key)

    async def scenario():
        debouncer = Debouncer(run, debounce=10, max_wait=10, urgent_delay=0.01)
        debouncer.poke(7)
        debouncer.poke(7, urgent=True)
        debouncer.poke(7)  # звичайне повідомлення слідом не скасовує терміновість
        await asyncio.sleep(0.05)

    asyncio.run(scenario())
    assert calls == [7]
