from support_chat_plugin.guard import Guard


def make(**kw):
    kw.setdefault("allowed", {"order number": "1234", "name": "Sam Rivera"})
    kw.setdefault("task", "Get a full refund for order 1234, the jacket zipper broke")
    return Guard(**kw)


def kinds(findings):
    return [f.kind for f in findings]


def test_allowed_details_pass():
    assert make().check("It's order 1234, under Sam Rivera.") == []


def test_unlisted_email_and_number_need_the_user():
    found = make().check("My email is sam@example.com and account 99887766.")
    texts = [f.text for f in found]
    assert "sam@example.com" in texts
    assert any("99887766" in t for t in texts)


def test_number_support_said_first_is_fine():
    g = make(heard=["Your refund reference is RF-48213."])
    assert g.check("Thanks, I have noted RF-48213.") == []


def test_never_share_values_are_caught():
    g = make(private=["+1 415 555 0100"])
    assert kinds(g.check("Call me on +1 415 555 0100")) == ["detail"]


def test_yes_to_an_offer_is_a_commitment():
    g = make(heard=["I can do a full refund of $180. Shall I go ahead and process it?"])
    assert kinds(g.check("Yes, please go ahead.")) == ["commitment"]


def test_yes_to_a_plain_question_is_not():
    g = make(heard=["Thanks! For security, can you confirm the full name on the order?"])
    assert g.check("Sure, it's Sam Rivera.") == []


def test_declining_an_offer_needs_no_approval():
    g = make(heard=["I can offer a 20% partial refund ($36). Would that work for you?"])
    assert g.check("No, I'd rather have a full refund, the jacket is unusable.") == []


def test_paying_always_needs_the_user():
    assert kinds(make().check("I'll pay the $15 restocking fee.")) == ["commitment"]


def test_declared_agree_needs_the_user_even_without_keywords():
    assert kinds(make().check("Let's do the store credit.", "agree")) == ["commitment"]


def test_unlisted_answer_to_a_request_goes_to_the_user():
    g = make(heard=["Could you share your date of birth to verify?"])
    assert kinds(g.check("It's the 3rd of March")) == ["detail"]


def test_card_numbers_are_never_typed():
    assert [f.kind for f in make().hard_block("Card 4111 1111 1111 1111")] == ["blocked"]
    assert make().hard_block("Order 1234") == []


def test_closing_question_is_not_an_offer():
    from support_chat_plugin.guard import is_offer

    assert not is_offer("Done! Refund RF-48213 for $180 is processed. Anything else I can help with?")
    assert is_offer("I can offer a 20% partial refund ($36). Would that work for you?")
    assert is_offer("My supervisor approved a full refund of $180. Shall I go ahead and process it?")
    g = make(heard=["Done! Refund RF-48213 for $180 is processed. Anything else I can help with?"])
    assert g.check("Yes, that's all, thanks!") == []
