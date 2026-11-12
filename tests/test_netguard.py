"""The chat browser opens public addresses only: every page, redirect hop and frame is checked."""

import functools
import http.server
import json
import threading

import pytest

from support_chat_plugin import handlers, netguard


@pytest.fixture(autouse=True)
def no_opt_out(monkeypatch):
    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "0")
    netguard._cache.clear()
    yield
    netguard._cache.clear()


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8765/", "http://localhost/", "http://10.0.0.5/", "http://192.168.1.1/admin",
    "http://172.16.0.1/", "http://100.64.0.1/", "http://169.254.169.254/latest/meta-data/",
    "http://[::1]/", "http://[fe80::1]/", "http://[fd00:ec2::254]/", "http://[::ffff:169.254.169.254]/",
    "http://metadata.google.internal/", "http://printer.local/", "http://0.0.0.0/", "file:///etc/passwd",
])
def test_private_loopback_link_local_and_metadata_are_refused(url):
    assert netguard.refusal(url)


def test_a_name_that_resolves_to_a_private_address_is_refused(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.215.14", "10.1.2.3"])
    assert "10.1.2.3" in netguard.refusal("https://support.example.com/chat")


def test_a_public_address_passes(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.215.14"])
    assert netguard.refusal("https://support.example.com/chat") is None
    assert netguard.refusal("about:blank") is None


def test_hermess_opt_out_lifts_private_but_never_metadata_or_link_local(monkeypatch):
    monkeypatch.setenv("HERMES_ALLOW_PRIVATE_URLS", "1")
    assert netguard.refusal("http://127.0.0.1:8765/") is None
    assert netguard.refusal("http://169.254.169.254/") and netguard.refusal("http://169.254.1.1/")
    assert netguard.refusal("http://metadata.google.internal/")


def test_support_chat_start_refuses_a_private_page_before_opening_anything(tmp_path, monkeypatch):
    from support_chat_plugin.browser import Browser

    opened = []
    monkeypatch.setattr(Browser, "_ensure", lambda self: opened.append(1))
    handlers.configure(tmp_path)
    out = json.loads(handlers.start({"url": "http://169.254.169.254/latest/meta-data/", "task": "x"}))
    assert out.get("error") and "session_id" not in out
    assert opened == []  # no browser was started, nothing was requested
    handlers.shutdown()
    handlers.configure(None)


class _Redirects(http.server.BaseHTTPRequestHandler):
    """A public-looking site whose pages redirect into the private network."""

    seen: list = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.seen.append(self.path)
        port = self.server.server_address[1]
        hops = {"/hop1": "/hop2", "/hop2": "http://169.254.169.254/latest/meta-data/",
                "/to-loopback": f"http://localhost:{port}/secret"}
        if self.path in hops:
            self.send_response(302)
            self.send_header("Location", hops[self.path])
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("content-type", "text/html")
        self.end_headers()
        body = f"<p>page {self.path}</p>"
        if self.path == "/framed":
            body += f"<iframe src='http://localhost:{port}/secret'></iframe>"
        self.wfile.write(body.encode())


@pytest.fixture()
def site(monkeypatch, tmp_path):
    """The site is served on 127.0.0.1 but its name, site.test, resolves as a public address would."""
    pytest.importorskip("playwright")
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Redirects)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    real = netguard._resolve
    monkeypatch.setattr(netguard, "_resolve", lambda host: ["93.184.215.14"] if host == "site.test" else real(host))
    from support_chat_plugin.browser import Browser

    b = Browser(tmp_path / "profile")
    real_ensure = b._ensure

    def ensure():  # site.test reaches the local server, as DNS would reach a real one
        if b._context is None:
            real_ensure()
            b._context.close()
            b._context = b._pw.chromium.launch_persistent_context(
                str(tmp_path / "profile2"), headless=True, executable_path=_exe(),
                args=[f"--host-resolver-rules=MAP site.test 127.0.0.1:{httpd.server_address[1]}"])

    b._ensure = ensure
    yield b, f"http://site.test:{httpd.server_address[1]}"
    b.shutdown()
    httpd.shutdown()


def _exe():
    from support_chat_plugin.browser import _chromium_candidates

    found = _chromium_candidates()
    if not found:
        pytest.skip("no Chromium")
    return found[0]


@pytest.mark.parametrize("path, refused", [("/hop1", "169.254.169.254"), ("/to-loopback", "localhost")])
def test_every_redirect_hop_is_checked_before_it_is_sent(site, path, refused):
    from support_chat_plugin.browser import Refused

    b, base = site
    assert b.open(base + "/plain").text().strip() == "page /plain"
    with pytest.raises(Refused) as err:
        b.open(base + path)
    assert refused in str(err.value)
    assert "/secret" not in _Redirects.seen


def test_a_frame_on_a_private_address_never_loads(site):
    b, base = site
    page = b.open(base + "/framed")
    b.run(lambda: page._page.wait_for_timeout(1500))
    assert page.text().strip() == "page /framed"
    assert "/secret" not in _Redirects.seen
