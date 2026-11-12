"""The three phone tools the model calls: start a support call, follow it, end it."""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import handlers
from .bland import Bland, BlandError
from .phone import Bridge, CallSession

_calls: dict[str, CallSession] = {}
_bridge: Optional[Bridge] = None
_client: Optional[Bland] = None  # set by tests; otherwise a client per call, from the settings
_approved: dict[str, float] = {}  # call plans the user approved through Hermes's gate -> when
_approved_lock = threading.Lock()
APPROVAL_TTL_S = 1800.0


def _bland() -> Bland:
    """The bland.ai client. Its key is read per request through Hermes's profile-scoped secrets."""
    return _client or Bland(base_url=handlers.setting("bland_api_url"))


def _bridge_url() -> Optional[str]:
    """The public HTTPS address that reaches this machine's bridge (a tunnel or reverse proxy to the
    ``call_bridge_port`` setting). Without it the call can agree to nothing."""
    global _bridge
    url = str(handlers.setting("call_bridge_url", "") or "").strip()
    if not url:
        return None
    with handlers._lock:
        if _bridge is None:
            _bridge = Bridge(port=int(handlers.setting("call_bridge_port", 8788)))
    return url


def _progress(parent_agent):
    say = handlers._progress(parent_agent)
    return lambda message: say("📞 " + message)


def _call(session_id, parent_agent) -> CallSession:
    sid = re.sub(r"[^a-f0-9]", "", str(session_id or ""))
    with handlers._lock:
        live = _calls.get(sid)
    if live is None:
        root = handlers._root() / "calls"
        folder = next((f for f in root.glob(f"*-{sid}") if (f / "session.json").is_file()), None) if sid and root.is_dir() else None
        if folder is None:
            raise LookupError(f"No support call with id {session_id!r}.")
        live = CallSession.restore(folder)
        if live.closed:
            raise LookupError(f"Support call {sid} is already closed; its transcript is in {folder}.")
        with handlers._lock:
            _calls[sid] = live
        if live.bridged and _bridge_url():
            _bridge.calls[sid] = live
    live.progress = _progress(parent_agent)
    return live


def _wait_s(args: dict) -> float:
    try:
        return max(5.0, min(float(args.get("max_wait_s") or 240.0), 300.0))
    except (TypeError, ValueError):
        return 240.0


def _plan(args: dict) -> tuple[Optional[dict], Optional[str]]:
    """The call ``args`` would place, normalised, or the reason they are not a call."""
    number = re.sub(r"[^\d+]", "", str(args.get("phone_number") or ""))
    task = str(args.get("task") or "").strip()
    if not re.fullmatch(r"\+\d{8,15}", number):
        return None, "phone_number must be the support line in international form, e.g. +18005551234."
    if not task:
        return None, "task must say what the user wants from support, in their words."
    shareable = args.get("shareable") or {}
    never = args.get("never_share") or []
    if not isinstance(shareable, dict) or not isinstance(never, list):
        return None, "shareable must be an object of label -> value; never_share a list of strings."
    try:
        minutes = max(1, min(int(args.get("max_minutes") or 30), 120))
    except (TypeError, ValueError):
        minutes = 30
    return {"number": number, "company": str(args.get("company") or "").strip(), "task": task,
            "shareable": {str(k): str(v) for k, v in shareable.items()}, "never": [str(v) for v in never],
            "minutes": minutes, "record": bool(args.get("record"))}, None


def _fingerprint(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_kw):
    """A paid call is placed only after the user says yes at Hermes's own approval gate (CLI prompt,
    gateway buttons), which the model cannot skip and which fails closed when nobody can answer."""
    if tool_name != "support_call_start" or not isinstance(args, dict):
        return None
    plan, _ = _plan(args)
    if plan is None:
        return None  # the tool reports what is wrong; it places nothing
    question = (f"Place a phone call through bland.ai to {plan['company'] or 'a support line'} at {plan['number']}? "
                f"Up to {plan['minutes']} minutes, {'recorded' if plan['record'] else 'not recorded'}; "
                f"bland.ai bills the call to your account. Goal: {plan['task'][:200]}")
    if handlers._approvals_off():
        return {"action": "block", "message": json.dumps({
            "status": "needs_user", "question": question,
            "detail": "Not placed: a phone call needs the user's OK and Hermes approvals are turned off here, so "
                      "nobody can be asked. Ask the user, and place it only after they say yes."})}
    key = _fingerprint(plan)
    with _approved_lock:
        now = time.monotonic()
        for old in [k for k, at in _approved.items() if now - at > APPROVAL_TTL_S]:
            _approved.pop(old, None)
        _approved[key] = now
    # A fresh rule key per call: an "always" answer never waves through a later, different call.
    return {"action": "approve", "message": question, "rule_key": f"support_call:{key[:16]}:{time.time_ns()}"}


def _take_approval(plan: dict) -> bool:
    """True once for a plan that went to the user through ``pre_tool_call``: Hermes runs the tool only
    after they said yes there. A call reaching the tool any other way is not placed."""
    with _approved_lock:
        at = _approved.pop(_fingerprint(plan), None)
    return at is not None and time.monotonic() - at <= APPROVAL_TTL_S


def start(args: dict, parent_agent=None, **_kw) -> str:
    plan, error = _plan(args)
    if plan is None:
        return handlers._err(error)
    if not _take_approval(plan):
        return handlers._err("Not placed: a phone call needs the user's OK through Hermes's approval prompt, and "
                             "this call did not go through it. Ask the user before trying again.")
    number, minutes = plan["number"], plan["minutes"]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    call = CallSession(number, plan["company"], plan["task"], plan["shareable"], plan["never"], folder=Path(),
                       max_minutes=minutes, record=plan["record"])
    call.folder = handlers._root() / "calls" / f"{stamp}-{call.id}"
    call.progress = _progress(parent_agent)
    url = _bridge_url()
    if url:
        _bridge.calls[call.id] = call
    try:
        call.place(_bland(), url)
    except BlandError as exc:
        if _bridge is not None:
            _bridge.calls.pop(call.id, None)
        return handlers._err(str(exc))
    with handlers._lock:
        _calls[call.id] = call
    call.progress(f"Calling {call.company or number}…")
    out = call.follow(_bland(), handlers._asker(parent_agent), max_wait_s=_wait_s(args))
    out["session_id"] = call.id
    if not url:
        out["note"] = ("No approval bridge is set up (the call_bridge_url setting), so the call agrees to nothing: "
                       "it brings back any offer for the user to decide.")
    return handlers._ok(out)


def wait(args: dict, parent_agent=None, **_kw) -> str:
    try:
        call = _call(args.get("session_id"), parent_agent)
        return handlers._ok(call.follow(_bland(), handlers._asker(parent_agent), max_wait_s=_wait_s(args)))
    except Exception as exc:
        return handlers._err(str(exc))


def end(args: dict, parent_agent=None, **_kw) -> str:
    try:
        call = _call(args.get("session_id"), parent_agent)
    except Exception as exc:
        return handlers._err(str(exc))
    result = call.finish(_bland(), str(args.get("outcome") or "").strip() or "No outcome given.",
                         bool(args.get("resolved")))
    with handlers._lock:
        _calls.pop(call.id, None)
    if _bridge is not None:
        _bridge.calls.pop(call.id, None)
    call.progress(f"Call closed. Transcript: {result['transcript_file']}")
    return handlers._ok(result)


def shutdown() -> None:
    global _bridge
    with handlers._lock:
        for c in _calls.values():
            try:
                c.save()
            except Exception:
                pass
        _calls.clear()
        if _bridge is not None:
            _bridge.close()
            _bridge = None


START = {
    "name": "support_call_start",
    "description": (
        "Phone a company's customer-support line for the user through bland.ai. Use when the user wants "
        "customer service handled by phone (a refund, a cancellation, a billing error). An AI voice agent places "
        "the call, gets through the phone menu from the task alone (no key sequence needed), and talks to support; "
        "it shares only the `shareable` details. The user approves the call itself (number, company, length, "
        "recording) at Hermes's prompt before it is placed. Every offer or commitment comes back to the user for "
        "approval before the agent says yes: you do not decide and do not ask with clarify. Then call support_call_wait "
        "until the call ends, and support_call_end with the outcome. A call costs money: place one only when the "
        "user asked for a phone call."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "phone_number": {"type": "string", "description": "The support line, international form, e.g. +18005551234."},
            "company": {"type": "string", "description": "Company name, e.g. 'Northwind Outfitters'."},
            "task": {"type": "string", "description": "What the user wants, in their words."},
            "shareable": {"type": "object", "additionalProperties": {"type": "string"},
                          "description": "Details the user allows sharing, label -> value. The voice agent knows nothing else."},
            "never_share": {"type": "array", "items": {"type": "string"}, "description": "Values that must never be said on the call."},
            "max_minutes": {"type": "integer", "description": "Hang up after this many minutes, default 30, max 120."},
            "record": {"type": "boolean", "description": "Record the call (bland.ai keeps the audio). Only when the user asked; the agent tells support the call is recorded."},
            "max_wait_s": {"type": "number", "description": "How long this call follows the call before returning, default 240."},
        },
        "required": ["phone_number", "task"],
    },
}

WAIT = {
    "name": "support_call_wait",
    "description": "Keep following a support call: returns what support said, asks the user about any offer, and "
                   "returns call_ended with the summary when the call is over (or on_call after max_wait_s: call again).",
    "parameters": {
        "type": "object",
        "properties": {"session_id": {"type": "string"}, "max_wait_s": {"type": "number", "description": "Default 240, max 300."}},
        "required": ["session_id"],
    },
}

END = {
    "name": "support_call_end",
    "description": "Hang up if still on the line and save the call's transcript and outcome. Then tell the user the "
                   "outcome with any reference number, and where the transcript is.",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "outcome": {"type": "string", "description": "What was agreed, with amounts and reference numbers."},
            "resolved": {"type": "boolean", "description": "Was the user's goal met?"},
        },
        "required": ["session_id", "outcome", "resolved"],
    },
}

TOOLS = [(START, start, "📞"), (WAIT, wait, "⏳"), (END, end, "✅")]
