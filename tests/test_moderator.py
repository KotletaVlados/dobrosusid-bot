from zoneinfo import ZoneInfo

import pytest

from dobrosusid.moderator import Decision, build_user_prompt, render_transcript, should_intervene
from dobrosusid.storage import Resident, StoredMessage

KYIV = ZoneInfo("Europe/Kyiv")
NOW = 1_800_000_000.0


def msg(message_id, user_id, author, text, *, reply_to=None, is_bot=False):
    return StoredMessage(message_id, user_id, author, text, reply_to, NOW, is_bot, False)


def decision(tension="conflict", action="reply", text="Сусіди, давайте спокійніше 🙂", emergency=False):
    return Decision(
        tension=tension, emergency=emergency, action=action, reply_to_message_id=None, text=text, introductions=[]
    )


def test_transcript_splits_old_and_new_and_tags_residents():
    messages = [
        msg(10, 1, "Іван", "хто знову кинув сміття?"),
        msg(11, 99, "Добросусід", "давайте спокійніше", is_bot=True),
        msg(12, 2, "Ольга К.", "не я!", reply_to=10),
    ]
    text = render_transcript(messages, {1: Resident(1, "Іван", "12")}, last_analyzed_id=10, tz=KYIV)

    old, new = text.split("НОВІ:")
    assert "#10" in old and "Іван [Іван, кв. 12] (id 1)" in old
    assert "#11" in old and "БОТ" in old  # власні повідомлення бота — лише контекст
    assert "#12" in new and "Ольга К. [не представився] (id 2) ↩#10: не я!" in new


def test_long_message_is_marked_as_cut():
    text = render_transcript([msg(1, 1, "Іван", "а" * 5000)], {}, last_analyzed_id=0, tz=KYIV)
    assert text.endswith("…[обрізано]")


def test_user_prompt_mentions_direct_address():
    assert "#12, #14" in build_user_prompt("...", [12, 14])
    assert "напряму" not in build_user_prompt("...", [])


POLICY = dict(now=NOW, cooldown_minutes=10, max_per_day=15)


@pytest.mark.parametrize("tension", ["calm", "tense"])
def test_no_unsolicited_reply_without_conflict(tension):
    assert not should_intervene(
        decision(tension), addressed=False, last_intervention_ts=None, interventions_today=0, **POLICY
    )


def test_conflict_respects_cooldown_and_daily_cap():
    d = decision("conflict")
    assert should_intervene(d, addressed=False, last_intervention_ts=None, interventions_today=0, **POLICY)
    assert not should_intervene(d, addressed=False, last_intervention_ts=NOW - 300, interventions_today=1, **POLICY)
    assert should_intervene(d, addressed=False, last_intervention_ts=NOW - 601, interventions_today=1, **POLICY)
    assert not should_intervene(d, addressed=False, last_intervention_ts=None, interventions_today=15, **POLICY)


def test_hostile_halves_cooldown():
    d = decision("hostile")
    assert should_intervene(d, addressed=False, last_intervention_ts=NOW - 301, interventions_today=1, **POLICY)


def test_emergency_bypasses_tension_cooldown_and_cap():
    assert should_intervene(
        decision("calm", emergency=True),
        addressed=False, last_intervention_ts=NOW, interventions_today=99, **POLICY,
    )


def test_direct_address_always_answered_but_empty_text_never_sent():
    assert should_intervene(
        decision("calm"), addressed=True, last_intervention_ts=NOW, interventions_today=99, **POLICY
    )
    assert not should_intervene(
        decision("hostile", text="  "), addressed=True, last_intervention_ts=None, interventions_today=0, **POLICY
    )
    assert not should_intervene(
        decision("hostile", action="none"), addressed=False, last_intervention_ts=None, interventions_today=0, **POLICY
    )
