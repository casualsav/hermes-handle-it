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
from .guard import _norm
from .phone import END_CALL, NO, YES, Bridge, CallSession, _question, _same
from .session import _question as _reply_question

_calls: dict[str, CallSession] = {}
_owners: dict[str, Optional[str]] = {}  # call id -> the Hermes session that holds it (None: unknown)
_bridge: Optional[Bridge] = None
_client: Optional[Bland] = None  # set by tests; otherwise a client per call, from the settings
_approved: dict[str, float] = {}  # call plans and answers the user approved through Hermes's gate -> when
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


def _progress(hermes_session):
    say = handlers._progress(hermes_session)
    return lambda message: say("📞 " + message)


def _call(session_id, hermes_session) -> CallSession:
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
    if hermes_session is not None:
        with handlers._lock:
            _owners[sid] = hermes_session
    live.progress = _progress(hermes_session)
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
    plan = {"number": number, "company": str(args.get("company") or "").strip(), "task": task,
            "shareable": {str(k): str(v) for k, v in shareable.items()}, "never": [str(v) for v in never],
            "minutes": minutes, "record": bool(args.get("record"))}
    # task, company and shareable become the instructions bland.ai's agent gets; never_share stays here
    sent = {"task": [plan["task"]], "company": [plan["company"]],
            "shareable": [p for kv in plan["shareable"].items() for p in kv]}
    secret = [n for n in (_norm(v) for v in plan["never"]) if n]
    leaks = [field for field, parts in sent.items() if any(n in _norm(p) for p in parts for n in secret)]
    if leaks:
        return None, (f"Not placed: {' and '.join(leaks)} contain{'s' if len(leaks) == 1 else ''} a never_share value, "
                      "and they go to bland.ai. Restate the call without it.")
    return plan, None


def _fingerprint(plan: dict) -> str:
    return hashlib.sha256(json.dumps(plan, sort_keys=True).encode()).hexdigest()


def _answer(text) -> str:
    """support_call_wait's ``answer`` as the call records it: the three choices, or the user's own words."""
    words = str(text or "").strip()
    return {"yes": YES, "no": NO, "end": END_CALL}.get(words.lower(), words)


def _answer_card(call: CallSession, q: dict, answer: str) -> Optional[str]:
    """What Hermes's approval prompt shows before ``answer`` reaches the call, or None when it needs none:
    a yes always; the user's words when they share a detail off the list or commit them, as a chat reply
    does; a no or an end never, since turning something down is never the user's risk."""
    if answer == YES:
        return _question(q, call.labels(q))
    if answer in (NO, END_CALL):
        return None
    findings = call.guard.check(answer)
    return _reply_question(q["they_said"], answer, findings) if findings else None


def _answer_plan(call: CallSession, q: dict, answer: str) -> dict:
    return {"call": call.id, "question": list(_same(q["they_said"], q["you_would_say"])), "answer": answer}


def _waiting(args: dict) -> tuple[Optional[CallSession], Optional[dict], str]:
    """The live call, its oldest open question and the answer ``args`` give it, for the hooks: only a call
    live in this process, so a hook never outlasts Hermes's time limit reopening one."""
    with handlers._lock:
        call = _calls.get(handlers._sid(args.get("session_id")))
    q = call.waiting() if call is not None else None
    return call, q, _answer(args.get("answer"))


def _remember(key: str) -> None:
    with _approved_lock:
        now = time.monotonic()
        for old in [k for k, at in _approved.items() if now - at > APPROVAL_TTL_S]:
            _approved.pop(old, None)
        _approved[key] = now


def pre_tool_call(tool_name: str = "", args: Optional[dict] = None, **_kw):
    """A paid call is placed, and a yes said on it for the user, only after the user says yes at Hermes's
    own approval gate (CLI prompt, gateway buttons), which the model cannot skip and which fails closed
    when nobody can answer."""
    if not isinstance(args, dict):
        return None
    if tool_name == "support_call_wait":
        return _pre_answer(args)
    if tool_name != "support_call_start":
        return None
    plan, _ = _plan(args)
    if plan is None:
        return None  # the tool reports what is wrong; it places nothing
    labels = ", ".join(plan["shareable"])  # the labels only: the values stay out of the prompt and its logs
    question = (f"Place a phone call through bland.ai to {plan['company'] or 'a support line'} at {plan['number']}? "
                f"Up to {plan['minutes']} minutes, {'recorded' if plan['record'] else 'not recorded'}; "
                f"bland.ai bills the call to your account. Goal: {plan['task'][:200]} "
                + (f"It may share: {labels}." if labels else "It shares none of your details."))
    if handlers._approvals_off():
        return {"action": "block", "message": json.dumps({
            "status": "needs_user", "question": question,
            "detail": "Not placed: a phone call needs the user's OK and Hermes approvals are turned off here, so "
                      "nobody can be asked. Ask the user, and place it only after they say yes."})}
    key = _fingerprint(plan)
    _remember(key)
    # A fresh rule key per call: an "always" answer never waves through a later, different call.
    return {"action": "approve", "message": question, "rule_key": f"support_call:{key[:16]}:{time.time_ns()}"}


def _pre_answer(args: dict):
    if not str(args.get("answer") or "").strip():
        return None
    call, q, answer = _waiting(args)
    if q is None:
        return None  # the tool says what is wrong; it records nothing
    question = _answer_card(call, q, answer)
    if question is None:
        return None
    if handlers._approvals_off():
        return {"action": "block", "message": json.dumps({
            "status": "needs_user", "question": question,
            "detail": "Not answered: this needs the user's OK and Hermes approvals are turned off here, so nobody "
                      "can be asked. Tell the user what support asked; on the call answer 'no' or 'end'."})}
    _remember(_fingerprint(_answer_plan(call, q, answer)))
    # A fresh rule key per ask: an "always" answer never waves through a later question.
    return {"action": "approve", "message": question, "rule_key": f"support_call:{call.id}:{time.time_ns()}"}


def post_tool_call(tool_name: str = "", args: Optional[dict] = None, status: str = "", error_message: str = "",
                   **_kw) -> None:
    """Hermes's gate refused the call or the answer, or nobody answered: forget what ``pre_tool_call``
    recorded, so only a fresh yes places or says it."""
    if status != "blocked" or not isinstance(args, dict):
        return
    plan = None
    if tool_name == "support_call_start":
        plan, _ = _plan(args)
    elif tool_name == "support_call_wait":
        call, q, answer = _waiting(args)
        plan = _answer_plan(call, q, answer) if q is not None else None
    if plan is not None:
        with _approved_lock:
            _approved.pop(_fingerprint(plan), None)


def _take_approval(plan: dict) -> bool:
    """True once for a plan that went to the user through ``pre_tool_call``: Hermes runs the tool only
    after they said yes there. A call or an answer reaching the tool any other way is refused."""
    with _approved_lock:
        at = _approved.pop(_fingerprint(plan), None)
    return at is not None and time.monotonic() - at <= APPROVAL_TTL_S


def start(args: dict, parent_agent=None, **hermes) -> str:
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
    holder = handlers._hermes_session(parent_agent, hermes)
    call.progress = _progress(holder)
    url = _bridge_url()
    if url:
        _bridge.calls[call.id] = call
        reason = call.reaches(url)
        if reason:
            _bridge.calls.pop(call.id, None)
            return handlers._err(
                f"Not placed: the approval bridge address ({url}) does not reach this plugin's bridge ({reason}), so "
                "the call could not ask the user anything. Fix the tunnel or reverse proxy behind the call_bridge_url "
                f"setting so it forwards to 127.0.0.1:{_bridge.port}, or remove the setting to place calls that agree "
                "to nothing.")
    try:
        call.place(_bland(), url)
    except BlandError as exc:
        if _bridge is not None:
            _bridge.calls.pop(call.id, None)
        if exc.maybe_sent:  # retrying could place a second paid call
            return handlers._err(
                "bland.ai did not answer in time, so the call may or may not have been placed. Do NOT place it "
                "again: tell the user to check their bland.ai call log (app.bland.ai) and end the call there if "
                "it is running.", detail=str(exc))
        return handlers._err(str(exc))
    with handlers._lock:
        _calls[call.id] = call
        _owners[call.id] = holder
    call.progress(f"Calling {call.company or number}…")
    out = call.follow(_bland(), handlers._asker(parent_agent), max_wait_s=_wait_s(args))
    out["session_id"] = call.id
    # Hermes's gate can answer yes with nobody asked (yolo), and the plugin cannot tell: say it every time.
    out["tell_user"] = (f"A call to {call.company or 'the support line'} at {number} has been placed; bland.ai bills "
                        "it to the user's account. Say so in your next message to the user.")
    if not url:
        out["note"] = ("No approval bridge is set up (the call_bridge_url setting), so the call agrees to nothing: "
                       "it brings back any offer for the user to decide.")
    return handlers._ok(out)


def wait(args: dict, parent_agent=None, **hermes) -> str:
    try:
        call = _call(args.get("session_id"), handlers._hermes_session(parent_agent, hermes))
        told = None
        if str(args.get("answer") or "").strip():
            q, answer = call.waiting(), _answer(args.get("answer"))
            if q is None:
                return handlers._err("Nothing on this call is waiting for an answer.")
            carded = _answer_card(call, q, answer) is not None
            if carded and not _take_approval(_answer_plan(call, q, answer)):
                return handlers._err("Not answered: this answer needs the user's OK through Hermes's approval prompt, "
                                     "and it did not go through it. Support's question is still waiting.")
            if not call.answer(q, answer):
                return handlers._err("Support's question was answered meanwhile (nobody answered in time, or the "
                                     "call ended). Call support_call_wait to keep following the call.")
            if carded:  # Hermes's gate can answer yes with nobody asked (yolo): say it every time
                said = "yes" if answer == YES else f"\"{answer}\""
                told = f"Support was told {said} to: \"{q['they_said']}\". Say so in your next message to the user."
        out = call.follow(_bland(), handlers._asker(parent_agent), max_wait_s=_wait_s(args))
        if told:
            out["tell_user"] = told
        return handlers._ok(out)
    except Exception as exc:
        return handlers._err(str(exc))


def end(args: dict, parent_agent=None, **hermes) -> str:
    try:
        call = _call(args.get("session_id"), handlers._hermes_session(parent_agent, hermes))
    except Exception as exc:
        return handlers._err(str(exc))
    result = call.finish(_bland(), str(args.get("outcome") or "").strip() or "No outcome given.",
                         bool(args.get("resolved")))
    with handlers._lock:
        _calls.pop(call.id, None)
        _owners.pop(call.id, None)
    if _bridge is not None:
        _bridge.calls.pop(call.id, None)
    if result.get("hangup") == "failed":
        call.progress("Could not hang up: the call may still be running. End it in bland.ai's dashboard.")
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
        _owners.clear()
        if _bridge is not None:
            _bridge.close()
            _bridge = None


def on_session_finalize(session_id: Optional[str] = None, **_kw) -> None:
    """Save and let go of the calls of the Hermes session that is closing, and stop the bridge once no call
    is left: on a gateway, other sessions' calls keep their bridge."""
    global _bridge
    with handlers._lock:
        closing, last = handlers._closing(_calls, _owners, session_id)
        for c in closing:
            if _bridge is not None:
                _bridge.calls.pop(c.id, None)
        if last and _bridge is not None:
            _bridge.close()
            _bridge = None
    for c in closing:
        try:
            c.save()
        except Exception:
            pass


START = {
    "name": "support_call_start",
    "description": (
        "Phone a company's customer-support line for the user through bland.ai. Use when the user wants "
        "customer service handled by phone (a refund, a cancellation, a billing error). An AI voice agent places "
        "the call, gets through the phone menu from the task alone (no key sequence needed), and talks to support; "
        "it shares only the `shareable` details. The user approves the call itself (number, company, length, "
        "recording) at Hermes's prompt before it is placed. Every offer or commitment comes back to the user for "
        "approval before the agent says yes: you do not decide and do not ask with clarify. Where Hermes cannot ask "
        "them directly the result is status needs_answer: follow its instruction. Then call support_call_wait "
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
                   "returns call_ended with the summary when the call is over (or on_call after max_wait_s: call again). "
                   "Status needs_answer means the call holds for the user's decision: answer it here.",
    "parameters": {
        "type": "object",
        "properties": {
            "session_id": {"type": "string"},
            "answer": {"type": "string", "description": (
                "Only after status needs_answer, for the oldest question waiting on the call: 'yes' to let the voice "
                "agent say what it proposed (Hermes shows the user the question first), 'no' to turn it down and "
                "keep going, 'end' to hang up, or the user's own words when support asked for something only they "
                "can give.")},
            "max_wait_s": {"type": "number", "description": "Default 240, max 300."},
        },
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
