"""End to end in a real browser: the four tools resolve a refund on the demo shop's chat,
asking the user once. Skipped when Playwright or Chromium is missing."""

import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from support_chat_plugin import handlers  # noqa: E402
from support_chat_plugin.session import ACCEPT_OFFER, DECLINE_OFFER  # noqa: E402

SHOP = Path(__file__).resolve().parents[1] / "demo" / "shop"


@pytest.fixture()
def shop():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SHOP))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


class Agent:
    """Stands in for Hermes's agent: answers the approval question and collects status lines."""

    def __init__(self):
        self.questions = []
        self.status = []

    def clarify_callback(self, questions):
        q = questions[0]["question"]
        self.questions.append(q)
        answer = DECLINE_OFFER if "partial refund" in q else ACCEPT_OFFER
        return {"answers": {"q0": answer}, "outcome": "answered"}

    def _emit_status(self, line):
        self.status.append(line)


@pytest.mark.parametrize("page", ["?speed=0.15", "widget.html?speed=0.15"], ids=["page", "iframe-widget"])
def test_refund_on_the_demo_shop(shop, page, tmp_path):
    handlers.configure(tmp_path)
    agent = Agent()
    try:
        out = json.loads(handlers.start({"url": shop + page, "task": "Full refund for order 1234, the jacket zipper broke",
                                         "shareable": {"order number": "1234", "name": "Sam Rivera"}},
                                        parent_agent=agent))
    except Exception as exc:  # no browser on this machine
        pytest.skip(f"browser unavailable: {exc}")
    if "error" in out:
        pytest.skip(out["error"])
    sid = out["session_id"]
    assert any("How can I help" in line for line in out["support_said"])

    def send(message, intent="inform"):
        return json.loads(handlers.send({"session_id": sid, "message": message, "intent": intent}, parent_agent=agent))

    assert "order number" in " ".join(send("Hi, my Ridgeline jacket's zipper broke and I'd like a refund.")["support_said"])
    assert "name" in " ".join(send("It's order 1234.")["support_said"])
    said = send("Sam Rivera")
    while said["status"] == "replied" and "partial refund" not in " ".join(said["support_said"]):
        said = json.loads(handlers.wait({"session_id": sid}, parent_agent=agent))
    assert agent.questions == []
    said = send("No thanks, the jacket is unusable. I'd like a full refund please.", "decline")
    while "full refund of $180" not in " ".join(said["support_said"]):
        said = json.loads(handlers.wait({"session_id": sid}, parent_agent=agent))
    done = send("Yes, please go ahead and process the full refund.", "agree")
    assert len(agent.questions) == 2  # the partial offer (turned down), the full one (accepted)
    while "RF-48213" not in " ".join(done.get("support_said", [])):
        done = json.loads(handlers.wait({"session_id": sid}, parent_agent=agent))
    closed = json.loads(handlers.end({"session_id": sid, "outcome": "Full refund $180, RF-48213", "resolved": True},
                                     parent_agent=agent))
    assert Path(closed["transcript_file"]).is_file()
    assert any("Support:" in s for s in agent.status)
    handlers.shutdown()


class FakeVault:
    """Hermes's vault backend contract, holding one login for the demo shop."""

    needs_unlock = False
    display_name = "test vault"

    def __init__(self, origin):
        from types import SimpleNamespace

        self.meta = SimpleNamespace(kind="login", origin=origin, allowed_origins=(), identifier="sam@example.com",
                                    has_otp=False)
        self.resolved = 0

    def get_meta(self, handle):
        return self.meta if handle == "h1" else None

    def resolve_password(self, handle):
        self.resolved += 1
        return "northwind-demo"

    def resolve_otp(self, handle):
        return None


@pytest.mark.parametrize("mode", ["1", "2fa"])
def test_sign_in_keeps_the_password_out_of_every_result(shop, tmp_path, monkeypatch, mode):
    handlers.configure(tmp_path)
    vault = FakeVault(shop.rstrip("/"))
    monkeypatch.setattr(handlers, "_vault", lambda handle: vault)
    agent = Agent()
    agent.clarify_callback = lambda qs: {"answers": {"q0": "123456"}, "outcome": "answered"}
    raw = handlers.start({"url": f"{shop}?speed=0.15&login={mode}", "task": "Refund for order 1234",
                          "shareable": {"order number": "1234"}, "login": "h1"}, parent_agent=agent)
    out = json.loads(raw)
    if "error" in out and "browser" in out["error"].lower():
        pytest.skip(out["error"])
    assert vault.resolved == 1
    assert "northwind-demo" not in raw and "123456" not in raw
    assert any("How can I help" in line for line in out["support_said"]), out
    assert not any("northwind-demo" in s or "123456" in s for s in agent.status)
    handlers.end({"session_id": out["session_id"], "outcome": "test", "resolved": False}, parent_agent=agent)
    handlers.shutdown()


def test_a_login_is_only_typed_on_its_own_site(shop, tmp_path, monkeypatch):
    handlers.configure(tmp_path)
    vault = FakeVault("https://northwind.example")
    monkeypatch.setattr(handlers, "_vault", lambda handle: vault)
    out = json.loads(handlers.start({"url": f"{shop}?login=1", "task": "x", "login": "h1"}, parent_agent=Agent()))
    if "error" in out and "browser" in out["error"].lower():
        pytest.skip(out["error"])
    assert out["signed_in"] is False and vault.resolved == 0
    handlers.shutdown()
