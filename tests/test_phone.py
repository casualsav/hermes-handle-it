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


def test_nobody_to_ask_means_nothing_is_agreed(standin):
    sid, out = _run(None)
    call = next(iter(standin.calls.values()))
    assert [c["got"]["decision"] for c in call.tool_calls] == ["stop"]
    assert not any(t["text"].startswith("Yes") for t in call.transcripts if t["user"] == "assistant")
    assert "follow up" in out["summary"]


def test_bridge_refuses_a_request_without_the_calls_token(standin):
    user = User([YES])
    _run(user)
    tool = next(iter(standin.calls.values())).payload["tools"][0]
    req = urllib.request.Request(tool["url"], data=b'{"they_said": "x", "you_would_say": "yes"}', method="POST",
                                 headers={"Authorization": "Bearer wrong", "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 404


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


def test_following_stops_when_bland_cannot_be_read(tmp_path):
    now = [0.0]
    call = CallSession("+18005550142", "Northwind", "Full refund", {}, [], tmp_path, clock=lambda: now[0],
                       sleep=lambda s: now.__setitem__(0, now[0] + s))
    call.call_id = "x"
    client = DeadLine()
    out = call.follow(client, None, max_wait_s=240)
    assert client.reads == 5 and out["status"] == "on_call"
    assert "503" in out["read_error"] and "may still be running" in out["detail"]
