"""The plugin's own browser: one Chromium driven by Playwright on one thread.

Playwright's sync API must stay on the thread that started it, while Hermes may
run tool calls on any worker thread, so every browser action is queued to a
single owner thread. When Hermes is configured with ``browser.cdp_url`` the
plugin attaches to that browser (Hermes's own browser or desktop) instead of
launching one.
"""

from __future__ import annotations

import os
import queue
import re
import shutil
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Optional

COMPOSERS = ("textarea", '[contenteditable="true"]', '[role="textbox"]', 'input[type="text"]', "input:not([type])")
COMPOSER_HINT = re.compile(r"message|type|chat|reply|write|ask|question", re.I)
LAUNCHER = re.compile(r"(live chat|chat with|chat now|start (a )?chat|message us|contact support|talk to|^chat$|get help)", re.I)
CODE_HINT = re.compile(r"one-time|otp|2fa|two.factor|verification|security code|sign.in code|\bcode\b", re.I)
SEND = re.compile(r"^\s*send\b|^submit$", re.I)


class BrowserUnavailable(RuntimeError):
    pass


def _hermes_browser_setting(key: str, default: Any = None) -> Any:
    """Read one key of Hermes's ``browser:`` config, read-only; absent outside Hermes."""
    try:
        from hermes_cli.config import load_config

        return (load_config() or {}).get("browser", {}).get(key, default)
    except Exception:
        return default


def _chromium_candidates() -> list[str]:
    """Chromium builds to try when Playwright's own download is missing: Playwright's cache first
    (newest revision), then Hermes's AGENT_BROWSER_EXECUTABLE_PATH, then the PATH."""
    found: list[str] = []
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".cache" / "ms-playwright")
    def revision(p: Path) -> int:
        m = re.search(r"chromium-(\d+)", str(p))
        return int(m.group(1)) if m else 0
    for exe in sorted(cache.glob("chromium-*/chrome-linux*/chrome"), key=revision, reverse=True):
        found.append(str(exe))
    for exe in sorted(cache.glob("chromium-*/chrome-mac*/*.app/Contents/MacOS/*"), key=revision, reverse=True):
        found.append(str(exe))
    env = os.environ.get("AGENT_BROWSER_EXECUTABLE_PATH", "").strip()
    if env:
        found.append(env)
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable", "chrome"):
        exe = shutil.which(name)
        if exe:
            found.append(exe)
    return list(dict.fromkeys(found))


class _Owner:
    """The single thread that owns Playwright."""

    def __init__(self) -> None:
        self._jobs: "queue.Queue[tuple[Callable, Future]]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="chat-browser", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            fn, fut = self._jobs.get()
            if fut.set_running_or_notify_cancel():
                try:
                    fut.set_result(fn())
                except BaseException as exc:  # handed back to the caller's thread
                    fut.set_exception(exc)

    def call(self, fn: Callable, timeout: float = 60.0) -> Any:
        fut: Future = Future()
        self._jobs.put((fn, fut))
        return fut.result(timeout=timeout)


class Browser:
    """``profile`` keeps cookies and storage on disk, so a chat widget reopened after a restart
    finds its conversation again."""

    def __init__(self, profile: Path) -> None:
        self._owner = _Owner()
        self._profile = profile
        self._pw = None
        self._browser = None
        self._context = None
        self.headed = False

    def _alive(self) -> bool:
        try:
            return self._context is not None and (self._browser is None or self._browser.is_connected()) \
                and self._context.pages is not None
        except Exception:
            return False

    def _ensure(self) -> None:
        if self._alive():
            return
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable("Playwright is not installed: pip install playwright") from exc
        if self._pw is None:
            self._pw = sync_playwright().start()
        cdp = str(_hermes_browser_setting("cdp_url", "") or "").strip()
        if cdp:
            self._browser = self._pw.chromium.connect_over_cdp(cdp)
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else self._browser.new_context()
            return
        headed = bool(_hermes_browser_setting("headed", False)) or os.environ.get("AGENT_BROWSER_HEADED", "").lower() in ("1", "true", "yes")
        self.headed = headed
        self._profile.mkdir(parents=True, exist_ok=True)
        opts = dict(headless=not headed, viewport={"width": 1280, "height": 800})
        errors = []
        for exe in [None, *_chromium_candidates()]:
            extra = {"executable_path": exe} if exe else {}
            try:
                self._context = self._pw.chromium.launch_persistent_context(str(self._profile), **opts, **extra)
                return
            except Exception as exc:
                errors.append(f"{exe or 'playwright default'}: {str(exc).splitlines()[0][:160]}")
        raise BrowserUnavailable(
            "Could not start a browser for the support chat. Do not try to repair the system browser; tell the "
            "user to run `python -m playwright install chromium` in Hermes's Python environment. Tried: "
            + " | ".join(errors))

    def open(self, url: str) -> "Page":
        def go():
            self._ensure()
            blank = [p for p in self._context.pages if p.url in ("about:blank", "")]
            page = blank[0] if blank else self._context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            return page

        return Page(self, self._owner.call(go, timeout=90))

    def run(self, fn: Callable, timeout: float = 60.0) -> Any:
        return self._owner.call(fn, timeout=timeout)

    def shutdown(self) -> None:
        def stop():
            try:
                if self._browser is None and self._context is not None:
                    self._context.close()
            finally:
                self._context = self._browser = None
                if self._pw is not None:
                    self._pw.stop()
                    self._pw = None

        try:
            self._owner.call(stop, timeout=30)
        except Exception:
            pass


class Page:
    """One chat page. Every method runs on the browser's owner thread."""

    def __init__(self, browser: Browser, page) -> None:
        self._b = browser
        self._page = page

    def text(self) -> str:
        def read():
            parts = []
            for frame in self._page.frames:
                try:
                    parts.append(frame.locator("body").inner_text(timeout=3000))
                except Exception:
                    continue
            return "\n".join(parts)

        return self._b.run(read, timeout=30)

    def _find_composer(self):
        best = None
        for frame in self._page.frames:
            for selector in COMPOSERS:
                try:
                    loc = frame.locator(selector)
                    count = loc.count()
                except Exception:
                    continue
                for i in range(count):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible() or not el.is_editable():
                            continue
                        hint = " ".join(filter(None, (el.get_attribute("aria-label"), el.get_attribute("placeholder"),
                                                      el.get_attribute("name"), el.get_attribute("id"))))
                    except Exception:
                        continue
                    if COMPOSER_HINT.search(hint):
                        return el
                    best = best or el
        return best

    def find_composer(self, open_chat: bool = True, wait_s: float = 20.0) -> bool:
        """True once a message box is on the page; clicks a chat launcher when there is none."""

        def find():
            import time

            deadline = time.time() + wait_s
            clicked = False
            while time.time() < deadline:
                if self._find_composer() is not None:
                    return True
                if open_chat and not clicked:
                    clicked = self._click_launcher()
                self._page.wait_for_timeout(1000)
            return False

        return self._b.run(find, timeout=wait_s + 30)

    def _click_launcher(self) -> bool:
        for frame in self._page.frames:
            for role in ("button", "link"):
                try:
                    loc = frame.get_by_role(role, name=LAUNCHER)
                    if loc.count() and loc.first.is_visible():
                        loc.first.click(timeout=5000)
                        return True
                except Exception:
                    continue
        return False

    def send(self, message: str) -> None:
        def type_and_send():
            box = self._find_composer()
            if box is None:
                raise RuntimeError("The chat's message box is gone (the chat may have closed).")
            box.click(timeout=5000)
            box.fill(message, timeout=10000)
            box.press("Enter")
            self._page.wait_for_timeout(800)
            left = ""
            try:
                left = box.input_value(timeout=1000) if box.evaluate("e => 'value' in e") else box.inner_text(timeout=1000)
            except Exception:
                pass
            if left.strip():
                for f in self._page.frames:
                    btn = f.get_by_role("button", name=SEND)
                    if btn.count():
                        btn.first.click(timeout=5000)
                        break

        self._b.run(type_and_send, timeout=60)

    # --- signing in: the password goes from Hermes's vault into the field, nowhere else ---------

    def origin(self) -> str:
        def get():
            from urllib.parse import urlsplit

            u = urlsplit(self._page.url)
            return f"{u.scheme}://{u.netloc}"

        return self._b.run(get, timeout=10)

    def _visible(self, selector: str):
        loc = self._page.main_frame.locator(selector)
        for i in range(loc.count()):
            el = loc.nth(i)
            try:
                if el.is_visible() and el.is_editable():
                    return el
            except Exception:
                continue
        return None

    def password_visible(self) -> bool:
        return bool(self._b.run(lambda: self._visible('input[type="password"]') is not None, timeout=15))

    def fill_login(self, identifier: str, password: str) -> bool:
        """Types the identifier and password into the page's sign-in form and submits it."""

        def fill():
            pw = self._visible('input[type="password"]')
            if pw is None:
                return False
            if identifier:
                user = self._visible('input[autocomplete="username"], input[autocomplete="email"], input[type="email"], '
                                     'input[name*="user" i], input[name*="email" i], input[id*="email" i], input[type="text"]')
                if user is not None:
                    user.fill(identifier, timeout=10000)
            pw.fill(password, timeout=10000)
            pw.press("Enter")
            self._page.wait_for_timeout(1500)
            return True

        return bool(self._b.run(fill, timeout=60))

    def code_field_visible(self) -> bool:
        def find():
            return self._code_field() is not None

        return bool(self._b.run(find, timeout=15))

    def _code_field(self):
        el = self._visible('input[autocomplete="one-time-code"]')
        if el is not None:
            return el
        loc = self._page.main_frame.locator('input:not([type="password"]):not([type="hidden"])')
        for i in range(loc.count()):
            e = loc.nth(i)
            try:
                hint = " ".join(filter(None, (e.get_attribute("aria-label"), e.get_attribute("placeholder"),
                                              e.get_attribute("name"), e.get_attribute("id"))))
                if e.is_visible() and CODE_HINT.search(hint):
                    return e
            except Exception:
                continue
        return None

    def fill_code(self, code: str) -> bool:
        def fill():
            el = self._code_field()
            if el is None:
                return False
            el.fill(code, timeout=10000)
            el.press("Enter")
            self._page.wait_for_timeout(1500)
            return True

        return bool(self._b.run(fill, timeout=30))

    def screenshot(self, path: Path) -> Optional[str]:
        def shoot():
            self._page.screenshot(path=str(path))
            return str(path)

        try:
            return self._b.run(shoot, timeout=30)
        except Exception:
            return None

    def close(self) -> None:
        """Closes the tab; a visible browser keeps the finished chat on screen for the user."""
        if self._b.headed:
            return
        try:
            self._b.run(self._page.close, timeout=15)
        except Exception:
            pass
