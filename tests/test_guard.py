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


def test_offer_stated_without_a_question_is_caught():
    from support_chat_plugin.guard import is_offer

    assert is_offer("I can offer a $36 store credit.")
    g = make(heard=["I can offer a $36 store credit."])
    assert kinds(g.check("Yes, that works")) == ["commitment"]
    assert kinds(g.check("Yes, that works, no problem.")) == ["commitment"]
    assert kinds(g.check("The store credit is fine.")) == ["commitment"]
    assert g.check("No, I'd rather have a full refund.") == []
    assert g.check("Could you make it a refund to my card instead?") == []


def test_offer_followed_by_another_bubble_is_still_open():
    g = make(heard=["I can offer a $36 store credit.", "Let me know!"])
    assert kinds(g.check("Yes, that works")) == ["commitment"]


# --- G1: text in other scripts and widths is folded before it is checked ---------------------

def test_fullwidth_digits_are_a_number():
    found = make().check("my account is １２３４５６７８９０")
    assert kinds(found) == ["detail"] and found[0].text == "１２３４５６７８９０"


def test_never_share_values_in_any_script_or_accent():
    assert kinds(make(private=["Иванов"]).check("Иванов")) == ["detail"]
    assert kinds(make(private=["José García"]).check("It's under Jose Garcia")) == ["detail"]
    assert kinds(make(private=["Jose Garcia"]).check("It's under José García")) == ["detail"]


def test_fullwidth_commitment_words_are_read():
    assert kinds(make().check("I'll ｐａｙ the fee.")) == ["commitment"]


# --- G2: card numbers with other separators ------------------------------------------------------

def test_card_numbers_with_other_separators_are_never_typed():
    g = make()
    for text in ("4111.1111.1111.1111", "4111  1111 1111 1111", "4111\n1111\n1111\n1111", "4111/1111/1111/1111",
                 "Order 1234\n4111 1111 1111 1111"):
        assert [f.kind for f in g.hard_block(text)] == ["blocked"], text


def test_a_number_that_fails_luhn_stays_an_ordinary_detail():
    g = make()
    assert g.hard_block("4111.1111.1111.1112") == []
    assert kinds(g.check("account 4111.1111.1111.1112")) == ["detail"]


# --- G3: grouped short numbers -----------------------------------------------------------------

def test_grouped_short_numbers_are_details():
    for text in ("acct 12 34 56 78", "call 555 01 42", "my code is 12 34 56", "ref 12.34.56"):
        assert kinds(make().check(text)) == ["detail"], text


def test_money_and_plain_prose_are_not_grouped_numbers():
    g = make()
    for text in ("The fee was $1,200.00 in total.", "It was 180.00 and I paid it already.", "Can I get 20% back?",
                 "Shipping takes 5–7 business days.", "I waited 2 weeks.", "It's order 1234 from 14 days ago.",
                 "It cost 1 180.00 last time."):
        assert [f for f in g.details(text)] == [], text


def test_grouped_number_support_said_first_is_fine():
    assert make(heard=["Your case number is 12 34 56 78."]).check("Thanks, case 12 34 56 78 noted.") == []


# --- G5: with an offer open, an answer that does not turn it down is agreeing -----------------

def test_any_answer_to_an_open_offer_that_does_not_turn_it_down_is_agreeing():
    g = make(heard=["I can offer a $36 store credit. Would that work?"])
    for text in ("Go for it", "Do it", "Please send it over", "Confirm", "Sí, acepto"):
        assert kinds(g.check(text)) == ["commitment"], text
    assert g.check("Could you tell me when it expires?") == []
    assert g.check("No thank you.") == []
    assert g.check("Let me check with my partner first.") == []
    assert g.check("Let me think.") == []


# --- G6: a consent question is an offer by itself ------------------------------------------------

def test_a_consent_question_is_an_offer():
    from support_chat_plugin.guard import is_offer

    assert is_offer("Is it ok to proceed with the free shipping label?")
    assert is_offer("May I proceed with the change?")
    assert is_offer("Shall we proceed?")
    assert is_offer("Do you confirm the new delivery address?")
    g = make(heard=["Is it ok to proceed with the free shipping label?"])
    assert kinds(g.check("Yes please")) == ["commitment"]
    for text in ("How can I help you today?", "Can you confirm your order number?",
                 "Is there anything else I can help with?"):
        assert not is_offer(text), text


# --- G7: paying, cancelling, closing: always the user's call ---------------------------------

def test_cancelling_and_closing_need_the_user():
    for text in ("Please cancel it", "cancel subscription", "close my account", "Confirm the order",
                 "Please terminate the contract.", "Downgrade me to basic.",
                 "I have no other questions, so please upgrade me"):
        assert kinds(make().check(text)) == ["commitment"], text


def test_a_refusal_in_the_same_clause_is_clean():
    for text in ("I don't want to upgrade", "No, I won't pay for that", "Please don't cancel my plan.",
                 "I can confirm the order number is 1234."):
        assert make().check(text) == [], text


def test_pay_findings_are_marked():
    found = make().check("Please cancel my plan.")
    assert found[0].pay is True and found[0].as_dict() == {
        "kind": "commitment", "text": "Please cancel my plan.", "why": "it pays, buys, cancels or authorises something"}
    g = make(heard=["I can offer a $36 store credit."])
    assert [f.pay for f in g.check("Yes, that works")] == [False]
