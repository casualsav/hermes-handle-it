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


# The browser on its own, against widget shapes real sites use.

ENTER_DOES_NOT_SEND = '<!doctype html><title>Chat</title><textarea aria-label="Type your message"></textarea>'

SHADOW = """<!doctype html><title>Chat</title><p>Light text</p><div id="host"></div>
<script>
  const root = document.getElementById("host").attachShadow({mode: "open"});
  root.innerHTML = "<style>p { color: teal; }</style><p>Shadow hello</p><div id='inner'></div>";
  root.getElementById("inner").attachShadow({mode: "open"}).innerHTML = "<p>Nested hello</p>";
  document.body.appendChild(document.createElement("div")).attachShadow({mode: "closed"}).innerHTML = "<p>Closed text</p>";
</script>"""

# An analytics or service-worker frame: in a frame that is not rendered, innerText is the raw textContent.
HIDDEN_FRAME = """<!doctype html><title>Chat</title><p>Visible hello</p>
<iframe style="display:none" srcdoc="<script>window.trackPageview = 'tracker-code';</script>
<noscript>You need to enable JavaScript to run this app.</noscript><p>Hidden frame text</p>"></iframe>"""

# A contact form (Gorgias's, on a real store) that e-mails support: no chat, and no <form> around the fields.
CONTACT_FORM = """<!doctype html><title>Contact us</title><h1>Contact us</h1>
<div class="form"><div><label for="name">Name</label><input id="name" type="text"></div>
<div><label for="email">Email</label><input id="email" type="email"></div>
<div><label for="subject">Subject</label><select id="subject"><option value="">Select a subject</option><option>Order Status</option></select></div>
<div><label for="message">Message</label><textarea id="message"></textarea></div>
<div><button type="button">Send</button></div></div>"""

# Each click lands in window.clicked, so a test can say what the plugin pressed.
_RECORD = """<script>window.clicked = [];
document.addEventListener("click", (e) => { const c = e.target.closest("[data-k]"); if (c) window.clicked.push(c.dataset.k); }, true);
</script>"""
_PANEL = """<div id="panel" hidden><textarea placeholder="Type a message here..."></textarea><button>Send</button></div>
<script>window.openChat = () => { document.getElementById("panel").hidden = false; };</script>"""

# Chaport's and Provide Support's shape: a nameless bubble, absolute inside a fixed container, marked as chat only
# by its class, and an FAQ button elsewhere on the page whose name speaks of a chat.
ICON_LAUNCHER = f"""<!doctype html><title>Shop</title>{_RECORD}
<button data-k="faq" aria-expanded="false">How can I add your chat to my website?</button>{_PANEL}
<div class="widget-container" style="position:fixed;right:0;bottom:0;width:0;height:0">
  <div class="chat-launcher-button" data-k="bubble" onclick="openChat()"
       style="position:absolute;right:20px;bottom:20px;width:60px;height:60px;border-radius:50%;background:#f60;cursor:pointer">
    <svg width="60" height="60"><circle cx="30" cy="30" r="12" fill="#fff"/></svg></div></div>"""

# Zendesk's and Intercom's shape: the bubble is a small fixed frame of its own.
FRAME_LAUNCHER = f"""<!doctype html><title>Shop</title>{_RECORD}{_PANEL}
<iframe name="chat-launcher" src="launcher-frame.html"
        style="position:fixed;right:20px;bottom:20px;width:64px;height:64px;border:0"></iframe>"""
LAUNCHER_FRAME = """<!doctype html><body style="margin:0"><button onclick="parent.openChat()"
  style="width:64px;height:64px;border-radius:50%;border:0;background:#06f"><svg width="24" height="24"></svg></button>"""

# Corner fixtures that are not a chat, though a theme's class or a banner's title mentions one; and Jivo's refusal.
NO_CHAT = f"""<!doctype html><title>Shop</title>{_RECORD}<div style="height:3000px">Products</div>
<a href="#top" class="scroll-top chat-offset" data-k="top" title="Back to top"
   style="position:fixed;right:20px;bottom:20px;width:48px;height:48px;background:#333"></a>
<div class="cookie-consent" title="Cookies for analytics and our chat" style="position:fixed;right:0;bottom:80px;width:300px">
  <button data-k="accept">I accept cookies</button> <button data-k="refuse" onclick="this.parentElement.remove()">I refuse cookies</button></div>"""


@pytest.fixture()
def chat_browser(tmp_path, monkeypatch):
    """The plugin's browser. The pages are on 127.0.0.1, so Hermes's opt-out is on, as for the demo."""
    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "1")
    b = browser.Browser(tmp_path / "profile")
    yield b
    b.shutdown()


@pytest.fixture()
def pages(tmp_path):
    """Small test-only pages, served like the shop."""
    root = tmp_path / "pages"
    root.mkdir()
    (root / "enter-does-not-send.html").write_text(ENTER_DOES_NOT_SEND, encoding="utf-8")
    (root / "shadow.html").write_text(SHADOW, encoding="utf-8")
    (root / "hidden-frame.html").write_text(HIDDEN_FRAME, encoding="utf-8")
    (root / "contact-form.html").write_text(CONTACT_FORM, encoding="utf-8")
    (root / "icon-launcher.html").write_text(ICON_LAUNCHER, encoding="utf-8")
    (root / "frame-launcher.html").write_text(FRAME_LAUNCHER, encoding="utf-8")
    (root / "launcher-frame.html").write_text(LAUNCHER_FRAME, encoding="utf-8")
    (root / "no-chat.html").write_text(NO_CHAT, encoding="utf-8")
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    httpd.shutdown()


def _open(b, url):
    try:
        return b.open(url)
    except browser.BrowserUnavailable as exc:
        pytest.skip(str(exc))


@pytest.mark.parametrize("page", ["?prechat=1&speed=0.15", "widget.html?prechat=1&speed=0.15"], ids=["page", "iframe-widget"])
def test_a_pre_chat_form_is_filled_before_the_chat_starts(shop, chat_browser, page):
    chat = _open(chat_browser, shop + page)
    assert chat.find_composer(open_chat=True, wait_s=6) is False  # the form's Name field is not the message box
    fields = chat.prechat_fields()
    assert [(f["label"], f["type"], f["required"], f["options"]) for f in fields] == [
        ("Name", "text", True, []), ("Email", "email", True, []), ("Order number", "text", False, [])]
    assert chat.fill_prechat({"Name": "Sam Rivera", "Email": "sam@example.com"}) is True
    assert chat.find_composer(open_chat=False, wait_s=10) is True
    assert chat.prechat_fields() == []
    chat.send("Hi, my jacket's zipper broke.")
    text = chat.text()
    assert "Chatting as Sam Rivera." in text and "Hi, my jacket's zipper broke." in text


def test_a_cookie_banner_over_the_launcher_is_refused_never_accepted(shop, chat_browser):
    chat = _open(chat_browser, shop + "widget.html?cookies=1&speed=0.15")
    assert chat.find_composer(open_chat=True, wait_s=20) is True
    text = chat.text()
    assert "Cookie choice: rejected." in text and "Cookie choice: accepted." not in text


def test_a_message_that_did_not_go_out_is_not_reported_sent(pages, chat_browser):
    chat = _open(chat_browser, pages + "enter-does-not-send.html")
    assert chat.find_composer(open_chat=False, wait_s=5) is True
    with pytest.raises(RuntimeError, match="did not send"):
        chat.send("Hello?")


def test_text_in_open_shadow_roots_is_read(pages, chat_browser):
    text = _open(chat_browser, pages + "shadow.html").text()
    assert text.index("Light text") < text.index("Shadow hello") < text.index("Nested hello")
    assert text.count("Shadow hello") == 1 and "Closed text" not in text and "teal" not in text


def test_a_frame_that_is_not_rendered_is_not_read(pages, chat_browser):
    text = _open(chat_browser, pages + "hidden-frame.html").text()
    assert "Visible hello" in text
    assert "tracker-code" not in text and "enable JavaScript" not in text and "Hidden frame text" not in text


def test_an_email_contact_forms_message_box_is_not_a_chat_box(pages, chat_browser):
    assert _open(chat_browser, pages + "contact-form.html").find_composer(open_chat=False, wait_s=2) is False


def _clicked(b, chat):
    return b.run(lambda: chat._page.evaluate("window.clicked"))


def test_a_nameless_corner_bubble_opens_the_chat(pages, chat_browser):
    chat = _open(chat_browser, pages + "icon-launcher.html")
    assert chat.find_composer(open_chat=True, wait_s=8) is True
    assert _clicked(chat_browser, chat) == ["bubble"]


def test_a_corner_bubble_in_its_own_frame_is_clicked_inside_it(pages, chat_browser):
    assert _open(chat_browser, pages + "frame-launcher.html").find_composer(open_chat=True, wait_s=8) is True


def test_back_to_top_and_a_cookie_banner_are_not_a_chat(pages, chat_browser):
    chat = _open(chat_browser, pages + "no-chat.html")
    assert chat.find_composer(open_chat=True, wait_s=3) is False
    assert _clicked(chat_browser, chat) == ["refuse"]
