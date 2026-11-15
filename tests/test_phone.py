"""A support phone call against the bland.ai stand-in: one ask places the call, each offer made on
the call comes back to the user before the agent says yes, and the outcome is saved."""

import importlib.util
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from support_chat_plugin import handlers, phone_tools
from support_chat_plugin.bland import Bland, BlandError
from support_chat_plugin.phone import NO, YES, CallSession
from support_chat_plugin.session import Turn

_spec = importlib.util.spec_from_file_location("bland_standin", Path(__file__).parents[1] / "demo" / "bland_standin.py")
standin_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(standin_mod)


class User:
    """The user at Hermes's question prompt: answers each question in turn."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = []

    def clarify_callback(self, questions):
        self.asked.append(questions[0]["question"])
        return {"answers": {"q0": self.answers.pop(0) if self.answers else None}}


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def standin(tmp_path, monkeypatch, make_ctx):
    s = standin_mod.StandIn()
    port = _free_port()
    monkeypatch.setattr(handlers, "_ctx", make_ctx({"call_bridge_url": f"http://127.0.0.1:{port}",
                                                    "call_bridge_port": port}))
    monkeypatch.setattr(handlers, "_approvals_off", lambda: False)
    monkeypatch.setattr(phone_tools, "_approved", {})
    monkeypatch.setattr(phone_tools, "_client", Bland(api_key="test-key", base_url=s.url))
    handlers.configure(tmp_path)
    yield s
    phone_tools.shutdown()
    s.close()
    handlers.configure(None)


ARGS = {"phone_number": "+1 (800) 555-0142", "company": "Northwind Outfitters",
        "task": "Full refund for order 1234, the jacket zipper broke after two weeks.",
        "shareable": {"order number": "1234", "name": "Sam Rivera"}, "never_share": ["+14155550199"],
        "max_wait_s": 60}


def _run(user, **extra):
    args = {**ARGS, **extra}
    # Hermes runs the hook, asks the user at its approval gate, and runs the tool only on a yes.
    assert phone_tools.pre_tool_call(tool_name="support_call_start", args=args)["action"] == "approve"
    out = json.loads(phone_tools.start(args, parent_agent=user))
    sid = out["session_id"]
    while out["status"] != "call_ended":
        out = json.loads(phone_tools.wait({"session_id": sid, "max_wait_s": 60}, parent_agent=user))
    return sid, out


def test_offer_on_the_call_comes_back_for_approval_and_outcome_is_saved(standin):
    user = User([NO, YES])
    sid, out = _run(user)
    call = next(iter(standin.calls.values()))

    # one ask placed one call, through the phone menu by intent, with only the allowed details
    assert len(standin.calls) == 1
    assert call.payload["ivr_mode"] is True and "precall_dtmf_sequence" not in call.payload
    assert "order number: 1234" in call.payload["task"] and "+14155550199" not in json.dumps(call.payload)
    assert "fails, times out or returns no decision" in call.payload["task"]  # a failed tool call agrees to nothing

    # both offers reached the user before the agent answered them
    assert len(user.asked) == 2
    assert "$36" in user.asked[0] and "$180" in user.asked[1]
    assert [c["got"]["decision"] for c in call.tool_calls] == ["declined", "accepted"]
    said = [t["text"] for t in call.transcripts if t["user"] == "assistant"]
    assert "Yes, the $36 partial refund works." not in said
    assert said.index("Yes please, go ahead with the full $180 refund.") > said.index(
        "Thanks, but the jacket is unusable after two weeks. The account holder would like the full $180 refund.")

    assert out["summary"] == "Full $180 refund agreed, reference RF-48213."
    assert out["price"] is not None
    done = json.loads(phone_tools.end({"session_id": sid, "outcome": "Full $180 refund, RF-48213",
                                       "resolved": True}, parent_agent=user))
    text = Path(done["transcript_file"]).read_text()
    assert "RF-48213" in text and "**Approvals asked:** 2" in text and "Cost (USD)" in text
    assert done["check"] == []
    state = json.loads((Path(done["transcript_file"]).parent / "session.json").read_text())
    assert state["closed"] and state["call_id"] == call.id


def test_the_call_itself_waits_for_the_users_ok(standin):
    directive = phone_tools.pre_tool_call(tool_name="support_call_start", args={**ARGS, "max_minutes": 15, "record": True})
    assert directive["action"] == "approve"
    for shown in ("+18005550142", "Northwind Outfitters", "15 minutes", "recorded", "bills"):
        assert shown in directive["message"]
    # an "always" answer to this call never covers the next one
    again = phone_tools.pre_tool_call(tool_name="support_call_start", args={**ARGS, "max_minutes": 15, "record": True})
    assert again["rule_key"] != directive["rule_key"]
    assert phone_tools.pre_tool_call(tool_name="support_chat_wait", args=ARGS) is None


def test_a_placed_call_asks_the_agent_to_report_it(standin):
    """Hermes's gate can answer yes with nobody asked (yolo), so the result itself says to report back."""
    assert phone_tools.pre_tool_call(tool_name="support_call_start", args=dict(ARGS))["action"] == "approve"
    out = json.loads(phone_tools.start(dict(ARGS), parent_agent=User([NO, YES])))
    assert "+18005550142" in out["tell_user"] and "bland.ai" in out["tell_user"]


def test_a_call_that_skipped_the_approval_is_not_placed(standin):
    out = json.loads(phone_tools.start(dict(ARGS), parent_agent=User([YES])))
    assert "Not placed" in out["error"] and standin.calls == {}
    phone_tools.pre_tool_call(tool_name="support_call_start", args=dict(ARGS))
    changed = json.loads(phone_tools.start({**ARGS, "max_minutes": 120}, parent_agent=User([YES])))
    assert "Not placed" in changed["error"] and standin.calls == {}  # approved 30 minutes, not 120


def test_with_approvals_off_the_call_is_not_placed(standin, monkeypatch):
    monkeypatch.setattr(handlers, "_approvals_off", lambda: True)
    directive = phone_tools.pre_tool_call(tool_name="support_call_start", args=dict(ARGS))
    assert directive["action"] == "block" and json.loads(directive["message"])["status"] == "needs_user"
    assert "Not placed" in json.loads(phone_tools.start(dict(ARGS), parent_agent=User([YES])))["error"]


def test_the_key_is_read_through_hermess_secret_scope(monkeypatch):
    import sys
    import types

    asked = []
    scope = types.ModuleType("agent.secret_scope")
    scope.get_secret = lambda name, default=None: asked.append(name) or "scoped-key"
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setenv("BLAND_API_KEY", "process-env-key")
    assert Bland().key == "scoped-key" and asked == ["BLAND_API_KEY"]


def _start_unasked(**extra):
    """A call placed from a gateway: Hermes passes the tool no agent, so nobody can be asked directly."""
    args = {**ARGS, **extra}
    assert phone_tools.pre_tool_call(tool_name="support_call_start", args=args)["action"] == "approve"
    return json.loads(phone_tools.start(args, session_id="hermes-1"))


def test_with_nobody_to_ask_the_question_goes_back_to_the_model_and_the_call_holds(standin):
    """Seen on a live call: with no clarify callback the bridge answered stop within a second and the
    user was never asked."""
    out = _start_unasked()
    assert out["status"] == "needs_answer" and out["session_id"]
    assert out["they_said"] == standin_mod.PARTIAL and out["you_would_say"] == "Yes, the $36 partial refund works."
    assert standin_mod.PARTIAL in out["question"]
    assert "support_call_wait" in out["instruction"] and "'yes'" in out["instruction"]
    assert "Never answer yes for the user in any other way" in out["instruction"]
    assert standin_mod.PARTIAL in out["support_said"]
    time.sleep(0.5)
    call = next(iter(standin.calls.values()))
    assert call.tool_calls == []  # the request is still held, not answered stop
    assert phone_tools._calls[out["session_id"]].questions[0]["answer"] is None


def test_a_yes_from_the_model_goes_through_hermess_approval_card(standin):
    sid = _start_unasked()["session_id"]
    args = {"session_id": sid, "answer": "yes", "max_wait_s": 60}
    directive = phone_tools.pre_tool_call(tool_name="support_call_wait", args=args)
    assert directive["action"] == "approve" and directive["rule_key"].startswith(f"support_call:{sid}:")
    assert standin_mod.PARTIAL in directive["message"] and "Yes, the $36 partial refund works." in directive["message"]

    out = json.loads(phone_tools.wait(args, session_id="hermes-1"))
    call = next(iter(standin.calls.values()))
    assert [c["got"]["decision"] for c in call.tool_calls] == ["accepted"]
    assert out["status"] == "call_ended" and "$36" in out["summary"]
    assert out["tell_user"] == f"Support was told yes to: \"{standin_mod.PARTIAL}\". Say so in your next message to the user."
    assert phone_tools._calls[sid].questions[0]["answer"] == YES


def test_a_yes_that_skipped_the_card_or_was_refused_there_is_not_recorded(standin):
    sid = _start_unasked()["session_id"]
    args = {"session_id": sid, "answer": "yes", "max_wait_s": 5}
    out = json.loads(phone_tools.wait(args))
    assert "approval prompt" in out["error"]
    assert phone_tools._calls[sid].questions[0]["answer"] is None

    assert phone_tools.pre_tool_call(tool_name="support_call_wait", args=args)["action"] == "approve"
    phone_tools.post_tool_call(tool_name="support_call_wait", args=args, status="blocked", error_message="User denied")
    assert "approval prompt" in json.loads(phone_tools.wait(args))["error"]
    assert phone_tools._calls[sid].questions[0]["answer"] is None and phone_tools._approved == {}


@pytest.mark.parametrize("answer, decision", [("no", "declined"), ("end", "stop")])
def test_a_no_or_an_end_from_the_model_needs_no_card(standin, answer, decision):
    sid = _start_unasked()["session_id"]
    args = {"session_id": sid, "answer": answer, "max_wait_s": 60}
    assert phone_tools.pre_tool_call(tool_name="support_call_wait", args=args) is None
    out = json.loads(phone_tools.wait(args))
    call = next(iter(standin.calls.values()))
    assert call.tool_calls[0]["got"]["decision"] == decision
    assert "tell_user" not in out
    if answer == "no":  # turned down, the call goes on to the next offer, which comes back to the model again
        assert out["status"] == "needs_answer" and out["they_said"] == standin_mod.FULL


def test_an_answer_with_nothing_waiting_is_an_error(standin):
    sid = _start_unasked()["session_id"]
    assert json.loads(phone_tools.wait({"session_id": sid, "answer": "end", "max_wait_s": 60}))["status"] == "call_ended"
    out = json.loads(phone_tools.wait({"session_id": sid, "answer": "no"}))
    assert "Nothing" in out["error"] and "waiting" in out["error"]


class Done:
    """A bland.ai whose call has ended and been priced."""

    def call(self, call_id):
        return {"completed": True, "price": 0.1, "transcripts": []}


class OnHold:
    """A bland.ai whose call goes on, with nothing new said."""

    def call(self, call_id):
        return {"status": "in-progress", "transcripts": []}


def _held_question(call, they_said, you_would_say):
    """bland.ai's agent asking on the bridge: held in its own thread until the call has an answer."""
    got = []
    threading.Thread(target=lambda: got.append(call.check(they_said, you_would_say)), daemon=True).start()
    for _ in range(500):
        if call.questions:
            break
        time.sleep(0.01)
    return got


def _fake_clock_call(tmp_path, monkeypatch, **kw):
    now = [0.0]
    call = CallSession("+18005550142", "L.L.Bean", "Return the boots from order 1234", {"order number": "1234"}, [],
                       tmp_path, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s), **kw)
    call.call_id = "x"
    monkeypatch.setattr(phone_tools, "_calls", {call.id: call})
    monkeypatch.setattr(phone_tools, "_approved", {})
    monkeypatch.setattr(phone_tools, "_client", Done())
    monkeypatch.setattr(handlers, "_approvals_off", lambda: False)
    return call, now


ZIP = "Can I have the name of the caller and the zip code?"


def _bridge_answer(got):
    for _ in range(500):
        if got:
            return got[0]
        time.sleep(0.01)
    raise AssertionError("the bridge request was never answered")


def test_a_typed_answer_with_an_unlisted_detail_gets_the_card_and_plain_guidance_does_not(tmp_path, monkeypatch):
    call, _ = _fake_clock_call(tmp_path, monkeypatch)
    got = _held_question(call, ZIP, "Let me check with the account holder.")
    detail = {"session_id": call.id, "answer": "The zip code is 04032."}
    assert "approval prompt" in json.loads(phone_tools.wait(detail))["error"]  # not without the card
    directive = phone_tools.pre_tool_call(tool_name="support_call_wait", args=detail)
    assert directive["action"] == "approve" and ZIP in directive["message"] and "04032" in directive["message"]
    assert "not list as shareable" in directive["message"]
    phone_tools.post_tool_call(tool_name="support_call_wait", args=detail, status="blocked", error_message="User denied")

    guidance = {"session_id": call.id, "answer": "Ask them to read the web address aloud."}
    assert phone_tools.pre_tool_call(tool_name="support_call_wait", args=guidance) is None
    json.loads(phone_tools.wait(guidance))
    assert _bridge_answer(got) == {"decision": "instruction", "instruction": "Ask them to read the web address aloud."}


def test_a_question_nobody_answers_is_answered_stop_after_170_seconds(tmp_path, monkeypatch):
    call, now = _fake_clock_call(tmp_path, monkeypatch, hold_s=85)
    got = _held_question(call, ZIP, "Let me check with the account holder.")
    assert call.follow(OnHold(), None)["status"] == "needs_answer"
    now[0] = 169.0
    assert call.follow(OnHold(), None)["status"] == "needs_answer" and got == []
    now[0] = 170.0
    call.follow(OnHold(), None)
    assert _bridge_answer(got)["decision"] == "stop"
    assert any(t.who == "note" and ZIP in t.text and "Nobody answered" in t.text for t in call.turns)


def test_a_call_from_a_gateway_belongs_to_the_hermes_session_that_placed_it(standin):
    sid = _start_unasked()["session_id"]  # Hermes's session id comes as the session_id keyword, with no agent
    assert phone_tools._owners[sid] == "hermes-1"
    phone_tools.on_session_finalize(session_id="hermes-2")
    assert sid in phone_tools._calls and phone_tools._bridge is not None
    phone_tools.on_session_finalize(session_id="hermes-1")
    assert phone_tools._calls == {} and phone_tools._bridge is None


def test_bridge_refuses_a_request_without_the_calls_token(standin):
    user = User([YES])
    _run(user)
    tool = next(iter(standin.calls.values())).payload["tools"][0]
    req = urllib.request.Request(tool["url"], data=b'{"they_said": "x", "you_would_say": "yes"}', method="POST",
                                 headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 404


def _ping(url, token):
    req = urllib.request.Request(url, data=b'{"ping": true}', method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def test_the_bridge_answers_a_ping_for_its_call_and_records_nothing(tmp_path):
    from support_chat_plugin.phone import Bridge
    bridge = Bridge()
    try:
        call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path)
        bridge.calls[call.id] = call
        url = f"http://127.0.0.1:{bridge.port}/calls/{call.id}/check"
        assert _ping(url, call.token) == {"ready": call.id}
        assert call.questions == [] and not list(tmp_path.iterdir())
        with pytest.raises(urllib.error.HTTPError) as err:
            _ping(url, "wrong")
        assert err.value.code == 404
        assert call.reaches(f"http://127.0.0.1:{bridge.port}") is None
    finally:
        bridge.close()


def test_a_call_whose_bridge_address_does_not_reach_the_bridge_is_not_placed(standin, monkeypatch, make_ctx):
    # a tunnel that forwards somewhere else: here the stand-in, which knows no /calls/<id>/check
    monkeypatch.setattr(handlers, "_ctx", make_ctx({"call_bridge_url": standin.url, "call_bridge_port": _free_port()}))
    assert phone_tools.pre_tool_call(tool_name="support_call_start", args=dict(ARGS))["action"] == "approve"
    out = json.loads(phone_tools.start(dict(ARGS), parent_agent=User([YES])))
    assert out["error"].startswith(f"Not placed: the approval bridge address ({standin.url}) does not reach")
    assert f"127.0.0.1:{phone_tools._bridge.port}" in out["error"]
    assert standin.calls == {} and phone_tools._bridge.calls == {}


def test_a_bridge_check_that_fails_says_why_without_the_token(tmp_path):
    slept = []
    tries = []

    def refused(req, timeout):
        tries.append(timeout)
        raise urllib.error.URLError(ConnectionRefusedError("Connection refused"))

    call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path, sleep=slept.append, urlopen=refused)
    reason = call.reaches("https://tunnel.example")
    assert reason and "refused" in reason and call.token not in reason
    assert tries == [10, 10] and slept == [2.0]  # one retry, for a tunnel that was just started

    def timed_out(req, timeout):
        raise TimeoutError("timed out")

    assert CallSession("+1", "N", "t", {}, [], tmp_path, sleep=slept.append, urlopen=timed_out).reaches(
        "https://tunnel.example") == "no answer in time"


def test_a_call_whose_bridge_address_reaches_the_bridge_is_placed(standin):
    assert phone_tools.pre_tool_call(tool_name="support_call_start", args=dict(ARGS))["action"] == "approve"
    out = json.loads(phone_tools.start(dict(ARGS), parent_agent=User([NO, YES])))
    assert "error" not in out and len(standin.calls) == 1
    assert next(iter(standin.calls.values())).payload["tools"][0]["url"].startswith(
        f"http://127.0.0.1:{phone_tools._bridge.port}/calls/")


def test_without_a_bridge_the_call_can_agree_to_nothing(tmp_path):
    call = CallSession("+18005550142", "Northwind", "Full refund", {"order number": "1234"}, [], tmp_path)
    payload = call.payload(None)
    assert "tools" not in payload
    assert "cannot agree to anything" in payload["task"]


class Mute:
    """A server that reads the whole request, then says nothing (a read timeout), hangs up (a reset),
    or answers 200 with something that is not JSON."""

    def __init__(self, mode):
        self.mode = mode
        self.requests = 0
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            raw = b""
            while b"\r\n\r\n" not in raw:
                raw += conn.recv(65536)
            head, body = raw.split(b"\r\n\r\n", 1)
            length = next((int(l.split(b":")[1]) for l in head.split(b"\r\n") if l.lower().startswith(b"content-length")), 0)
            while len(body) < length:
                body += conn.recv(65536)
            self.requests += 1
            if self.mode == "silent":
                time.sleep(1.5)
            elif self.mode == "reset":
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            else:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: 6\r\nConnection: close\r\n\r\n<html>")
            conn.close()

    def close(self):
        self.sock.close()


@pytest.mark.parametrize("mode", ["silent", "reset", "html"])
def test_every_transport_failure_is_a_bland_error(mode):
    server = Mute(mode)
    try:
        with pytest.raises(BlandError) as err:
            Bland(api_key="test-key", base_url=server.url, timeout_s=0.3).send_call({"phone_number": "+1", "task": "x"})
        assert err.value.maybe_sent  # the POST reached bland.ai; whether a call exists is unknown
        assert "test-key" not in str(err.value)
    finally:
        server.close()
    with pytest.raises(BlandError) as refused:
        Bland(api_key="test-key", base_url=f"http://127.0.0.1:{_free_port()}").send_call({})
    assert not refused.value.maybe_sent


def test_a_call_that_may_have_been_placed_is_not_placed_again(standin, monkeypatch):
    server = Mute("silent")
    monkeypatch.setattr(phone_tools, "_client", Bland(api_key="test-key", base_url=server.url, timeout_s=0.3))
    try:
        phone_tools.pre_tool_call(tool_name="support_call_start", args=dict(ARGS))
        out = json.loads(phone_tools.start(dict(ARGS), parent_agent=User([YES])))
        assert "may or may not have been placed" in out["error"] and "call log" in out["error"]
        assert "not place it again" in out["error"].lower()
        again = json.loads(phone_tools.start(dict(ARGS), parent_agent=User([YES])))
        assert "Not placed" in again["error"] and server.requests == 1
    finally:
        server.close()


def test_a_refused_call_cannot_be_placed_later(standin):
    assert phone_tools.pre_tool_call(tool_name="support_call_start", args=dict(ARGS))["action"] == "approve"
    phone_tools.post_tool_call(tool_name="support_call_start", args=dict(ARGS), status="blocked",
                               error_message="User denied")
    out = json.loads(phone_tools.start(dict(ARGS), parent_agent=User([YES])))
    assert "Not placed" in out["error"] and standin.calls == {}


@pytest.mark.parametrize("args", [
    {**ARGS, "task": "Full refund for order 1234; my number is +1 415 555 0199 if they ask."},
    {**ARGS, "shareable": {"order number": "1234", "callback": "(415) 555-0199 / +14155550199"}},
    {**ARGS, "never_share": ["José Pérez"], "task": "Full refund for order 1234 for JOSE PEREZ."},
    {**ARGS, "never_share": ["Sam Rivera"], "shareable": {"name": "sam  rivera"}},
])
def test_a_never_share_value_never_goes_to_bland(standin, args):
    plan, why = phone_tools._plan(args)
    assert plan is None and "never_share" in why
    assert phone_tools.pre_tool_call(tool_name="support_call_start", args=args) is None
    out = json.loads(phone_tools.start(args, parent_agent=User([YES])))
    assert "never_share" in out["error"] and standin.calls == {} and phone_tools._approved == {}
    assert phone_tools._plan({**ARGS, "never_share": ["", " - "]})[0] is not None


def test_the_approval_question_names_what_may_be_shared_not_the_values(standin):
    message = phone_tools.pre_tool_call(tool_name="support_call_start", args={**ARGS, "max_minutes": 15})["message"]
    assert "It may share: order number, name." in message and "Sam Rivera" not in message
    for shown in ("+18005550142", "Northwind Outfitters", "15 minutes", "not recorded", "bills"):
        assert shown in message
    none = phone_tools.pre_tool_call(tool_name="support_call_start", args={**ARGS, "shareable": {}})["message"]
    assert "It shares none of your details." in none


def test_an_answer_goes_only_to_the_question_it_was_given_to(tmp_path):
    call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path, hold_s=0.05)
    partial, full = standin_mod.PARTIAL, standin_mod.FULL
    assert call.check(partial, "Yes, the $36 partial refund works.")["decision"] == "pending"
    call.questions[0]["answer"] = YES
    assert call.check(full, "Yes please, the full $180.")["decision"] == "pending"  # not the $36 yes
    assert len(call.questions) == 2
    assert call.check(partial, "Yes, the $36 partial refund works.")["decision"] == "accepted"
    assert call.check(full, "Yes please, the full $180.")["decision"] == "pending"
    assert call.check("Sure, I'll hold.", "")["decision"] == "pending"  # other words, no ticket: a new question
    assert len(call.questions) == 3


def test_asking_again_with_the_pending_tickets_is_the_same_question_whatever_the_words(tmp_path):
    """Seen on a live call: after pending, the voice agent passes support's latest words, not the question's."""
    call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path, hold_s=0.05)
    first = call.check(ZIP, "")
    assert first["decision"] == "pending" and first["ticket"] == call.questions[0]["ticket"]
    assert f"ticket {first['ticket']}" in first["instruction"] and "before saying anything else" in first["instruction"]
    call.answer(call.questions[0], "The zip code is 04032.")
    restored = CallSession.restore(tmp_path, hold_s=0.05)  # the ticket survives a save
    assert restored.check("Sure, I'll hold.", "", first["ticket"]) == {
        "decision": "instruction", "instruction": "The zip code is 04032."}
    assert len(restored.questions) == 1


def test_an_unfilled_template_from_bland_is_an_empty_value(tmp_path):
    """Seen on a live call: with you_would_say left out, bland.ai sent the template text itself."""
    from support_chat_plugin.phone import Bridge
    bridge = Bridge()
    try:
        call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path, hold_s=0.05)
        bridge.calls[call.id] = call
        body = json.dumps({"they_said": ZIP, "you_would_say": "{{input.you_would_say}}", "ticket": "{{ input.ticket }}"})
        req = urllib.request.Request(f"http://127.0.0.1:{bridge.port}/calls/{call.id}/check", data=body.encode(),
                                     method="POST", headers={"Authorization": f"Bearer {call.token}",
                                                             "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            assert json.loads(resp.read())["decision"] == "pending"
    finally:
        bridge.close()
    q = call.questions[0]
    assert q["they_said"] == ZIP and q["you_would_say"] == ""
    question = phone_tools._question(q)
    assert ZIP in question and "{{" not in question and "I'll answer: \"\"" not in question
    call.answer(q, YES)
    assert "Say yes to what they asked." in call.check(ZIP, "")["instruction"]


def test_the_tool_declares_the_ticket_and_waits_as_long_as_one_hold(tmp_path):
    from support_chat_plugin import phone
    tool = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path).payload("https://b.example")["tools"][0]
    assert tool["timeout"] == int((phone.HOLD_S + 10) * 1000)
    assert tool["input_schema"]["properties"]["ticket"] == {"type": "string"}
    assert "ticket" not in tool["input_schema"]["required"]
    assert tool["body"]["ticket"] == "{{input.ticket}}" and tool["response"]["ticket"] == "$.ticket"
    assert phone.ANSWER_S > phone.HOLD_S


class DeadLine:
    """A bland.ai whose stop and reads fail, as with a revoked key or an outage."""

    def __init__(self):
        self.stops = self.reads = 0

    def stop(self, call_id):
        self.stops += 1
        raise BlandError("bland.ai POST /v1/calls/x/stop answered 401: invalid key")

    def call(self, call_id):
        self.reads += 1
        raise BlandError("bland.ai GET /v1/calls/x answered 503: unavailable")


def test_a_failed_hang_up_is_loud(tmp_path):
    call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path, sleep=lambda s: None)
    call.call_id = "x"
    client = DeadLine()
    out = call.finish(client, "Nothing agreed", False)
    assert client.stops == 2 and out["hangup"] == "failed"
    assert "may still be running" in out["detail"] and "dashboard" in out["detail"]
    assert "may still be running" in Path(out["transcript_file"]).read_text()


ZAPPOS = [  # a real call to a human agent, in order
    ("support", "Hello. Thank you for contacting Zappos. This is Luna Speak in my happy name."),
    ("support", "speaking. May I have your name, please?"),
    ("you", "Hi Luna. I am an AI assistant calling on behalf of a customer with a general question about your return policy."),
    ("support", "Okay, understand. And could you let me know that general question?"),
    ("you", "Yes, I have three questions. First, how long does a customer have to return shoes bought on zappos.com? "
            "Second, is return shipping free? And third, where can I find the full return policy online?"),
    ("support", "The first one, the return time frame is up to sixty days The return is free, and as to the return policy, "
                "where it could be located I'd have to go ahead and check into that for you."),
    ("you", "Got it, sixty days and free returns. Please go ahead and check where I can find the full return policy."),
    ("support", "Okay, one moment, please."),
    ("you", "Okay, thank you."),
    ("support", "I'm seen here from the Zappos.com app. All you need to do is scroll all the way down to the bottom of the "
                "screen, and from there, you should see the options listed in. contact service as FAQ or."),
    ("support", "Shipping and return policy."),
    ("you", "Understood. So to summarize: customers have sixty days to return items, return shipping is free, and the "
            "policy can be found at the bottom of the app under Shipping and Return Policy. Thank you so much for your "
            "help, Luna."),
    ("support", "Not a problem. I'm happy I was able to assist. And apart from that, do you have any other questions or concerns?"),
    ("you", "No, that answers all my questions. Thank you for your time."),
]


def test_an_answered_offer_does_not_flag_every_later_line(tmp_path):
    call = CallSession("+18009274466", "Zappos", "Ask about the return policy", {}, [], tmp_path)
    call.turns = [Turn(who, text) for who, text in ZAPPOS]
    check = " ".join(call.audit())
    assert "'Okay, thank you.'" not in check
    assert "So to summarize" not in check and "that answers all my questions" not in check


def test_a_yes_to_a_later_offer_is_still_flagged(tmp_path):
    call = CallSession("+18005550142", "Northwind", "Full refund for order 1234", {}, [], tmp_path)
    call.turns = [Turn("support", "I can offer a $36 partial refund. Would that work?"),
                  Turn("you", "No thanks, the account holder would like the full refund."),
                  Turn("support", "I can offer a $50 store credit. Would you like that?"),
                  Turn("you", "Yes, the store credit works.")]
    assert call.audit() == ["said 'Yes, the store credit works.' with no approval from you"]


class Waiting:
    """A bland.ai on hold: the same waiting note, again and again, between two support lines said twice."""

    def call(self, call_id):
        note = "[Waiting] Auto response disabled by IVR mode (amd: true)"
        lines = [("user", "Please hold."), *[("agent-action", note)] * 26, ("user", "Please hold."),
                 ("assistant", "Thank you."), ("assistant", "Thank you."), ("agent-action", note)]
        return {"transcripts": [{"id": i, "user": who, "text": text} for i, (who, text) in enumerate(lines)]}


def test_a_waiting_note_said_again_is_written_once(tmp_path):
    call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path)
    call.call_id = "x"
    call._read(Waiting())
    note = "[Waiting] Auto response disabled by IVR mode (amd: true)"
    assert [(t.who, t.text) for t in call.turns] == [
        ("support", "Please hold."), ("note", note), ("support", "Please hold."),
        ("you", "Thank you."), ("you", "Thank you."), ("note", note)]
    assert call.seen == list(range(31))
    assert call._read(Waiting()) == [] and len(call.turns) == 6


def test_following_stops_when_bland_cannot_be_read(tmp_path):
    now = [0.0]
    call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path, clock=lambda: now[0],
                       sleep=lambda s: now.__setitem__(0, now[0] + s))
    call.call_id = "x"
    client = DeadLine()
    out = call.follow(client, None, max_wait_s=240)
    assert client.reads == 5 and out["status"] == "on_call"
    assert "503" in out["read_error"] and "may still be running" in out["detail"]
