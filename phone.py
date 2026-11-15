"""One support phone call through bland.ai: the call's instructions, the approval bridge, the transcript.

bland.ai's voice agent runs the call by itself from a ``task`` written in plain sentences, and
finds its way through the company's phone menu from that task (``ivr_mode``), so no key sequence
has to be known. What it may not do by itself is say yes: every offer, commitment, or detail it
was not given goes through the ``CheckWithAccountHolder`` tool, an HTTPS request from bland.ai to
this plugin's bridge. The bridge holds the request while the user is asked here, in Hermes, and
hands the answer back to the call. A request the user has not answered yet comes back ``pending``
after ``HOLD_S`` and the agent asks the support person to keep holding, then asks again.

Without a bridge the agent is told it can agree to nothing: it collects the offer and a reference
and ends the call, and the offer comes back to the user afterwards.
"""

from __future__ import annotations

import http.client
import json
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional

from .bland import Bland
from .guard import Guard, _norm, asks_back
from .session import HOLD, Ask, Progress, Turn, _nothing

YES = "Yes, go ahead"
NO = "No: keep working toward the goal"
END_CALL = "End the call"
# bland.ai documents a 1-60 s timeout range only for saved tools; for the inline tool sent here no
# maximum is documented. The tool's timeout is derived from HOLD_S: these two constants are the place to change.
HOLD_S = 50.0
POLL_S = 3.0
SETTLE_S = 45.0  # how long to wait for the price and summary after the call ends
# How long a question with nobody to ask directly may wait for the model to bring back the user's answer before
# support is told the account holder will follow up: the voice agent asks again every HOLD_S meanwhile.
ANSWER_S = 170.0  # longer than one hold
READ_FAILURES = 5  # reads of the call failing in a row before following it stops: a revoked key or an outage
TOOL = "CheckWithAccountHolder"
PING_TIMEOUT_S = 10.0
PING_RETRY_S = 2.0  # a tunnel that was just started may need a moment
WHO = {"user": "support", "assistant": "you"}


class CallSession:
    def __init__(self, phone_number: str, company: str, task: str, allowed: dict, private: list, folder: Path,
                 max_minutes: int = 30, record: bool = False, session_id: Optional[str] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = POLL_S, hold_s: float = HOLD_S,
                 urlopen: Callable = urllib.request.urlopen) -> None:
        self.id = session_id or uuid.uuid4().hex[:8]
        self.phone_number = phone_number
        self.company = company
        self.task = task
        self.guard = Guard(allowed=dict(allowed or {}), task=task, private=list(private or []))
        self.folder = folder
        self.max_minutes = max_minutes
        self.record = record
        self.token = secrets.token_urlsafe(24)
        self.call_id: Optional[str] = None
        self.bridged = False
        self.turns: list[Turn] = []
        self.seen: list = []  # ids of bland.ai transcript lines already in turns
        self.questions: list[dict] = []
        self.approvals: list[dict] = []
        self.details: dict = {}
        self.ended = False
        self.closed = False
        self.progress: Progress = _nothing
        self._read_failures = 0
        self._read_error = ""
        self._clock, self._sleep, self._poll, self._hold = clock, sleep, poll_s, hold_s
        self._urlopen = urlopen
        self._cv = threading.Condition()

    # --- the call's instructions ---------------------------------------------------------

    def instructions(self) -> str:
        shareable = "\n".join(f"- {k}: {v}" for k, v in self.guard.allowed.items()) or "- (nothing)"
        lines = [
            f"You are phoning {self.company or 'a company'}'s customer support for your client, the account holder, "
            "to get this done:",
            self.task,
            "",
            "If an automated phone menu answers, listen to the options and press the key or say the option that "
            "leads to a person in customer support for this goal. If you are put on hold, wait quietly.",
            "As soon as a person answers, say first that you are an AI assistant calling on behalf of a customer"
            + (" and that the call is being recorded" if self.record else "")
            + ". If they object, apologise, thank them and end the call. Then explain the problem briefly and "
            "politely and work toward the goal.",
            "",
            "Details you may share when they are asked for:",
            shareable,
            "You know nothing else about the account holder. Never guess or make up a detail.",
            "",
        ]
        if self.bridged:
            lines += [
                f"You may never agree to anything yourself. Use {TOOL} before you answer whenever support makes "
                "an offer or proposes a resolution, asks you to confirm a charge, a cancellation or a change, or "
                "asks for a detail not listed above. Pass what they said and what you would say if the account "
                "holder agrees. Then do exactly what the decision says:",
                "- accepted: say yes in the words you proposed and confirm the details;",
                "- declined: politely turn it down and keep working toward the goal;",
                "- instruction: follow the account holder's instruction;",
                "- pending: thank them for holding, say you are still waiting on the account holder and ask them "
                f"to hold a little longer; when they answer, use {TOOL} again with the reply's ticket, before "
                "saying anything else;",
                "- stop: thank them, say the account holder will follow up, and end the call.",
                f"If {TOOL} fails, times out or returns no decision from this list, you have no answer: agree to "
                "nothing, share nothing new, say the account holder will follow up, and end the call.",
            ]
        else:
            lines += [
                "You cannot agree to anything on this call, and you cannot share anything not listed above. When "
                "support makes an offer or asks for something else, ask them to note it on the case, repeat the "
                "offer back so it is clear, and say the account holder will decide and follow up.",
            ]
        lines += ["", "If anything was agreed, ask for a reference or case number and repeat it back before you end the call."]
        return "\n".join(lines)

    def payload(self, bridge_url: Optional[str]) -> dict:
        self.bridged = bool(bridge_url)
        body = {
            "phone_number": self.phone_number,
            "task": self.instructions(),
            "ivr_mode": True,
            "wait_for_greeting": True,
            "record": self.record,
            "max_duration": self.max_minutes,
            "metadata": {"support_call": self.id},
        }
        if bridge_url:
            body["tools"] = [{
                "name": TOOL,
                "description": "Ask the account holder before agreeing to an offer, a charge, a cancellation or a "
                               "change, or before sharing a detail you were not given. Returns their decision.",
                "speech": HOLD,
                "url": f"{bridge_url.rstrip('/')}/calls/{self.id}/check",
                "method": "POST",
                "headers": {"Authorization": f"Bearer {self.token}"},
                "body": {"they_said": "{{input.they_said}}", "you_would_say": "{{input.you_would_say}}",
                         "ticket": "{{input.ticket}}"},
                "input_schema": {
                    "example": {"they_said": "I can refund $36 to your card. Would that work?",
                                "you_would_say": "Yes, the $36 refund works, thank you."},
                    "type": "object",
                    "properties": {"they_said": {"type": "string"}, "you_would_say": {"type": "string"},
                                   "ticket": {"type": "string"}},
                    "required": ["they_said", "you_would_say"],
                },
                "response": {"decision": "$.decision", "instruction": "$.instruction", "ticket": "$.ticket"},
                "timeout": int((self._hold + 10) * 1000),
            }]
        return body

    def reaches(self, bridge_url: str) -> Optional[str]:
        """None when ``bridge_url`` reaches this plugin's bridge for this call, otherwise why not, in a
        few words without the token: the address bland.ai's tool will call, pinged the way it will."""
        req = urllib.request.Request(f"{bridge_url.rstrip('/')}/calls/{self.id}/check", data=b'{"ping": true}',
                                     method="POST", headers={"Authorization": f"Bearer {self.token}",
                                                             "Content-Type": "application/json"})
        reason = self._ping(req)
        if reason is not None:
            self._sleep(PING_RETRY_S)
            reason = self._ping(req)
        return reason

    def _ping(self, req: urllib.request.Request) -> Optional[str]:
        try:
            with self._urlopen(req, timeout=PING_TIMEOUT_S) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}"
        except urllib.error.URLError as exc:
            return f"connection failed: {exc.reason}"
        except TimeoutError:
            return "no answer in time"
        except (OSError, http.client.HTTPException) as exc:
            return f"connection failed: {exc or type(exc).__name__}"
        try:
            out = json.loads(raw or b"")
        except ValueError:
            return "not JSON"
        return None if out == {"ready": self.id} else "an answer that is not this bridge's"

    def place(self, client: Bland, bridge_url: Optional[str]) -> None:
        self.call_id = client.send_call(self.payload(bridge_url))
        self.turns.append(Turn("note", f"Called {self.phone_number} through bland.ai (call {self.call_id})."))
        self.save()

    # --- the bridge side: a request from the call ---------------------------------------

    def check(self, they_said: str, you_would_say: str, ticket: str = "") -> dict:
        """bland.ai's agent asks before it says yes. Holds up to ``hold_s`` for the user's answer.
        A request with the ticket of an undelivered question (from its ``pending`` reply) is that question,
        whatever its words. Otherwise a question is its words: the same words are the same question,
        different words a new one, so an answer never reaches an offer it was not given to."""
        key = _same(they_said, you_would_say)
        with self._cv:
            open_ = [q for q in self.questions if not q.get("delivered")]
            q = next((q for q in open_ if ticket and q.get("ticket") == ticket), None)
            q = q or next((q for q in open_ if _same(q["they_said"], q["you_would_say"]) == key), None)
            if q is None:
                findings = self.guard.check(you_would_say or "", "agree")
                q = {"they_said": they_said, "you_would_say": you_would_say, "ticket": secrets.token_hex(3),
                     "shares": [f.as_dict() for f in findings if f.kind == "detail"], "answer": None,
                     "at": self._clock()}
                self.questions.append(q)
                self.save()
                self._cv.notify_all()
            deadline = self._clock() + self._hold
            while True:
                self._expire()
                left = deadline - self._clock()
                if q["answer"] is None and not q.get("asking"):
                    left = min(left, q["at"] + ANSWER_S - self._clock())
                if q["answer"] is not None or left <= 0:
                    break
                self._cv.wait(timeout=left)
            if q["answer"] is None:
                return {"decision": "pending", "ticket": q.get("ticket", ""), "instruction": (
                    "The account holder has not answered yet. Say: \"Thank you for holding. I'm still waiting on the "
                    "account holder. Could you hold a little longer?\" When they answer, use "
                    f"{TOOL} again with ticket {q.get('ticket', '')}, before saying anything else.")}
            q["delivered"] = True
            self.save()
            return _decision(q["answer"], q["you_would_say"])

    # --- the Hermes side: following the call --------------------------------------------

    def follow(self, client: Bland, ask: Optional[Ask], max_wait_s: float = 240.0) -> dict:
        """Follow the call: new transcript lines, questions for the user, the end of the call. With nobody
        to ask directly (``ask`` None) a question returns at once as ``needs_answer``, for the model."""
        start = self._clock()
        heard: list[str] = []
        waiting = None
        while True:
            heard += self._read(client)
            self._answer_questions(ask)
            with self._cv:
                self._expire()
                waiting = self.waiting() if ask is None else None
            if (waiting is not None or self.ended or self._read_failures >= READ_FAILURES
                    or self._clock() - start >= max_wait_s):
                break
            self._sleep(self._poll)
        # bland.ai marks a call completed before it has priced and summarised it
        settle = self._clock()
        while self.ended and self.details.get("price") is None and self._clock() - settle < SETTLE_S:
            self._sleep(self._poll)
            heard += self._read(client)
        self.save()
        out = {"status": "call_ended" if self.ended else "on_call", "support_said": heard,
               "waited_s": round(self._clock() - start)}
        if self.ended:
            out.update({k: self.details.get(k) for k in ("summary", "call_length", "price", "answered_by",
                                                         "recording_url", "error_message") if self.details.get(k) is not None})
            out["hint"] = "The call is over. Call support_call_end with the outcome."
        elif waiting is not None:
            labels = self.labels(waiting)
            out.update({"status": "needs_answer", "session_id": self.id, "they_said": waiting["they_said"],
                        "you_would_say": waiting["you_would_say"], "shares": labels,
                        "question": _question(waiting, labels), "instruction": (
                            "The call is holding for the account holder's decision. If this meets what the user "
                            "asked for, call support_call_wait with this session_id and answer 'yes': Hermes will "
                            "show the user the question before anything is said. To turn it down and keep going, "
                            "answer 'no'. To hang up, answer 'end'. If support asked for a detail you were not "
                            "given, ask the user for it, then pass their words as the answer. Never answer yes for "
                            f"the user in any other way. With no answer within {round(ANSWER_S)} seconds of the "
                            "question, support is told the account holder will follow up.")})
        else:
            out["hint"] = "Still on the call. Call support_call_wait to keep following it."
        if not self.bridged and self.ended:
            out["decide"] = "Nothing was agreed on the call. Tell the user any offer support made and let them decide."
        if not self.ended and self._read_failures >= READ_FAILURES:
            out["read_error"] = self._read_error
            out["detail"] = (f"The call's state could not be read from bland.ai ({self._read_failures} tries in a row), "
                             "so the call may still be running. Tell the user. support_call_end tries to hang up; "
                             "otherwise it runs until its max-minutes cap or until it is ended in bland.ai's dashboard.")
        return out

    def _read(self, client: Bland) -> list[str]:
        if not self.call_id:
            return []
        try:
            data = client.call(self.call_id)
        except Exception as exc:
            self._read_failures += 1
            self._read_error = str(exc) or type(exc).__name__
            self.progress(f"Could not read the call: {exc}")
            return []
        self._read_failures = 0
        self.details = {k: v for k, v in data.items() if k not in ("transcripts", "concatenated_transcript")}
        heard = []
        for line in data.get("transcripts") or []:
            key = line.get("id", len(self.seen))
            if key in self.seen:
                continue
            self.seen.append(key)
            who = WHO.get(line.get("user"), "note")
            text = str(line.get("text") or "").strip()
            if not text:
                continue
            if who == "note" and self.turns and (self.turns[-1].who, self.turns[-1].text) == ("note", text):
                continue  # bland.ai repeats its waiting note for as long as the hold lasts
            self.turns.append(Turn(who, text))
            if who == "support":
                self.guard.heard.append(text)
                heard.append(text)
                self.progress(f"Support: {text}")
            elif who == "you":
                self.progress(f"Said: {text}")
        if data.get("completed") or str(data.get("status", "")).lower() in ("completed", "failed", "error"):
            self.ended = True
        return heard

    def waiting(self) -> Optional[dict]:
        """The oldest question from the call that nobody is answering yet."""
        with self._cv:
            return next((q for q in self.questions if q["answer"] is None and not q.get("asking")), None)

    def labels(self, q: dict) -> list[str]:
        """The labels of the user's shareable details that the answer drafted for ``q`` would say."""
        said = _norm(q["you_would_say"] or "")
        return [k for k, v in self.guard.allowed.items()
                if not k.startswith("told ") and _norm(str(v)) and _norm(str(v)) in said]

    def answer(self, q: dict, answer: str) -> bool:
        """The user's answer to ``q``, brought back by the model; False when it was answered meanwhile."""
        with self._cv:
            if q["answer"] is not None:
                return False
            self._record(q, answer)
            return True

    def _answer_questions(self, ask: Optional[Ask]) -> None:
        if ask is None:
            return  # nobody to ask here: follow hands the question to the model
        with self._cv:
            q = self.waiting()
            if q is None:
                return
            q["asking"] = True
        self.progress("Support made an offer on the call: waiting for your call.")
        answer = ask(_question(q), [YES, NO, END_CALL])
        with self._cv:
            self._record(q, answer)

    def _expire(self) -> None:
        """A question nobody is asking about gets no answer after ``ANSWER_S``: support is not held forever.
        Called with ``_cv`` held."""
        for q in self.questions:
            if q["answer"] is None and not q.get("asking") and self._clock() - q.setdefault("at", self._clock()) >= ANSWER_S:
                self._record(q, None, f"Asked you: \"{q['they_said']}\". Nobody answered within {round(ANSWER_S)} seconds.")
                self.progress("Nobody answered support's question in time: they were told you will follow up.")

    def _record(self, q: dict, answer: Optional[str], note: Optional[str] = None) -> None:
        with self._cv:
            q["answer"] = answer or ""
            q.pop("asking", None)
            self.approvals.append({"asked": _question(q), "answer": answer})
            self.turns.append(Turn("note", note or f"Asked you: \"{q['they_said']}\". You answered: {answer or 'no answer'}"))
            if answer not in (None, "", YES, NO, END_CALL):
                self.guard.allowed[f"told {len(self.guard.allowed) + 1}"] = answer
            self.save()
            self._cv.notify_all()

    # --- ending and saving ---------------------------------------------------------------

    def finish(self, client: Bland, outcome: str, resolved: bool) -> dict:
        still_on = None  # why hanging up failed, while the call may still be running
        if not self.ended and self.call_id:
            error = None
            for attempt in range(2):
                try:
                    client.stop(self.call_id)
                    error = None
                    break
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    if attempt == 0:
                        self._sleep(self._poll)
            self._read(client)
            if error is None:
                self.turns.append(Turn("note", "Hung up."))
            elif self.ended:
                self.turns.append(Turn("note", f"Could not hang up ({error}), but the call had already ended."))
            else:
                still_on = (f"Could not hang up, tried twice ({error}). The call may still be running, billed until its "
                            f"{self.max_minutes}-minute cap; end it in bland.ai's dashboard (app.bland.ai).")
                self.turns.append(Turn("note", still_on))
        with self._cv:
            for q in self.questions:  # a request still held gets its answer: the call is over
                if q["answer"] is None:
                    q["answer"] = ""
            self._cv.notify_all()
        audit = self.audit()
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / "transcript.md"
        body = [
            f"# Support call {self.id}",
            "",
            f"- **Called:** {self.company} {self.phone_number}".replace("  ", " "),
            f"- **bland.ai call:** {self.call_id or 'not placed'}",
            f"- **Goal:** {self.task}",
            f"- **Outcome:** {outcome}",
            f"- **Resolved:** {'yes' if resolved else 'no'}",
            f"- **Approvals asked:** {len(self.approvals)}",
        ]
        for key, label in (("call_length", "Length (min)"), ("price", "Cost (USD)"), ("summary", "bland.ai summary"),
                           ("recording_url", "Recording")):
            if self.details.get(key) is not None:
                body.append(f"- **{label}:** {self.details[key]}")
        if audit:
            body.append("- **Check:** " + "; ".join(audit))
        if still_on:
            body.append(f"- **Hang-up failed:** {still_on}")
        names = {"support": "Support", "you": "Hermes, for you", "note": "Note"}
        body += ["", "## Transcript", "", "```",
                 "\n".join(f"[{t.at[11:19]}] {names[t.who]}: {t.text}" for t in self.turns), "```", ""]
        path.write_text("\n".join(body), encoding="utf-8")
        self.closed = True
        self.save()
        out = {"status": "closed", "outcome": outcome, "resolved": resolved, "transcript_file": str(path),
               "approvals": self.approvals, "check": audit, "price": self.details.get("price")}
        if still_on:
            out["hangup"] = "failed"
            out["detail"] = "Tell the user right away. " + still_on
        return out

    def audit(self) -> list[str]:
        """What the call's agent said that the user had not allowed: details off the list, and a yes
        to an offer no approval covered. bland.ai speaks without asking us, so this is checked after."""
        guard = Guard(allowed=dict(self.guard.allowed), task=self.task, private=list(self.guard.private))
        accepted = any(q["answer"] == YES for q in self.questions)
        out = []
        for t in self.turns:
            if t.who == "support":
                guard.heard.append(t.text)
            elif t.who == "you" and t.text != HOLD:
                for f in guard.check(t.text):
                    if f.kind == "detail":
                        out.append(f"said {f.text!r}: {f.why}")
                    elif f.kind == "commitment" and not accepted:
                        out.append(f"said {f.text!r} with no approval from you")
                if not asks_back(t.text):  # a reply closes the offers heard before it; a question back does not
                    guard.answered = len(guard.heard)
        return out

    def save(self) -> None:
        with self._cv:  # the bridge's threads save too, through the same temporary file
            self.folder.mkdir(parents=True, exist_ok=True)
            state = {
                "id": self.id, "phone_number": self.phone_number, "company": self.company, "task": self.task,
                "allowed": self.guard.allowed, "private": self.guard.private, "max_minutes": self.max_minutes,
                "record": self.record, "token": self.token, "call_id": self.call_id, "bridged": self.bridged,
                "seen": self.seen, "turns": [asdict(t) for t in self.turns],
                "questions": [{k: v for k, v in q.items() if k not in ("asking", "at")} for q in self.questions],
                "approvals": self.approvals, "details": self.details, "ended": self.ended, "closed": self.closed,
            }
            tmp = self.folder / "session.json.tmp"
            tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.folder / "session.json")

    @classmethod
    def restore(cls, folder: Path, **kw) -> "CallSession":
        st = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        s = cls(st["phone_number"], st["company"], st["task"], st["allowed"], st["private"], folder,
                max_minutes=st["max_minutes"], record=st.get("record", False), session_id=st["id"], **kw)
        s.token, s.call_id, s.bridged, s.seen = st["token"], st["call_id"], st["bridged"], list(st["seen"])
        s.turns = [Turn(**t) for t in st["turns"]]
        s.questions, s.approvals, s.details = list(st["questions"]), list(st["approvals"]), dict(st["details"])
        s.ended, s.closed = st["ended"], st["closed"]
        for t in s.turns:
            if t.who == "support":
                s.guard.heard.append(t.text)
        return s


def _same(they_said: str, you_would_say: str) -> tuple[str, str]:
    return _norm(they_said or ""), _norm(you_would_say or "")


def _filled(value) -> str:
    """A body value as text; one bland.ai left as its template (``{{input.x}}``, the input never given) is empty."""
    text = str(value or "")
    return "" if re.fullmatch(r"\s*\{\{.*\}\}\s*", text, re.S) else text


def _question(q: dict, labels: list = ()) -> str:
    lines = [f"On the call, support said: \"{q['they_said']}\""]
    if not q["you_would_say"]:
        lines.append("I have no answer drafted. What should I answer? If you say yes, I'll tell them you agree.")
        return "\n".join(lines)
    lines.append(f"If you say yes, I'll answer: \"{q['you_would_say']}\"")
    if q.get("shares"):
        lines.append("That answer shares " + "; ".join(f"{f['why']} ({f['text']})" for f in q["shares"]) + ".")
    if labels:
        lines.append("It says your " + ", ".join(labels) + ".")
    lines.append("Go ahead?")
    return "\n".join(lines)


def _decision(answer: str, proposed: str = "") -> dict:
    if answer == YES:
        return {"decision": "accepted", "instruction": "The account holder agrees. " + (
            "Say yes as you proposed." if proposed else "Say yes to what they asked.")}
    if answer == NO:
        return {"decision": "declined",
                "instruction": "The account holder does not agree. Politely turn it down and keep working toward the goal."}
    if answer == END_CALL:
        return {"decision": "stop", "instruction": "Thank them, say the account holder will follow up, and end the call."}
    if not answer:
        return {"decision": "stop", "instruction": "The account holder could not be reached, so nothing is agreed. "
                                                   "Thank them, say the account holder will follow up, and end the call."}
    return {"decision": "instruction", "instruction": answer}


class Bridge:
    """The HTTP endpoint bland.ai's tool calls: ``POST /calls/<id>/check`` with the call's bearer
    token. Listens on 127.0.0.1; a tunnel or reverse proxy publishes it as ``bridge_url``."""

    def __init__(self, port: int = 0, host: str = "127.0.0.1") -> None:
        self.calls: dict[str, CallSession] = {}
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                parts = self.path.strip("/").split("/")
                call = bridge.calls.get(parts[1]) if len(parts) == 3 and parts[0] == "calls" and parts[2] == "check" else None
                auth = self.headers.get("Authorization", "")
                if call is None or not secrets.compare_digest(auth, f"Bearer {call.token}"):
                    return self._send(404, {"error": "unknown call"})
                try:
                    data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "body is not JSON"})
                if not isinstance(data, dict):
                    return self._send(400, {"error": "body is not an object"})
                if data == {"ping": True}:
                    return self._send(200, {"ready": call.id})
                self._send(200, call.check(*(_filled(data.get(k)) for k in ("they_said", "you_would_say", "ticket"))))

            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_a):
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, name="support-call-bridge", daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
