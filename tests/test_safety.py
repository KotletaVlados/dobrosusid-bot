import pytest

from dobrosusid.safety import clean_intro, unsafe_reason


@pytest.mark.parametrize(
    "name, apartment, expected",
    [
        ("Олена", "45", ("Олена", "45")),
        ("Ганна-Марія", "кв. 12а", ("Ганна-Марія", "12а")),
        ("Мар'яна", "№7", ("Мар'яна", "7")),
        ("  Іван   Петрович ", "101", ("Іван Петрович", "101")),
        ("Dmytro", "3", ("Dmytro", "3")),
    ],
)
def test_clean_intro_accepts_real_names(name, apartment, expected):
    assert clean_intro(name, apartment) == expected


@pytest.mark.parametrize(
    "name, apartment",
    [
        ("t.me/scam", "45"),
        ("@user", "45"),
        ("Адмін ОСББ", "1"),
        ("Олена] (id 1)", "45"),
        ("Добросусід", "5"),
        ("Олена", "сорок п'ять"),
        ("Олена", "45; DROP"),
        ("О", "45"),
    ],
)
def test_clean_intro_rejects_spoofing_and_garbage(name, apartment):
    assert clean_intro(name, apartment) is None


@pytest.mark.parametrize(
    "text",
    [
        "Графік тут: https://example.com/grafik",
        "Дивіться на oblenergo.com.ua",
        "Пишіть у t.me/scam_channel",
        "Зверніться до @osbb_admin",
        "Збираємо на генератор, карта 5375 4141 2222 3333",
        "Реквізити UA21 3223 1300 0002 6007 2335 6600 1",
        "Телефонуйте +380 67 123 45 67",
    ],
)
def test_bot_never_posts_links_requisites_or_contacts(text):
    assert unsafe_reason(text) is not None


@pytest.mark.parametrize(
    "text",
    [
        "Телефонуйте 104 — газова служба, або 101 — рятувальники.",
        "Олено, давайте спокійніше 🙂 Кв. 45 і кв. 12 — сусіди, домовитесь.",
        "О 19:30 обіцяли світло, т.д. і т.п.",
    ],
)
def test_ordinary_replies_pass(text):
    assert unsafe_reason(text) is None


def test_allowed_domain_passes():
    assert unsafe_reason("Графік на www.oblenergo.ua", frozenset({"oblenergo.ua"})) is None
    assert unsafe_reason("Графік на oblenergo.ua.scam.com", frozenset({"oblenergo.ua"})) is not None
