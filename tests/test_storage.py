from dobrosusid.storage import KEEP_MESSAGES_SECONDS, Storage


def make_store(tmp_path):
    return Storage(str(tmp_path / "test.sqlite3"))


def test_new_messages_skip_bot_and_analyzed(tmp_path):
    store = make_store(tmp_path)
    store.upsert_chat(-1, "Будинок", 42)
    store.add_message(-1, 10, 1, "Іван", "привіт", None, 100.0)
    store.add_message(-1, 11, 99, "Бот", "вітаю", None, 101.0, is_bot=True)
    store.add_message(-1, 12, 2, "Олена", "добрий вечір", 10, 102.0)
    store.set_last_analyzed(-1, 10)

    new = store.messages_after(-1, store.get_chat(-1).last_analyzed_id)
    assert [m.message_id for m in new] == [12]
    assert [m.message_id for m in store.recent_messages(-1, 2)] == [11, 12]


def test_last_analyzed_never_goes_back(tmp_path):
    store = make_store(tmp_path)
    store.upsert_chat(-1, None, None)
    store.set_last_analyzed(-1, 20)
    store.set_last_analyzed(-1, 5)
    assert store.get_chat(-1).last_analyzed_id == 20


def test_upsert_keeps_original_adder(tmp_path):
    store = make_store(tmp_path)
    store.upsert_chat(-1, "A", 42)
    store.upsert_chat(-1, "B", None)
    chat = store.get_chat(-1)
    assert chat.added_by == 42 and chat.title == "B"


def test_residents_and_reminders(tmp_path):
    store = make_store(tmp_path)
    store.set_resident(-1, 1, "Іван", "12")
    store.set_resident(-1, 1, "Іван Петрович", "12")
    assert store.residents(-1)[1].name == "Іван Петрович"
    assert store.forget_resident(-1, 1)
    assert not store.forget_resident(-1, 1)

    assert not store.was_reminded(-1, 2)
    store.mark_reminded(-1, [2])
    assert store.was_reminded(-1, 2)


def test_migrate_moves_residents_and_drops_old_context(tmp_path):
    store = make_store(tmp_path)
    store.upsert_chat(-1, "Група", 42)
    store.set_last_analyzed(-1, 50)
    store.set_resident(-1, 1, "Іван", "12")
    store.add_message(-1, 50, 1, "Іван", "текст", None, 100.0)

    store.migrate_chat(-1, -100)

    assert store.get_chat(-1) is None
    chat = store.get_chat(-100)
    assert chat.added_by == 42 and chat.last_analyzed_id == 0
    assert 1 in store.residents(-100)
    assert store.recent_messages(-100, 10) == []


def test_prune_and_events(tmp_path):
    store = make_store(tmp_path)
    now = 10 * KEEP_MESSAGES_SECONDS
    store.add_message(-1, 1, 1, "Іван", "старе", None, now - KEEP_MESSAGES_SECONDS - 1)
    store.add_message(-1, 2, 1, "Іван", "нове", None, now)
    store.add_event(-1, "llm", now - 10)
    store.add_event(-2, "llm", now - 5)
    store.prune_messages(now)

    assert [m.message_id for m in store.recent_messages(-1, 10)] == [2]
    assert store.count_events("llm", now - 60) == 2
    assert store.count_events("llm", now - 60, chat_id=-1) == 1
    assert store.last_event(-2, "llm") == now - 5
    assert store.last_event(-2, "intervention") is None
