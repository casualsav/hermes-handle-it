from support_chat_plugin import reader


def test_new_lines_are_found_and_labels_joined():
    before = ["Help", "Dana joined the chat.", "Dana", "Hi, how can I help?"]
    after = before + ["You", "I need a refund for order 1234", "Dana", "Sorry to hear that! Which order?"]
    new = reader.appeared(before, after)
    assert reader.incoming(new, ["I need a refund for order 1234"]) == ["Dana: Sorry to hear that! Which order?"]


def test_typing_and_end_markers():
    assert reader.typing_now(["Dana is typing…"])
    assert reader.ended(["Dana has left the chat."])
    assert reader.incoming(["Dana is typing…", "10:42 AM"], []) == []


def test_system_lines_keep_no_speaker():
    assert reader.incoming(["Dana · Northwind support", "Dana joined the chat."], []) == [
        "Dana · Northwind support", "Dana joined the chat."]
