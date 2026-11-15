"""The chat loop against a fake page and a fake clock: replies arrive on a schedule."""

import json

from support_chat_plugin.session import ACCEPT_OFFER, APPROVE, DECLINE_OFFER, DENY, FOLLOW_UP, HOLD, STOP, ChatSession


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakePage:
    """A chat page: ``replies`` maps something we type to what support answers, after a delay."""

    def __init__(self, clock, replies, greeting="Hi, I'm Dana. How can I help?"):
        self.clock = clock
        self.lines = ["Northwind Help", "Live chat"]
        self.pending = [(3.0, ["Dana", greeting])]
        self.replies = replies
        self.sent = []
        self.closed = False

    def text(self):
        due = [p for p in self.pending if p[0] <= self.clock.t]
        for p in due:
            self.pending.remove(p)
            self.lines += p[1]
        typing = ["Dana is typing…"] if any(p[0] - self.clock.t < 2 for p in self.pending) else []
        return "\n".join(self.lines + typing)

    def send(self, message):
        self.sent.append(message)
        self.lines += ["You", message]
        for key, (delay, answer) in self.replies.items():
            if key in message:
                self.pending.append((self.clock.t + delay, ["Dana", answer]))
                break

    def screenshot(self, path):
        return None

    def close(self):
        self.closed = True


def session(tmp_path, replies, **kw):
    clock = Clock()
    page = FakePage(clock, replies)
    s = ChatSession(page, "http://shop.test/help", "Full refund for order 1234",
                    {"order number": "1234"}, [], tmp_path / "chat", clock=clock, sleep=clock.sleep, **kw)
    return s, page, clock


def test_waits_for_a_slow_reply_and_reports_progress(tmp_path):
    s, page, clock = session(tmp_path, {"1234": (200.0, "Found it. I can offer a partial refund of $36.")})
    seen = []
    s.progress = seen.append
    assert s.wait_for_reply()["support_said"] == ["Dana: Hi, I'm Dana. How can I help?"]
    out = s.send("Hi, I'd like a refund for order 1234, the zipper broke.", ask=None)
    assert out["status"] == "replied"
    assert out["support_said"] == ["Dana: Found it. I can offer a partial refund of $36."]
    assert out["waited_s"] >= 200
    assert any(m.startswith("Still holding") for m in seen)
    assert "Support is typing…" in seen


def test_no_time_cap_a_wait_that_runs_out_says_still_waiting(tmp_path):
    s, page, clock = session(tmp_path, {"1234": (1000.0, "Sorry for the wait!")})
    s.wait_for_reply()
    out = s.send("Order 1234 please", ask=None, max_wait_s=240)
    assert out["status"] == "still_waiting"
    again = [s.wait_for_reply(max_wait_s=300) for _ in range(3)]
    assert again[-1]["support_said"] == ["Dana: Sorry for the wait!"]


def test_agreeing_waits_for_the_user_and_holds_the_agent(tmp_path):
    s, page, clock = session(tmp_path, {"Yes, please": (5.0, "Done! Refund RF-48213 is processed."),
                                        "checking": (2.0, "Sure, take your time.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can do a full refund of $180. Shall I go ahead?"]
    s.wait_for_reply()
    asked = []

    def ask(question, choices):
        asked.append(question)
        clock.t += 30  # the user takes a while
        return ACCEPT_OFFER

    out = s.send("Yes, please go ahead with the full refund.", intent="agree", ask=ask)
    assert len(asked) == 1 and "Shall I go ahead?" in asked[0] and "Accept it?" in asked[0]
    assert page.sent == [HOLD, "Yes, please go ahead with the full refund."]
    assert out["approved_by_user"] is True
    assert out["support_said"] == ["Dana: Done! Refund RF-48213 is processed."]
    assert "Dana: Sure, take your time." in [t.text for t in s.turns]


def test_a_no_sends_nothing(tmp_path):
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    page.lines += ["Dana", "I can offer a $20 credit instead. Would that work?"]
    s.wait_for_reply()
    out = s.send("Yes, that works.", ask=lambda q, c: DECLINE_OFFER)
    assert out["status"] == "user_declined_offer"
    assert "Yes, that works." not in page.sent


def test_silence_accepts_nothing(tmp_path):
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    page.lines += ["Dana", "Shall I process the $36 partial refund?"]
    s.wait_for_reply()
    assert s.send("Yes please.", ask=lambda q, c: None)["status"] == "no_answer"
    assert s.send("Yes please.", ask=None)["status"] == "needs_user"
    assert "Yes please." not in page.sent


def test_a_chat_survives_a_restart(tmp_path):
    s, page, clock = session(tmp_path, {"1234": (5.0, "Thanks, looking now.")})
    s.wait_for_reply()
    s.send("Order 1234", ask=None)
    saved = json.loads((tmp_path / "chat" / "session.json").read_text())
    assert saved["sent"] == ["Order 1234"]
    page.lines += ["Dana", "Found it: Ridgeline jacket."]  # said while Hermes was down
    back = ChatSession.restore(tmp_path / "chat", page, clock=clock, sleep=clock.sleep)
    assert back.id == s.id and [t.text for t in back.turns] == [t.text for t in s.turns]
    assert back.wait_for_reply()["support_said"] == ["Dana: Found it: Ridgeline jacket."]


def test_the_transcript_is_saved(tmp_path):
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    out = s.finish("Nothing yet", resolved=False)
    text = (tmp_path / "chat" / "transcript.md").read_text()
    assert "Hi, I'm Dana" in text and out["resolved"] is False and page.closed


def test_hermes_approval_gate_path(tmp_path):
    """The pre_tool_call hook asks Hermes's approval gate; the tool runs only after a yes."""
    s, page, clock = session(tmp_path, {"Yes, please": (5.0, "Done! Refund RF-48213 is processed.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can do a full refund of $180. Shall I go ahead?"]
    s.wait_for_reply()
    action, question = s.before_approval("Yes, please go ahead.", "agree")
    assert action == "ask" and "Shall I go ahead?" in question
    assert page.sent == [HOLD]
    assert s.before_approval("Thanks, one more question about shipping.", "ask") == ("", "")
    s.before_approval("Yes, please go ahead.", "agree")
    out = s.send("Yes, please go ahead.", intent="agree", ask=None)
    assert out["approved_by_user"] is True and page.sent == [HOLD, "Yes, please go ahead."]
    assert out["support_said"] == ["Dana: Done! Refund RF-48213 is processed."]


def test_card_numbers_are_blocked_before_the_gate(tmp_path):
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    action, why = s.before_approval("My card is 4111 1111 1111 1111", "pay")
    assert action == "block" and page.sent == []


def test_every_offer_is_the_users_call_even_when_the_model_would_decline(tmp_path):
    s, page, clock = session(tmp_path, {"full refund": (5.0, "Let me check with my supervisor.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can offer a 20% partial refund ($36). Would that work for you?"]
    s.wait_for_reply()
    asked = []
    out = s.send("No thanks, I'd like a full refund.", intent="decline",
                 ask=lambda q, c: asked.append(q) or DECLINE_OFFER)
    assert len(asked) == 1 and "partial refund" in asked[0]
    assert out["user_declined_offer"] is True and page.sent[-1] == "No thanks, I'd like a full refund."


def test_accepting_an_offer_the_model_wanted_to_decline(tmp_path):
    s, page, clock = session(tmp_path, {"I'll take": (5.0, "Done, $36 refunded.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can offer a 20% partial refund ($36). Would that work for you?"]
    s.wait_for_reply()
    asked = []
    ask = lambda q, c: asked.append(q) or ACCEPT_OFFER
    assert s.send("No thanks, I'd like a full refund.", ask=ask)["status"] == "user_accepts_offer"
    out = s.send("Yes, I'll take the $36 partial refund.", intent="agree", ask=ask)
    assert len(asked) == 1 and out["support_said"] == ["Dana: Done, $36 refunded."]


def test_a_question_back_does_not_answer_the_offer(tmp_path):
    s, page, clock = session(tmp_path, {"label": (3.0, "Yes, it's prepaid.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can do a full refund of $180. Shall I go ahead?"]
    s.wait_for_reply()
    out = s.send("Is the return label prepaid?", ask=lambda q, c: 1 / 0)
    assert out["support_said"] == ["Dana: Yes, it's prepaid."]


def test_statement_offer_and_a_plain_yes_wait_for_the_user(tmp_path):
    """Support states an offer with no question and the model, marking it only 'inform', says yes:
    the guard alone stops it for the user's approval."""
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    page.lines += ["Dana", "I can offer a $36 store credit."]
    s.wait_for_reply()
    action, question = s.before_approval("Yes, that works", "inform")
    assert action == "ask" and "$36 store credit" in question
    assert page.sent == [HOLD]


def test_statement_offer_asks_the_user_on_a_direct_call(tmp_path):
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    page.lines += ["Dana", "I can offer a $36 store credit."]
    s.wait_for_reply()
    asked = []
    out = s.send("Yes, that works", intent="inform", ask=lambda q, choices: asked.append(q) or DECLINE_OFFER)
    assert asked and "$36 store credit" in asked[0]
    assert "Yes, that works" not in page.sent and out["status"] == "user_declined_offer"


def _offer_on_the_gate(tmp_path):
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    page.lines += ["Dana", "I can offer a 20% partial refund ($36). Would that work for you?"]
    s.wait_for_reply()
    assert s.before_approval("Yes, I'll take the $36 partial refund.", "agree")[0] == "ask"
    return s, page


def test_a_declined_card_is_asked_again_not_sent(tmp_path):
    s, page = _offer_on_the_gate(tmp_path)
    s.approval_refused("Yes, I'll take the $36 partial refund.", "BLOCKED: Action denied by user.")
    out = s.send("Yes, I'll take the $36 partial refund.", intent="agree", ask=None)
    assert out["status"] == "needs_user"
    assert "Yes, I'll take the $36 partial refund." not in page.sent and not s.closed


def test_an_unanswered_card_tells_support_and_ends_with_nothing_agreed(tmp_path):
    s, page = _offer_on_the_gate(tmp_path)
    s.approval_refused("Yes, I'll take the $36 partial refund.",
                       "BLOCKED: Action timed out without user response. Do not retry.")
    assert page.sent == [HOLD, FOLLOW_UP] and s.closed and page.closed
    assert s.pending is None and s.approvals[-1]["answer"] is None
    assert json.loads((tmp_path / "chat" / "session.json").read_text())["closed"] is True


def test_a_block_for_another_message_changes_nothing(tmp_path):
    s, page = _offer_on_the_gate(tmp_path)
    s.approval_refused("Something else.", "BLOCKED: Action timed out without user response.")
    assert s.pending == "Yes, I'll take the $36 partial refund." and not s.closed


def test_the_post_tool_call_hook_closes_a_chat_nobody_answered(tmp_path):
    from support_chat_plugin import handlers

    s, page = _offer_on_the_gate(tmp_path)
    handlers._sessions[s.id] = s
    handlers.post_tool_call("support_chat_send", {"session_id": s.id, "message": "Yes, I'll take the $36 partial refund."},
                            status="blocked", error_message="BLOCKED: Action timed out without user response.")
    assert s.id not in handlers._sessions and s.closed and page.sent[-1] == FOLLOW_UP


def test_a_block_that_is_not_a_timeout_does_not_end_the_chat(tmp_path):
    s, page = _offer_on_the_gate(tmp_path)
    s.approval_refused("Yes, I'll take the $36 partial refund.", "BLOCKED: Failed to send approval request to user.")
    assert s.pending is None and not s.closed and page.sent == [HOLD]


# --- G4: a question back keeps the offer open ------------------------------------------------

def test_an_offer_stays_open_across_a_question_back(tmp_path):
    s, page, clock = session(tmp_path, {"expiry": (3.0, "It expires in 30 days.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can offer a $36 credit. Would that work?"]
    s.wait_for_reply()
    out = s.send("Could you tell me the expiry?", ask=lambda q, c: 1 / 0)
    assert out["support_said"] == ["Dana: It expires in 30 days."]
    assert s.open_offer() == "Dana: I can offer a $36 credit. Would that work?"
    assert [f.kind for f in s.guard.commitments("Yes, go ahead")] == ["commitment"]
    asked = []
    out = s.send("Yes, go ahead", ask=lambda q, c: asked.append(q) or DECLINE_OFFER)
    assert asked and "$36 credit" in asked[0] and "Yes, go ahead" not in page.sent


# --- G8: accepting an offer covers that offer only, once ------------------------------------------

def test_an_accepted_offer_does_not_cover_paying_for_something_else(tmp_path):
    s, page, clock = session(tmp_path, {"take the credit": (3.0, "Done, the credit is on your account.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can offer a $36 store credit. Would that work?"]
    s.wait_for_reply()
    asked = []

    def ask(q, choices):
        asked.append(choices)
        return ACCEPT_OFFER if ACCEPT_OFFER in choices else DENY

    assert s.send("No thanks, not today.", ask=ask)["status"] == "user_accepts_offer"
    extra = "Yes, and also upgrade me to premium for $99, cancel my old plan."
    assert s.send(extra, intent="agree", ask=ask)["status"] == "user_declined"
    assert extra not in page.sent and asked[-1] == [APPROVE, DENY, STOP]
    out = s.send("Yes, I'll take the credit.", intent="agree", ask=ask)
    assert out["sent"] == "Yes, I'll take the credit." and s.decided == {}
    page.lines += ["Dana", "I can offer a $36 store credit. Would that work?"]  # the same words again, later
    s.wait_for_reply()
    before = len(asked)
    s.send("Yes, please.", intent="agree", ask=lambda q, c: asked.append(c) or DECLINE_OFFER)
    assert len(asked) == before + 1 and "Yes, please." not in page.sent


def test_an_accepted_paid_offer_lets_its_own_payment_through(tmp_path):
    s, page, clock = session(tmp_path, {"upgrade me": (3.0, "Done, you are on premium.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can upgrade you to premium for $10 a month. Would that work?"]
    s.wait_for_reply()
    ask = lambda q, c: ACCEPT_OFFER if ACCEPT_OFFER in c else 1 / 0
    assert s.send("No thanks, not today.", ask=ask)["status"] == "user_accepts_offer"
    out = s.send("Yes, please upgrade me.", intent="agree", ask=ask)
    assert out["sent"] == "Yes, please upgrade me."


# --- S1: a pending approval does not survive a restart ----------------------------------------

def test_a_pending_approval_is_not_restored(tmp_path):
    s, page = _offer_on_the_gate(tmp_path)
    assert "pending" not in json.loads((tmp_path / "chat" / "session.json").read_text())
    back = ChatSession.restore(tmp_path / "chat", page, clock=s._clock, sleep=s._sleep)
    assert back.pending is None
    out = back.send("Yes, I'll take the $36 partial refund.", intent="agree", ask=None)
    assert out["status"] == "needs_user" and "Yes, I'll take the $36 partial refund." not in page.sent


# --- S2: failures to type the hold or follow-up line are said, not swallowed --------------------

class FailingPage(FakePage):
    def __init__(self, *a, fail=(), **kw):
        super().__init__(*a, **kw)
        self.fail = set(fail)

    def send(self, message):
        if message in self.fail:
            raise RuntimeError("composer gone")
        super().send(message)


def _failing_session(tmp_path, fail):
    clock = Clock()
    page = FailingPage(clock, {}, fail=fail)
    s = ChatSession(page, "http://shop.test/help", "Full refund for order 1234", {"order number": "1234"}, [],
                    tmp_path / "chat", clock=clock, sleep=clock.sleep)
    s.wait_for_reply()
    return s, page


def test_a_hold_line_that_cannot_be_typed_is_noted_and_the_user_is_still_asked(tmp_path):
    s, page = _failing_session(tmp_path, {HOLD})
    asked = []
    out = s.send("My email is sam@example.com", ask=lambda q, c: asked.append(q) or DENY)
    assert asked and out["status"] == "user_declined"
    assert any(t.who == "note" and "could not be told to hold on" in t.text for t in s.turns)


def test_the_hold_line_is_never_typed_twice_in_a_row(tmp_path):
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    s.send("My email is sam@example.com", ask=lambda q, c: DENY)
    s.send("My phone is +1 415 555 0100", ask=lambda q, c: DENY)
    assert page.sent == [HOLD]


def test_a_follow_up_that_cannot_be_typed_is_not_claimed(tmp_path):
    s, page = _failing_session(tmp_path, {FOLLOW_UP})
    page.lines += ["Dana", "I can offer a 20% partial refund ($36). Would that work for you?"]
    s.wait_for_reply()
    assert s.before_approval("Yes, I'll take the $36 partial refund.", "agree")[0] == "ask"
    s.approval_refused("Yes, I'll take the $36 partial refund.", "BLOCKED: Action timed out without user response.")
    text = (tmp_path / "chat" / "transcript.md").read_text()
    assert s.closed and "could not be told" in text and "Support was told" not in text


# --- S3: only a timed-out approval counts as nobody answering ------------------------------------

def test_a_hook_timeout_is_a_block_not_a_missing_answer(tmp_path):
    s, page = _offer_on_the_gate(tmp_path)
    outcome = s.approval_refused("Yes, I'll take the $36 partial refund.",
                                 "pre_tool_call plugin callback timed out or is still running")
    assert outcome == "blocked" and not s.closed and page.sent == [HOLD]


def test_a_user_denial_is_declined(tmp_path):
    s, page = _offer_on_the_gate(tmp_path)
    assert s.approval_refused("Yes, I'll take the $36 partial refund.", "BLOCKED: User denied this action.") == "declined"


# --- R2: an agent leaving is a hint, not the end -----------------------------------------------

def test_an_agent_leaving_is_reported_with_a_hint(tmp_path):
    s, page, clock = session(tmp_path, {})
    s.wait_for_reply()
    page.lines += ["Dana has left the chat."]
    out = s.wait_for_reply()
    assert out["status"] == "replied" and not s.ended
    assert "support_chat_end" in out["hint"]
    page.lines += ["Sam joined the chat.", "Sam", "Hi, I'm taking over from Dana."]
    assert s.wait_for_reply()["status"] == "replied" and not s.ended


def test_a_reply_that_needed_an_ok_asks_the_agent_to_report_it(tmp_path):
    """Hermes's gate can answer yes with nobody asked (yolo), so the result itself says to report back."""
    s, page, clock = session(tmp_path, {"Yes, please": (5.0, "Done! Refund RF-48213 is processed.")})
    s.wait_for_reply()
    page.lines += ["Dana", "I can do a full refund of $180. Shall I go ahead?"]
    s.wait_for_reply()
    s.before_approval("Yes, please go ahead.", "agree")
    out = s.send("Yes, please go ahead.", intent="agree", ask=None)
    assert "Yes, please go ahead." in out["tell_user"]
