"""Reads a chat out of whatever the page shows, without knowing the widget.

A support chat only ever grows, so each read is the page's visible text as
lines, compared with the read before it: lines that appeared are new. Our own
messages, the typing indicator and short speaker labels are recognised so the
user sees "Dana: Hi, how can I help?" rather than raw page text.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

# The whole line is the indicator: "Dana is typing…", "typing...". A message that mentions writing is not.
TYPING = re.compile(
    r"(?:[\w .'’&-]{1,40}\s)?(?:is|are)\s+(?:typing|writing)(?:\s+a message)?\s*(?:…|\.\.\.)?|"
    r"(?:typing|composing)\s*(?:…|\.\.\.)?",
    re.I,
)
TYPING_MAX = 60
# An agent leaving is not the end: another may join. Only a closing said on a short line ends the chat.
ENDED = re.compile(
    r"\b(?:chat|conversation|session) (?:has (?:been )?|was |is (?:now )?)?(?:ended|closed)\b|"
    r"\bended the (?:chat|conversation|session)\b",
    re.I,
)
ENDED_MAX = 80
LEFT = re.compile(r"\bleft the (?:chat|conversation)\b", re.I)
JOINED = re.compile(r"\bjoined the (?:chat|conversation)\b", re.I)
SYSTEM = re.compile(r"\b(joined|left) the (chat|conversation)\b", re.I)
OWN_LABELS = {"you", "me", "customer", "visitor", "guest"}
UI_WORDS = {"send", "submit", "sign in", "log in", "start chat", "chat", "close", "minimize", "attach"}
_TIME = r"(?:\d{1,2}:\d{2}(?::\d{2})?(?:\s?[ap]\.?m\.?)?|just now|now|\d+\s?(?:s|sec|m|min|mins|h)\s?ago)"
TIMESTAMP = re.compile(rf"^\s*{_TIME}\s*$", re.I)
RECEIPT = re.compile(rf"^\s*(?:seen|delivered|read|sent)(?:\s*[·•|,-]?\s*(?:at\s+)?{_TIME})?\s*$", re.I)
_OWN_LABEL = re.compile(r"^(?:%s)\s*[:-]\s*" % "|".join(sorted(OWN_LABELS)), re.I)
_LEAD_TIME = re.compile(rf"^\[?{_TIME}\]?\s*[-·|]?\s*", re.I)
_TRAIL_TIME = re.compile(rf"\s*[-·|]?\s*\[?{_TIME}\]?$", re.I)
_QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"'})


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


def _plain(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).translate(_QUOTES)
    return " ".join(text.split()).lower()


def _is_own(line: str, sent: list[str]) -> bool:
    """Our own message as the page shows it, maybe with "You:" or a time around it."""
    if line.lower() in OWN_LABELS:
        return True
    rest = _LEAD_TIME.sub("", line.strip(), count=1)
    labelled = _OWN_LABEL.match(rest)
    rest = _TRAIL_TIME.sub("", rest[labelled.end():] if labelled else rest, count=1)
    low = _plain(rest)
    if not low:
        return bool(labelled)
    for message in sent:
        m = _plain(message)
        if low == m or (len(low) >= 8 and low in m):
            return True
    return False


def _is_typing(line: str) -> bool:
    return len(line) <= TYPING_MAX and bool(TYPING.fullmatch(line.strip()))


def _is_label(line: str) -> bool:
    return len(line) <= 40 and len(line.split()) <= 4 and not re.search(r"[.?!,:;]$", line) and line[:1].isupper()


def incoming(new_lines: list[str], sent: list[str]) -> list[str]:
    """The support side's new messages: our own text, typing indicators, timestamps and delivery receipts
    dropped, a speaker label joined to the message under it ("Dana" + "Hi" -> "Dana: Hi")."""
    kept = [ln for ln in new_lines if not _is_own(ln, sent) and not _is_typing(ln) and not TIMESTAMP.match(ln)
            and not RECEIPT.match(ln) and ln.lower() not in UI_WORDS]
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
    return any(_is_typing(ln) for ln in lines)


def ended(lines: list[str]) -> bool:
    return any(len(ln) <= ENDED_MAX and ENDED.search(ln) for ln in lines)


def agent_left(lines: list[str]) -> bool:
    """An agent left the chat and nobody joined after."""
    left = [i for i, ln in enumerate(lines) if len(ln) <= ENDED_MAX and LEFT.search(ln)]
    return bool(left) and not any(JOINED.search(ln) for ln in lines[left[-1] + 1:])
