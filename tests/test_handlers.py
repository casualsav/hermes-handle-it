import json
import re
import sys
import types
from pathlib import Path

import pytest

from support_chat_plugin import handlers


class Ctx:
    """Hermes's PluginContext: dispatch_tool runs the named core tool with the kwargs given."""

    def __init__(self):
        self.dispatched = []

    def dispatch_tool(self, name, args, **kwargs):
        self.dispatched.append((name, args))
        reply = kwargs["callback"]([{"qid": "q0", **args["questions"][0], "multi_select": False}])
        return json.dumps({"responses": [{"status": "answered", "user_response": reply["answers"]["q0"]}],
                           "outcome": "answered"})


class User:
    def clarify_callback(self, questions):
        return {"answers": {"q0": questions[0]["choices"][1]}, "outcome": "answered"}


def test_the_approval_question_goes_through_hermess_clarify_tool(monkeypatch):
    ctx = Ctx()
    monkeypatch.setattr(handlers, "_ctx", ctx)

    answer = handlers._asker(User())("Take the $20 credit?", ["Accept", "Decline"])

    assert answer == "Decline"
    assert ctx.dispatched == [("clarify", {"questions": [{"question": "Take the $20 credit?",
                                                          "choices": ["Accept", "Decline"]}]})]


# --- the tools and hooks against a fake browser -----------------------------------------------

NAME = {"label": "Name", "type": "text", "required": True, "options": []}
EMAIL = {"label": "Email", "type": "email", "required": True, "options": []}
TOPIC = {"label": "Topic", "type": "select", "required": False, "options": ["Billing", "Orders"]}


class FakePage:
    """A chat page; with ``fields`` its message box only shows once the pre-chat form is filled."""

    def __init__(self, fields=None, composer=True, form_stays=False):
        self.lines = ["Northwind Help", "Live chat"]
        self.sent = []
        self.closed = False
        self.filled = None
        self.fields = list(fields or [])
        self.composer = composer and not self.fields
        self.form_stays = form_stays

    def _form_done(self):
        return self.filled is not None and not self.form_stays

    def text(self):
        return "\n".join(self.lines)

    def send(self, message):
        self.sent.append(message)
        self.lines += ["You", message]

    def screenshot(self, path):
        return None

    def close(self):
        self.closed = True

    def find_composer(self, open_chat=True, **_kw):
        return self.composer or self._form_done()

    def prechat_fields(self):
        return [] if self._form_done() else list(self.fields)

    def fill_prechat(self, values):
        self.filled = dict(values)
        return True


class FakeBrowser:
    def __init__(self):
        self.pages = []
        self.next_page = None
        self.down = False

    def open(self, url):
        page, self.next_page = self.next_page or FakePage(), None
        self.pages.append(page)
        return page

    def shutdown(self):
        self.down = True


class Agent:
    """The running agent Hermes passes to a tool: its session id, and nobody to ask."""

    def __init__(self, session_id):
        self.session_id = session_id


def _plan(fields, shareable):
    """Stand-in for prechat.plan: a field takes the shareable value filed under its exact label."""
    values = {f["label"]: shareable[f["label"]] for f in fields if f["label"] in shareable}
    return values, [f["label"] for f in fields if f["required"] and f["label"] not in shareable]


@pytest.fixture()
def browser(monkeypatch, tmp_path):
    fake = FakeBrowser()
    monkeypatch.setattr(handlers, "_home", tmp_path)
    monkeypatch.setattr(handlers, "_ctx", None)
    monkeypatch.setattr(handlers, "_sessions", {})
    monkeypatch.setattr(handlers, "_owners", {}, raising=False)
    monkeypatch.setattr(handlers, "_browser", fake)
    monkeypatch.setattr(handlers, "_get_browser", lambda: fake)
    monkeypatch.setattr(handlers, "prechat", types.SimpleNamespace(plan=_plan), raising=False)
    monkeypatch.setattr(handlers.ChatSession, "wait_for_reply",
                        lambda self, max_wait_s=240.0, quiet_s=3.0: {"status": "replied", "support_said": ["Dana: Hi"],
                                                                     "waited_s": 0})
    return fake


def _start(parent_agent=None, **args):
    out = json.loads(handlers.start({"url": "https://shop.test/help", "task": "Full refund for order 1234",
                                     "shareable": {"order number": "1234"}, **args}, parent_agent=parent_agent))
    return out


ACCEPT = {"message": "Yes, I accept the $36 partial refund.", "intent": "agree"}


def test_every_approval_ask_gets_its_own_rule_key(browser):
    sid = _start()["session_id"]

    first = handlers.pre_tool_call("support_chat_send", {"session_id": sid, **ACCEPT})
    second = handlers.pre_tool_call("support_chat_send", {"session_id": sid, **ACCEPT})

    assert first["action"] == second["action"] == "approve"
    assert first["rule_key"].startswith(f"support_chat:{sid}:")
    assert first["rule_key"] != second["rule_key"]


def test_with_approvals_off_a_risky_reply_is_blocked_as_needs_user(browser, monkeypatch):
    """Characterization: holds today."""
    monkeypatch.setattr(handlers, "_approvals_off", lambda: True)
    sid = _start()["session_id"]

    out = handlers.pre_tool_call("support_chat_send", {"session_id": sid, **ACCEPT})

    assert out["action"] == "block"
    assert json.loads(out["message"])["status"] == "needs_user"
    assert handlers._sessions[sid].pending is None


def test_the_hook_never_reopens_a_chat_that_is_not_live(browser, monkeypatch):
    sid = _start()["session_id"]
    handlers._sessions.pop(sid).save()  # saved on disk, as after a restart
    opened = []
    monkeypatch.setattr(handlers, "_get_browser", lambda: opened.append(1) or browser)

    assert handlers.pre_tool_call("support_chat_send", {"session_id": sid, **ACCEPT}) is None
    assert opened == []


def test_a_denied_reply_keeps_the_chat_and_an_unanswered_one_closes_it(browser):
    """Characterization: holds today."""
    sid = _start()["session_id"]
    args = {"session_id": sid, **ACCEPT}
    assert handlers.pre_tool_call("support_chat_send", args)["action"] == "approve"

    handlers.post_tool_call("support_chat_send", args, status="blocked", error_message="User denied this action")

    assert sid in handlers._sessions
    assert handlers._sessions[sid].pending is None

    assert handlers.pre_tool_call("support_chat_send", args)["action"] == "approve"
    handlers.post_tool_call("support_chat_send", args, status="blocked",
                            error_message="Approval timed out without user response")

    assert sid not in handlers._sessions


def test_finalizing_one_hermes_session_leaves_the_others_chats_running(browser):
    a = _start(Agent("A"))["session_id"]
    b = _start(Agent("B"))["session_id"]
    nobody = _start()["session_id"]

    handlers.on_session_finalize(session_id="A", platform="cli")

    assert set(handlers._sessions) == {b}
    assert not browser.down
    assert (next((handlers._root() / "chats").glob(f"*-{a}")) / "session.json").is_file()
    assert nobody not in handlers._sessions

    handlers.on_session_finalize(session_id="B", platform="cli")

    assert handlers._sessions == {}
    assert browser.down


def test_a_prechat_form_asking_for_an_unlisted_detail_types_nothing(browser):
    page = browser.next_page = FakePage(fields=[NAME, EMAIL, TOPIC])

    out = _start(shareable={"Name": "Sam Rivera"})

    assert out["status"] == "needs_details"
    assert out["form_asks_for"] == ["Email"]
    assert page.closed and page.filled is None and page.sent == []
    assert handlers._sessions == {}


def test_a_prechat_form_is_filled_from_the_shareable_details_only(browser, make_ctx, monkeypatch):
    ctx = make_ctx()
    monkeypatch.setattr(handlers, "_ctx", ctx)
    page = browser.next_page = FakePage(fields=[NAME, EMAIL, TOPIC])

    out = _start(shareable={"Name": "Sam Rivera", "Email": "sam@example.com", "order number": "1234"})

    assert out["status"] == "replied"
    assert page.filled == {"Name": "Sam Rivera", "Email": "sam@example.com"}
    notes = [t.text for t in handlers._sessions[out["session_id"]].turns if t.who == "note"]
    assert notes == ["Filled the chat's pre-chat form: Name, Email"]
    assert "Filled the chat's pre-chat form: Name, Email" in ctx.progress()
    assert not any("sam@example.com" in line for line in ctx.progress())


def test_a_prechat_form_that_stays_after_filling_lists_what_it_still_wants(browser):
    page = browser.next_page = FakePage(fields=[NAME, TOPIC], form_stays=True)

    out = _start(shareable={"Name": "Sam Rivera"})

    assert out["status"] == "needs_details"
    assert out["form_asks_for"] == ["Topic"]
    assert page.closed and handlers._sessions == {}


def test_no_message_box_and_no_form_is_an_error_that_does_not_blame_a_form(browser):
    page = browser.next_page = FakePage(composer=False)

    out = _start()

    assert "no chat message box" in out["error"]
    assert "pre-chat form" not in out["error"]
    assert page.closed


def _declared(key):
    text = (Path(handlers.__file__).with_name("plugin.yaml")).read_text(encoding="utf-8")
    block = re.search(rf"^{key}:\n((?:\s+- .+\n)+)", text, re.M).group(1)
    return {line.strip()[2:].strip() for line in block.splitlines()}


def test_register_provides_exactly_what_plugin_yaml_declares(make_ctx, monkeypatch):
    """Characterization: holds today."""
    monkeypatch.setattr(handlers, "_ctx", None)
    ctx = make_ctx()

    sys.modules["support_chat_plugin"].register(ctx)

    assert {name for name, _, _ in ctx.tools} == _declared("provides_tools")
    assert {name for name, _ in ctx.hooks} == _declared("provides_hooks")
    assert ("pre_tool_call", handlers.pre_tool_call) in ctx.hooks
    assert ("post_tool_call", handlers.post_tool_call) in ctx.hooks
    assert ("on_session_finalize", handlers.on_session_finalize) in ctx.hooks
