from support_chat_plugin import reader


def test_new_lines_are_found_and_labels_joined():
    before = ["Help", "Dana joined the chat.", "Dana", "Hi, how can I help?"]
    after = before + ["You", "I need a refund for order 1234", "Dana", "Sorry to hear that! Which order?"]
    new = reader.appeared(before, after)
    assert reader.incoming(new, ["I need a refund for order 1234"]) == ["Dana: Sorry to hear that! Which order?"]


def test_typing_and_end_markers():
    assert reader.typing_now(["Dana is typing…"])
    assert reader.ended(["Dana ended the chat."])
    assert reader.incoming(["Dana is typing…", "10:42 AM"], []) == []


def test_system_lines_keep_no_speaker():
    assert reader.incoming(["Dana · Northwind support", "Dana joined the chat."], []) == [
        "Dana · Northwind support", "Dana joined the chat."]


def test_own_echo_with_a_label_curly_quote_or_time_is_ours():
    sent = ["One moment please, I am checking...", "I'm sure it's order 1234."]
    assert reader.incoming(["You: One moment please, I am checking..."], sent) == []
    assert reader.incoming(["I’m sure it’s order 1234."], sent) == []
    assert reader.incoming(["10:42 AM I'm sure it's order 1234."], sent) == []
    assert reader.incoming(["Customer - I'm sure it's order 1234. 10:42"], sent) == []
    assert reader.incoming(["Dana: Sure, which order is it?"], sent) == ["Dana: Sure, which order is it?"]


def test_an_agent_leaving_is_not_the_end_of_the_chat():
    assert not reader.ended(["Dana has left the chat."])
    assert not reader.ended(["Dana left the conversation"])
    assert reader.agent_left(["Dana has left the chat."])
    assert not reader.agent_left(["Dana has left the chat.", "Sam joined the chat."])


def test_explicit_closings_end_the_chat_only_on_short_lines():
    for line in ("This conversation was closed", "The chat has ended.", "Chat closed", "Session has been closed",
                 "Dana ended the chat", "This chat is closed."):
        assert reader.ended([line]), line
    long = ("Dana: I can see your previous chat was closed before we could finish, so I have reopened the case "
            "and added the notes.")
    assert not reader.ended([long])


def test_typing_only_when_the_line_is_an_indicator():
    for line in ("Dana is typing…", "Support is typing...", "Dana is writing a message", "typing…", "Composing…",
                 "Dana and Sam are typing"):
        assert reader.typing_now([line]), line
    message = "The system is writing it to your account"
    assert not reader.typing_now([message])
    assert reader.incoming([message], []) == [message]


def test_delivery_receipts_are_dropped():
    assert reader.incoming(["Seen", "Delivered", "Read", "Sent", "Delivered · 10:42 AM", "Seen just now"], []) == []
