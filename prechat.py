"""Which shareable details go into which field of a chat's pre-chat form. Pure Python, no browser.

Fields come from ``browser.Page.prechat_fields``; a field is filled only when a shareable detail fits
its label and its type, so nothing the user did not list as shareable is ever typed into a form.
"""

from __future__ import annotations

import re

YES = frozenset({"yes", "true", "agree", "accept", "on", "1"})
FILLER = frozenset({"your", "the", "please", "enter", "address", "full", "number"})
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

_NAME = frozenset({"name", "first", "given"})
_LAST = frozenset({"last", "surname", "family"})
_MAIL = frozenset({"email", "mail"})
_PHONE = frozenset({"phone", "telephone", "mobile", "tel"})
_ORDER = frozenset({"order", "id", "no", "ref", "reference"})
_TOPIC = frozenset({"topic", "subject", "department", "reason", "category"})


def yes_like(value: str) -> bool:
    return str(value).strip().lower() in YES


def _words(text: str) -> list[str]:
    text = str(text).lower().replace("e-mail", "email")
    return [w for w in re.split(r"[\W_]+", text) if w and w not in FILLER]


def _kind(text: str) -> str:
    """What a label or a shareable key asks for: a family name for the usual details, else its words."""
    words = _words(text)
    have = set(words)
    joined = " ".join(words)
    if have & _LAST and have & {"name", "surname"}:
        return "last name"
    if have & _MAIL:
        return "email"
    if have & _PHONE:
        return "phone"
    if have & _TOPIC or "how can we help" in joined:
        return "topic"
    if "name" in have and have <= _NAME:
        return "name"
    if "order" in have and have <= _ORDER:
        return "order"
    return joined


def _fits(field_type: str, value: str, options: list[str]) -> bool:
    value = value.strip()
    if not value:
        return False
    if field_type == "email":
        return bool(EMAIL.fullmatch(value))
    if field_type == "tel":
        return len(re.sub(r"\D", "", value)) >= 7
    if field_type == "select":
        return any(value.lower() == o.strip().lower() for o in options)
    if field_type == "checkbox":
        return yes_like(value)
    return True


def plan(fields: list[dict], shareable: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """``(values, missing)``: ``values`` maps a field's label to the shareable value that fits it;
    ``missing`` lists the labels of required fields nothing on the shareable list fits."""
    details = [(_kind(k), str(v)) for k, v in shareable.items() if _kind(k)]
    used: dict[int, str] = {}  # detail -> the label it filled; one value fills one field unless labels are identical
    values: dict[str, str] = {}
    missing: list[str] = []
    for field in fields:
        label = str(field.get("label") or "")
        if label in values:
            continue
        field_type = str(field.get("type") or "text")
        options = [str(o) for o in field.get("options") or []]
        kind = _kind(label)
        chosen = next((i for i, (k, v) in enumerate(details)
                       if kind and k == kind and used.get(i, label) == label and _fits(field_type, v, options)), None)
        if chosen is not None:
            used[chosen] = label
            values[label] = details[chosen][1]
        elif field.get("required"):
            entry = f"{label} (one of: {', '.join(options)})" if field_type == "select" and options else label
            if entry not in missing:
                missing.append(entry)
    return values, missing
