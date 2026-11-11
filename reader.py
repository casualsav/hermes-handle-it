"""Reads a chat out of whatever the page shows, without knowing the widget.

A support chat only ever grows, so each read is the page's visible text as
lines, compared with the read before it: lines that appeared are new. Our own
messages, the typing indicator and short speaker labels are recognised so the
user sees "Dana: Hi, how can I help?" rather than raw page text.
"""

from __future__ import annotations

import difflib
import re

TYPING = re.compile(r"\b(is typing|are typing|typing\s*(?:…|\.\.\.)|is writing|composing)\b", re.I)
ENDED = re.compile(
    r"(has left the chat|left the conversation|chat (?:has )?ended|conversation (?:has )?ended|"
    r"chat (?:is |has been )?closed|ended the chat|session (?:has )?ended)",
    re.I,
)
SYSTEM = re.compile(r"\b(joined|left) the (chat|conversation)\b", re.I)
OWN_LABELS = {"you", "me", "customer", "visitor", "guest"}
UI_WORDS = {"send", "submit", "sign in", "log in", "start chat", "chat", "close", "minimize", "attach"}
TIMESTAMP = re.compile(r"^\s*(\d{1,2}:\d{2}(\s?[ap]m)?|just now|now|\d+\s?(s|sec|m|min|mins|h)\s?ago)\s*$", re.I)


def lines_of(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


def appeared(before: list[str], after: list[str]) -> list[str]:
    """Lines present in ``after`` that were not in ``before`` (insertions and replacements)."""
    out: list[str] = []
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("insert", "replace"):
            out.extend(after[j1:j2])
    return out


def _is_own(line: str, sent: list[str]) -> bool:
    low = line.lower()
    if low in OWN_LABELS:
        return True
    for message in sent:
        m = message.lower()
        if low == m or (len(low) >= 8 and low in m):
            return True
    return False


def _is_label(line: str) -> bool:
    return len(line) <= 40 and len(line.split()) <= 4 and not re.search(r"[.?!,:;]$", line) and line[:1].isupper()


def incoming(new_lines: list[str], sent: list[str]) -> list[str]:
    """The support side's new messages: our own text, typing indicators and timestamps dropped,
    a speaker label joined to the message under it ("Dana" + "Hi" -> "Dana: Hi")."""
    kept = [ln for ln in new_lines if not _is_own(ln, sent) and not TYPING.search(ln) and not TIMESTAMP.match(ln)
            and ln.lower() not in UI_WORDS]
    out: list[str] = []
    i = 0
    while i < len(kept):
        line = kept[i]
        if i + 1 < len(kept) and _is_label(line) and not _is_label(kept[i + 1]) and not SYSTEM.search(kept[i + 1]):
            out.append(f"{line}: {kept[i + 1]}")
            i += 2
            continue
        out.append(line)
        i += 1
    return out


def typing_now(lines: list[str]) -> bool:
    return any(TYPING.search(ln) for ln in lines)


def ended(lines: list[str]) -> bool:
    return any(ENDED.search(ln) for ln in lines)
