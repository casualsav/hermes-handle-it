"""End to end in a real browser: the four tools resolve a refund on the demo shop's chat,
asking the user once. Skipped when Playwright or Chromium is missing."""

import functools
import http.server
import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("playwright")

from support_chat_plugin import browser, handlers  # noqa: E402
from support_chat_plugin.session import ACCEPT_OFFER, DECLINE_OFFER  # noqa: E402

SHOP = Path(__file__).resolve().parents[1] / "demo" / "shop"


@pytest.fixture()
def hermes(monkeypatch, make_ctx):
    """Hermes around the plugin. The shop runs on 127.0.0.1, so these runs use Hermes's own opt-out
    of private-address blocking, as a user trying the demo does."""
    ctx = make_ctx()
    monkeypatch.setattr(handlers, "_ctx", ctx)
    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "1")
    return ctx


@pytest.fixture()
def shop():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(SHOP))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


class Agent:
    """Stands in for Hermes's agent: answers the approval question."""

    session_id = "hermes-session-1"

    def __init__(self):
        self.questions = []

    def clarify_callback(self, questions):
        q = questions[0]["question"]
        self.questions.append(q)
        answer = DECLINE_OFFER if "partial refund" in q else ACCEPT_OFFER
        return {"answers": {"q0": answer}, "outcome": "answered"}


@pytest.mark.parametrize("page", ["?speed=0.15", "widget.html?speed=0.15"], ids=["page", "iframe-widget"])
def test_refund_on_the_demo_shop(shop, page, tmp_path, hermes):
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
    assert any("Support:" in s for s in hermes.progress())
    assert all(p["session_id"] == "hermes-session-1" for e, p in hermes.events)
    assert [name for name, _ in hermes.dispatched] == ["clarify", "clarify"]
    handlers.shutdown()


def test_a_chat_behind_a_sign_in_does_not_start_without_one(shop, tmp_path, hermes):
    handlers.configure(tmp_path)
    out = json.loads(handlers.start({"url": f"{shop}?login=1", "task": "x", "max_wait_s": 5}, parent_agent=Agent()))
    if "Could not start a browser" in out.get("error", ""):
        pytest.skip(out["error"])
    assert out.get("error") and "session_id" not in out and handlers._sessions == {}
    handlers.shutdown()


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def hermes_browser(tmp_path, monkeypatch):
    """A Chromium standing in for Hermes's own browser, reachable at ``browser.cdp_url``."""
    exe = next(iter(browser._chromium_candidates()), None)
    if exe is None:
        pytest.skip("no Chromium")
    port = _free_port()
    proc = subprocess.Popen([exe, "--headless=new", "--no-sandbox", f"--remote-debugging-port={port}", "--no-first-run",
                             f"--user-data-dir={tmp_path / 'hermes-browser'}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        proc.kill()
        pytest.skip("Chromium did not start")
    monkeypatch.setattr(browser, "_hermes_browser_setting",
                        lambda key, default=None: url if key == "cdp_url" else default)
    yield url
    handlers.shutdown()
    proc.kill()
    proc.wait(timeout=10)


def test_a_sign_in_made_in_hermess_browser_carries_into_the_chat(shop, tmp_path, hermes, hermes_browser):
    """The model signs in with Hermes's browser_vault in Hermes's own browser; the chat, opened in
    that same browser, starts signed in. The plugin never sees the password."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:  # what browser_vault fill does on Hermes's page
        b = pw.chromium.connect_over_cdp(hermes_browser)
        page = b.contexts[0].new_page()
        page.goto(f"{shop}?login=1&speed=0.15")
        page.fill("#password", "northwind-demo")
        page.press("#password", "Enter")
        page.wait_for_selector("#composer", state="visible")
        page.close()
        b.close()
    handlers.configure(tmp_path)
    raw = handlers.start({"url": f"{shop}?login=1&speed=0.15", "task": "Refund for order 1234"}, parent_agent=Agent())
    out = json.loads(raw)
    assert any("How can I help" in line for line in out["support_said"]), out
    assert "northwind-demo" not in raw
    handlers.end({"session_id": out["session_id"], "outcome": "test", "resolved": False}, parent_agent=Agent())
