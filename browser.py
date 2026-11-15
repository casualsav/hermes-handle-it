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
import sys
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Callable, Optional

from . import netguard, prechat

COMPOSERS = ("textarea", '[contenteditable="true"]', '[contenteditable=""]', '[contenteditable="plaintext-only"]',
             '[role="textbox"]', 'input[type="text"]', "input:not([type])")
COMPOSER_HINT = re.compile(r"message|type|chat|reply|write|ask|question", re.I)
# A short button or link name that speaks of a chat and is not a close, minimise, end or transcript control.
# Playwright hands the pattern to the page's JavaScript, so it uses only syntax both languages read alike.
_NOT_LAUNCHER = r"close|minimi[sz]e|\bend\b|hide|leave|history|download|transcript|e-?mail"
LAUNCHER = re.compile(
    rf"^(?!.*({_NOT_LAUNCHER}))(?=.{{1,60}}$)"
    r".*(\b(live)?chat|messenger|messaging|message us|contact support|talk to|get help)", re.I)
# Consent banners are only ever refused: agreeing to tracking is not the plugin's to give.
REFUSE_CONSENT = re.compile(
    r"^\s*((i )?(reject|decline|deny|refuse)( all)?( cookies)?|(necessary|essential)( cookies)? only"
    r"|only necessary( cookies)?|use necessary cookies only)\s*$", re.I)
CONSENT_TRIES = 3
# Chat bubbles load seconds after the page does; until then a launcher found by its name alone is more often the
# page's own text about chats (a product link that leaves the page) than the chat, so it waits this long.
NAMED_LAUNCHER_AFTER_S = 8.0
# A frame that is a chat widget by its address or name; a form in any other place must speak of a chat itself.
WIDGET_FRAME = re.compile(r"chat|messag|messenger|conversation", re.I)
# What else sits in a page's bottom corner and may mention a chat in passing; a corner bubble near any of these
# words is not clicked. JavaScript reads it too.
CORNER_SKIP = (_NOT_LAUNCHER + r"|cookie|consent|gdpr|privacy|to.?top\b|scroll|newsletter|subscri|promo|coupon"
               r"|discount|video|player|accessib|a11y|feedback|survey|whatsapp|wechat|snapchat|telegram")
# A corner bubble is a chat's when something on it says so: WIDGET_FRAME's words, a launcher, or Chaport's
# own class prefix (its bubble says nothing else).
CORNER_CHAT = WIDGET_FRAME.pattern + r"|launcher|chaport"
SEND = re.compile(r"^\s*send\b|^submit$", re.I)

# Per element: the words that name it (attributes and label text), its tag, how many other visible fields
# share its <form>, and its _PRECHAT mark; a field among others is a form's field, not a chat's message box.
_COMPOSER_INFO = r"""e => {
  const words = [e.getAttribute('aria-label'), e.getAttribute('placeholder'), e.getAttribute('name'), e.id];
  for (const label of e.labels || []) words.push(label.innerText);
  for (const id of (e.getAttribute('aria-labelledby') || '').split(/\s+/)) {
    const label = id && e.getRootNode().getElementById(id);
    if (label) words.push(label.innerText);
  }
  const form = e.closest('form');
  const others = form ? Array.from(form.querySelectorAll('input, select, textarea')).filter((x) => x !== e &&
    x.getClientRects().length > 0 && !/^(hidden|submit|button|reset|image|file|checkbox|radio)$/i.test(x.type || '')).length : 0;
  return {tag: e.tagName.toLowerCase(), hint: words.filter(Boolean).join(' '), others,
          mark: e.getAttribute('data-handle-it-field')};
}"""

# The body's text, then the text of each open shadow root under it (depth first, document order). Nothing from a
# frame that is not rendered (a display:none analytics frame): there innerText is textContent, scripts included.
_READ_TEXT = r"""body => {
  if (!body.getClientRects().length) return '';
  const parts = [body.innerText];
  const skip = /^(STYLE|SCRIPT|TEMPLATE|LINK|SLOT|NOSCRIPT)$/;
  const walk = (root) => {
    for (const host of root.querySelectorAll('*')) {
      if (!host.shadowRoot) continue;
      const text = [];
      for (const node of host.shadowRoot.childNodes) {
        if (node.nodeType === 3 && host.getClientRects().length && node.textContent.trim()) text.push(node.textContent.trim());
        if (node.nodeType === 1 && !skip.test(node.tagName) && node.getClientRects().length) text.push(node.innerText);
      }
      const chunk = text.join('\n');
      if (chunk.trim() && !parts.join('\n').includes(chunk)) parts.push(chunk);
      walk(host.shadowRoot);
    }
  };
  walk(body);
  return parts.join('\n');
}"""

# The pre-chat form groups of one frame: visible fields that share a <form> (or the nearest container) with a
# visible submit control named like "Start chat". Each field and submit control gets a data-handle-it-* mark so
# Playwright can find it again in the same call. ``chat`` says the form or its button speaks of a chat.
_PRECHAT = r"""() => {
  const SUBMIT = /^\W*(start (the |a )?chat(ting)?|begin (the |a )?chat|start (a |the )?conversation|chat now|continue|next|submit|send)\W*$/i;
  const IGNORE = /search|newsletter|subscribe|password|coupon/i;
  const SIGN_IN = /\b(username|current-password|new-password|one-time-code)\b/i;
  const deep = (root, sel, out = []) => {
    out.push(...root.querySelectorAll(sel));
    for (const el of root.querySelectorAll('*')) if (el.shadowRoot) deep(el.shadowRoot, sel, out);
    return out;
  };
  const visible = (e) => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== 'hidden';
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const kind = (e) => {
    const tag = e.tagName.toLowerCase();
    if (tag === 'textarea' || tag === 'select') return tag;
    const t = (e.getAttribute('type') || 'text').toLowerCase();
    if (t === 'email' || t === 'tel' || t === 'checkbox') return t;
    return (t === 'text' || t === 'number' || t === 'url') ? 'text' : null;
  };
  const labelOf = (e) => {
    let text = e.getAttribute('aria-label');
    if (!text && e.labels && e.labels.length) text = e.labels[0].innerText;
    if (!text) text = (e.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
      .map((id) => { const l = e.getRootNode().getElementById(id); return l ? l.innerText : ''; }).join(' ');
    return clean(text || e.getAttribute('placeholder') || e.getAttribute('name') || e.id || kind(e));
  };
  const fields = deep(document, 'input, textarea, select').filter((e) => kind(e) && !e.disabled && !e.readOnly &&
    visible(e) && !IGNORE.test([labelOf(e), e.getAttribute('name'), e.id, e.getAttribute('placeholder')].join(' ')));
  if (!fields.length) return [];
  const groups = [];
  for (const b of deep(document, 'button, input[type="submit"], input[type="button"], [role="button"]')) {
    if (!visible(b) || b.disabled || !SUBMIT.test(clean(b.getAttribute('aria-label') || b.innerText || b.value))) continue;
    let box = b.form || b.closest('form');
    if (!box) {
      box = b.parentElement;
      while (box && !fields.some((f) => box.contains(f))) box = box.parentElement;
      if (box && (box === document.body || box === document.documentElement) && window.top === window) continue;
    }
    if (!box || groups.some((g) => g.box === box)) continue;
    const own = fields.filter((f) => f.form === box || box.contains(f));
    const signIn = deep(box, 'input').some((i) => visible(i) &&
      (i.type === 'password' || SIGN_IN.test(i.getAttribute('autocomplete') || '')));
    if (own.length && !signIn) groups.push({box, submit: b, fields: own});
  }
  for (const e of deep(document, '[data-handle-it-field], [data-handle-it-submit]')) {
    e.removeAttribute('data-handle-it-field');
    e.removeAttribute('data-handle-it-submit');
  }
  const marks = new Map();
  return groups.map((g, gi) => {
    g.submit.setAttribute('data-handle-it-submit', String(gi));
    const named = clean(g.submit.getAttribute('aria-label') || g.submit.innerText || g.submit.value);
    return {submit: gi, chat: /chat|conversation/i.test((g.box.innerText || '') + ' ' + named), fields: g.fields.map((f) => {
      if (!marks.has(f)) { marks.set(f, marks.size); f.setAttribute('data-handle-it-field', String(marks.get(f))); }
      const label = labelOf(f);
      const options = f.tagName === 'SELECT' ? Array.from(f.options).filter((o) => !o.disabled && !o.hidden && o.value !== '')
        .map((o) => clean(o.label || o.text)).filter(Boolean) : [];
      return {mark: marks.get(f), label: label.replace(/\s*\*\s*$/, ''), type: kind(f), options,
              required: !!f.required || f.getAttribute('aria-required') === 'true' || /\*\s*$/.test(label)};
    })};
  });
}"""

# The page's corner chat bubble, which often has no name to find it by: a small element (or frame) in a bottom
# corner, fixed or inside something fixed, on top where it sits, that it or a container up to the fixed one marks
# as a chat by id, class, title, alt, label, address or its own short text; nothing on that way may be a CORNER_SKIP
# control or a link that leaves the page (a WhatsApp or phone button). Marks it data-handle-it-launcher and says
# whether it is a frame.
_CORNER_LAUNCHER = r"""(body, [chat, skip]) => {
  chat = new RegExp(chat, 'i'); skip = new RegExp(skip, 'i');
  const up = (e) => e.parentElement || (e.getRootNode() instanceof ShadowRoot ? e.getRootNode().host : null);
  const deep = (root, out = []) => {
    for (const e of root.querySelectorAll('*')) { out.push(e); if (e.shadowRoot) deep(e.shadowRoot, out); }
    return out;
  };
  const all = deep(document);
  for (const e of all) e.removeAttribute('data-handle-it-launcher');
  const W = innerWidth, H = innerHeight;
  for (const e of all) {
    const r = e.getBoundingClientRect();
    if (r.width < 16 || r.height < 16 || r.width > 320 || r.height > 160 || r.top < H / 2 || r.bottom > H + 1 ||
        r.bottom < H - 200 || (r.left > 200 && r.right < W - 200)) continue;
    let hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
    while (hit && hit.shadowRoot) {
      const inner = hit.shadowRoot.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      if (!inner || inner === hit) break;
      hit = inner;
    }
    while (hit && hit !== e) hit = up(hit);
    if (!hit) continue;
    const short = (e.innerText || '').trim();
    const words = [short.length <= 60 ? short : ''];
    let fixed = false, leaves = false;
    for (let a = e; a && !fixed; a = up(a)) {
      for (const n of ['id', 'class', 'title', 'alt', 'aria-label', 'src', 'href', 'name']) words.push(a.getAttribute(n) || '');
      leaves = leaves || (a.tagName === 'A' && !/^\s*(#|javascript:|$)/i.test(a.getAttribute('href') || ''));
      fixed = /^(fixed|sticky)$/.test(getComputedStyle(a).position);
    }
    const said = words.join(' ');
    if (!fixed || leaves || skip.test(said) || !chat.test(said)) continue;
    e.setAttribute('data-handle-it-launcher', '');
    return e.tagName === 'IFRAME' ? 'frame' : 'element';
  }
  return null;
}"""


class BrowserUnavailable(RuntimeError):
    pass


class Refused(RuntimeError):
    """The page, a redirect hop or a frame of it is an address the browser may not open."""


class _Guard:
    """Checks every document load of one page before the browser sends it: the page itself, each
    redirect hop and each frame. Chromium pauses each hop as its own request here (Playwright's
    ``route`` never sees redirects), and a refused one fails with ERR_BLOCKED_BY_CLIENT."""

    def __init__(self, context, page) -> None:
        self.refused: list[str] = []
        self._cdp = context.new_cdp_session(page)
        self._cdp.on("Fetch.requestPaused", self._paused)
        self._cdp.send("Fetch.enable", {"patterns": [
            {"urlPattern": "*", "resourceType": "Document", "requestStage": "Request"}]})

    def _paused(self, event: dict) -> None:
        try:
            why = netguard.refusal(str((event.get("request") or {}).get("url") or ""))
        except Exception as exc:  # fail closed: a request is never left paused or let through unchecked
            why = f"an address the guard could not check ({type(exc).__name__})"
        try:
            if why:
                self.refused.append(why)
                self._cdp.send("Fetch.failRequest", {"requestId": event["requestId"], "errorReason": "BlockedByClient"})
            else:
                self._cdp.send("Fetch.continueRequest", {"requestId": event["requestId"]})
        except Exception:
            pass  # the page or its target went away


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
    default = Path.home() / ("Library/Caches" if sys.platform == "darwin" else ".cache") / "ms-playwright"
    cache = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or default)
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
        self._guards: dict = {}
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
        """Opens ``url`` in a guarded tab. Raises ``Refused`` when the address, any redirect hop or a
        frame of the page is private, loopback, link-local or a cloud metadata endpoint."""
        why = netguard.refusal(url)
        if why:
            raise Refused(f"Not opening {url}: {why}.")

        def go():
            self._ensure()
            blank = [p for p in self._context.pages if p.url in ("about:blank", "")]
            page = blank[0] if blank else self._context.new_page()
            guard = self._guards.get(page)
            if guard is None:
                guard = self._guards[page] = _Guard(self._context, page)
                page.on("close", lambda closed: self._guards.pop(closed, None))
            guard.refused.clear()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                if not guard.refused:
                    raise
            if guard.refused:
                why = guard.refused[-1]
                try:
                    page.goto("about:blank")
                except Exception:
                    pass
                raise Refused(f"Stopped loading {url}: it leads to {why}.")
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
                self._guards.clear()
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

    def _readable(self, frame) -> bool:
        """False for a frame the guard would not have loaded; such a frame is never read or filled."""
        try:
            return not netguard.refusal(frame.url)
        except Exception:
            return False

    def text(self) -> str:
        """Each frame's text: its body, then any open shadow roots (closed ones and canvas stay unreadable)."""

        def read():
            parts = []
            for frame in self._page.frames:
                if not self._readable(frame):
                    continue
                body = frame.locator("body")
                try:
                    got = body.evaluate(_READ_TEXT, timeout=3000)
                    if not isinstance(got, str):
                        raise TypeError("no text")
                    parts.append(got)
                    continue
                except Exception as exc:
                    if type(exc).__name__ == "TimeoutError":
                        continue
                try:
                    parts.append(body.inner_text(timeout=3000))
                except Exception:
                    continue
            return "\n".join(parts)

        return self._b.run(read, timeout=30)

    def _find_composer(self):
        best = None
        for frame in self._page.frames:
            # Fields grouped with others around a submit control, with or without a <form>: a contact form's
            # message box, not a chat's
            crowded = set()
            if self._readable(frame):
                try:
                    groups = frame.locator("body").evaluate(_PRECHAT, timeout=3000) or []
                    crowded = {str(f["mark"]) for g in groups if len(g.get("fields") or []) > 1 for f in g["fields"]}
                except Exception:
                    pass
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
                        info = el.evaluate(_COMPOSER_INFO, timeout=2000)
                    except Exception:
                        continue
                    if info.get("others") or info.get("mark") in crowded:
                        continue
                    if COMPOSER_HINT.search(info.get("hint") or ""):
                        return el
                    if info.get("tag") != "input":  # an unnamed <input> is a form field far more often than a chat box
                        best = best or el
        return best

    def find_composer(self, open_chat: bool = True, wait_s: float = 20.0) -> bool:
        """True once a message box is on the page; clicks a chat launcher when there is none, after refusing
        any cookie banner that may cover it."""

        def find():
            import time

            start = time.time()
            deadline = start + wait_s
            clicked = False
            refused = 0
            while time.time() < deadline:
                if self._find_composer() is not None:
                    return True
                if clicked and self._prechat_groups():
                    return False  # the chat opened on a pre-chat form: the caller fills it
                if open_chat and not clicked:
                    if refused < CONSENT_TRIES and self._refuse_consent():
                        refused += 1
                        self._page.wait_for_timeout(500)
                    clicked = self._click_launcher(named=time.time() - start >= NAMED_LAUNCHER_AFTER_S)
                self._page.wait_for_timeout(1000)
            return False

        return self._b.run(find, timeout=wait_s + 30)

    def _first_visible(self, role: str, name: "re.Pattern"):
        for frame in self._page.frames:
            try:
                loc = frame.get_by_role(role, name=name)
                for i in range(min(loc.count(), 10)):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:
                continue
        return None

    def _refuse_consent(self) -> bool:
        button = self._first_visible("button", REFUSE_CONSENT)
        if button is None:
            return False
        try:
            button.click(timeout=5000)
            return True
        except Exception:
            return False

    def _click_corner_launcher(self) -> bool:
        main = self._page.main_frame
        if not self._readable(main):
            return False
        try:
            kind = main.locator("body").evaluate(_CORNER_LAUNCHER, [CORNER_CHAT, CORNER_SKIP], timeout=3000)
            if not kind:
                return False
            target = main.locator("[data-handle-it-launcher]").first
            if kind == "frame":  # clicked inside the bubble's own frame, and only one the guard would have loaded
                frame = target.element_handle(timeout=2000).content_frame()
                if frame is None or not self._readable(frame):
                    return False
                button = frame.get_by_role("button").first
                target = button if button.is_visible() else frame.locator("body")
            target.click(timeout=5000)
            return True
        except Exception:
            return False

    def _click_launcher(self, named: bool) -> bool:
        # The corner bubble first: a page's own text also speaks of chats (an FAQ entry, a "Live chat" product
        # link), and the first named match is often that and not the chat.
        if self._click_corner_launcher():
            return True
        if not named:
            return False
        for frame in self._page.frames:
            for role in ("button", "link"):
                try:
                    loc = frame.get_by_role(role, name=LAUNCHER)
                    for i in range(min(loc.count(), 10)):
                        if loc.nth(i).is_visible():
                            loc.nth(i).click(timeout=5000)
                            return True
                except Exception:
                    continue
        return False

    @staticmethod
    def _holds(box, message: str) -> bool:
        """True while the message box still holds ``message``."""
        want = " ".join(message.split())
        if not want:
            return False
        try:
            left = box.input_value(timeout=1000) if box.evaluate("e => 'value' in e", timeout=1000) \
                else box.inner_text(timeout=1000)
        except Exception:
            return False
        return want in " ".join(left.split())

    def send(self, message: str) -> None:
        def type_and_send():
            box = self._find_composer()
            if box is None:
                raise RuntimeError("The chat's message box is gone (the chat may have closed).")
            box.click(timeout=5000)
            try:
                box.fill(message, timeout=10000)
            except Exception:  # some rich editors refuse fill; typing reaches them
                box.click(timeout=5000)
                box.press_sequentially(message, timeout=30000)
            box.press("Enter")
            self._page.wait_for_timeout(800)
            if not self._holds(box, message):
                return
            for f in self._page.frames:
                try:
                    btn = f.get_by_role("button", name=SEND)
                    if btn.count():
                        btn.first.click(timeout=5000)
                        self._page.wait_for_timeout(800)
                        break
                except Exception:
                    continue
            if self._holds(box, message):
                raise RuntimeError("The message did not send: it is still in the chat's message box, and neither "
                                   "Enter nor a Send button sent it.")

        self._b.run(type_and_send, timeout=60)

    def _prechat_groups(self) -> list:
        """``(frame, group)`` for every pre-chat form group, best first: inside an iframe, then mentioning chat.
        Only a form that speaks of a chat, or sits in a chat widget's frame, counts: a contact or newsletter
        form on the same page is never filled."""
        found = []
        main = self._page.main_frame
        for frame in self._page.frames:
            if not self._readable(frame):
                continue
            try:
                groups = frame.locator("body").evaluate(_PRECHAT, timeout=3000)
            except Exception:
                continue
            widget = frame is not main and bool(WIDGET_FRAME.search(f"{frame.url} {frame.name}"))
            found.extend((frame, g) for g in groups or [] if g.get("fields") and (g.get("chat") or widget))
        found.sort(key=lambda fg: (fg[0] is main, not fg[1].get("chat")))
        return found

    def prechat_fields(self) -> list[dict]:
        """The visible pre-chat form's fields, ``[]`` when there is none (or the message box is already there).
        Each: ``{"label", "type", "required", "options"}``."""

        def read():
            if self._find_composer() is not None:
                return []
            groups = self._prechat_groups()
            if not groups:
                return []
            return [{"label": str(f.get("label") or ""), "type": str(f.get("type") or "text"),
                     "required": bool(f.get("required")), "options": [str(o) for o in f.get("options") or []]}
                    for f in groups[0][1]["fields"]]

        return self._b.run(read, timeout=60)

    def fill_prechat(self, values: dict[str, str]) -> bool:
        """Fills the fields whose label is a key of ``values`` and presses the form's submit button; True
        when it was pressed."""

        def fill():
            if self._find_composer() is not None:
                return False
            groups = self._prechat_groups()
            if not groups:
                return False
            frame, group = groups[0]
            for field in group["fields"]:
                label = str(field.get("label") or "")
                if label not in values:
                    continue
                value = str(values[label])
                el = frame.locator(f'[data-handle-it-field="{int(field["mark"])}"]').first
                try:
                    if field.get("type") == "select":
                        choice = next((o for o in field.get("options") or [] if o.lower() == value.strip().lower()), None)
                        if choice is not None:
                            el.select_option(label=choice, timeout=5000)
                    elif field.get("type") == "checkbox":
                        if prechat.yes_like(value):
                            el.check(timeout=5000)
                    else:
                        el.fill(value, timeout=5000)
                except Exception:
                    continue
            try:
                frame.locator(f'[data-handle-it-submit="{int(group["submit"])}"]').first.click(timeout=5000)
                return True
            except Exception:
                return False

        return self._b.run(fill, timeout=90)

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
