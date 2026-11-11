"""The chat loop against a fake page and a fake clock: replies arrive on a schedule."""

import json

from support_chat_plugin.session import ACCEPT_OFFER, APPROVE, DECLINE_OFFER, DENY, HOLD, ChatSession


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
