"""The three phone tools the model calls: start a support call, follow it, end it."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from . import handlers
from .bland import Bland, BlandError
from .phone import Bridge, CallSession

_calls: dict[str, CallSession] = {}
_bridge: Optional[Bridge] = None
_client: Optional[Bland] = None


def _bland() -> Bland:
    global _client
    if _client is None:
        _client = Bland()
    return _client


def _bridge_url() -> Optional[str]:
    """The public HTTPS address that reaches this machine's bridge (a tunnel or reverse proxy to
    ``SUPPORT_CALL_BRIDGE_PORT``). Without it the call can agree to nothing."""
    global _bridge
    url = os.environ.get("SUPPORT_CALL_BRIDGE_URL", "").strip()
    if not url:
        return None
    with handlers._lock:
        if _bridge is None:
            _bridge = Bridge(port=int(os.environ.get("SUPPORT_CALL_BRIDGE_PORT") or 8788))
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


def start(args: dict, parent_agent=None, **_kw) -> str:
    number = re.sub(r"[^\d+]", "", str(args.get("phone_number") or ""))
    task = str(args.get("task") or "").strip()
    if not re.fullmatch(r"\+\d{8,15}", number):
        return handlers._err("phone_number must be the support line in international form, e.g. +18005551234.")
    if not task:
        return handlers._err("task must say what the user wants from support, in their words.")
    shareable = args.get("shareable") or {}
    never = args.get("never_share") or []
    if not isinstance(shareable, dict) or not isinstance(never, list):
        return handlers._err("shareable must be an object of label -> value; never_share a list of strings.")
    try:
        minutes = max(1, min(int(args.get("max_minutes") or 30), 120))
    except (TypeError, ValueError):
        minutes = 30
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    call = CallSession(number, str(args.get("company") or "").strip(), task,
                       {str(k): str(v) for k, v in shareable.items()}, [str(v) for v in never], folder=Path(),
                       max_minutes=minutes)
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
        out["note"] = ("No approval bridge is set up (SUPPORT_CALL_BRIDGE_URL), so the call agrees to nothing: "
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
        "it shares only the `shareable` details. Every offer or commitment comes back to the user for approval "
        "before the agent says yes: you do not decide and do not ask with clarify. Then call support_call_wait "
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
