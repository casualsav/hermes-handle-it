"""One support conversation: the waiting loop, the gate in front of every message, the transcript.

A chat has no time cap. Every step is saved to ``session.json`` in the chat's folder, so a chat
outlives one tool call, one agent turn or a restart of Hermes: the next call reopens the page in
the same browser profile and carries on from the saved transcript.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from . import reader
from .guard import PAYMENT, Finding, Guard, asks_back, is_offer

# ask(question, choices) -> the user's answer (a choice or typed text), or None when nobody answered
Ask = Callable[[str, list], Optional[str]]
Progress = Callable[[str], None]

APPROVE = "Approve: send it"
DENY = "Don't agree: keep negotiating"
STOP = "Stop the chat"
ACCEPT_OFFER = "Accept the offer"
DECLINE_OFFER = "Turn it down: keep negotiating"
HOLD = "One moment please, I'm checking with the account holder."
FOLLOW_UP = ("Thank you for your patience. The account holder has stepped away and can't confirm right now, "
             "so they will follow up with you directly.")
QUIET_S = 3.0  # a reply counts once the support side has been quiet this long
HEARTBEAT_S = 60.0  # how often a long wait tells the user it is still holding


@dataclass
class Turn:
    who: str  # "support", "you" or "note"
    text: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))


def _nothing(_msg: str) -> None:
    pass


class ChatSession:
    def __init__(self, page, url: str, task: str, allowed: dict, private: list, folder: Path,
                 session_id: Optional[str] = None, baseline: Optional[str] = None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, poll_s: float = 1.0) -> None:
        self.id = session_id or uuid.uuid4().hex[:8]
        self.page = page
        self.url = url
        self.task = task
        self.guard = Guard(allowed=dict(allowed or {}), task=task, private=list(private or []))
        self.folder = folder
        self.turns: list[Turn] = []
        self.sent: list[str] = []
        self.approvals: list[dict] = []
        self.ended = False
        self.closed = False
        # A message Hermes's approval gate is asking the user about. In memory only: after a restart nobody
        # can say the gate's yes was for it.
        self.pending: Optional[str] = None
        self.pending_offer: Optional[tuple[str, str]] = None  # (offer, "accept" or "decline") the pending reply gives
        self.decided: dict = {}  # offer text -> "accept" or "decline", the user's call on it
        # What the page showed before the chat opened: anything after it is the chat.
        self._lines = reader.lines_of(baseline if baseline is not None else page.text()) if page is not None else []
        self._clock, self._sleep, self._poll = clock, sleep, poll_s
        self._shots = 0
        self.progress: Progress = _nothing

    # --- persistence ---------------------------------------------------------------------

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        state = {
            "id": self.id, "url": self.url, "task": self.task, "allowed": self.guard.allowed,
            "private": self.guard.private, "heard": self.guard.heard, "turns": [asdict(t) for t in self.turns],
            "sent": self.sent, "approvals": self.approvals, "ended": self.ended, "closed": self.closed,
            "lines": self._lines, "shots": self._shots, "decided": self.decided,
            "answered": self.guard.answered,
        }
        tmp = self.folder / "session.json.tmp"
        tmp.write_text(json.dumps(state, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.folder / "session.json")

    @classmethod
    def restore(cls, folder: Path, page, **kw) -> "ChatSession":
        state = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        s = cls(None, state["url"], state["task"], state["allowed"], state["private"], folder,
                session_id=state["id"], **kw)
        s.page = page
        s.guard.heard = list(state["heard"])
        s.guard.answered = state.get("answered", 0)
        s.turns = [Turn(**t) for t in state["turns"]]
        s.sent = list(state["sent"])
        s.approvals = list(state["approvals"])
        s.ended, s.closed = state["ended"], state["closed"]
        s._lines = list(state["lines"])
        s._shots = state.get("shots", 0)
        s.decided = dict(state.get("decided") or {})
        return s

    # --- reading -------------------------------------------------------------------------

    def wait_for_reply(self, max_wait_s: float = 240.0, quiet_s: float = QUIET_S) -> dict:
        """Wait until the support side has said something and then gone quiet for ``quiet_s``
        (people send several bubbles), or ``max_wait_s`` passes, or the chat ends. A wait that
        ends with nothing said is not a failure: the caller waits again, as long as it takes."""
        start = self._clock()
        last_text: list[str] = self._lines
        last_change = start
        last_beat = start
        was_typing = False
        heard: list[str] = []
        while True:
            now = self._clock()
            lines = reader.lines_of(self.page.text())
            if lines != last_text:
                last_text, last_change = lines, now
            fresh = reader.incoming(reader.appeared(self._lines, lines), self.sent)
            typing = reader.typing_now(lines)
            if typing and not was_typing:
                self.progress("Support is typing…")
            was_typing = typing
            if fresh and reader.ended(fresh):
                heard = fresh
                self.ended = True
                break
            if fresh and now - last_change >= quiet_s and not typing:
                heard = fresh
                break
            if now - start >= max_wait_s:
                heard = fresh
                break
            if now - last_beat >= HEARTBEAT_S:
                last_beat = now
                self.progress(f"Still holding the support chat ({int(now - start)}s without a reply).")
            self._sleep(self._poll)
        self._lines = last_text
        for line in heard:
            self.turns.append(Turn("support", line))
            self.guard.heard.append(line)
            self.progress(f"Support: {line}")
        shot = self._shoot() if heard else None
        self.save()
        status = "chat_ended" if self.ended else ("replied" if heard else "still_waiting")
        out = {"status": status, "support_said": heard, "waited_s": round(self._clock() - start)}
        if shot:
            out["screenshot"] = shot
        if status == "still_waiting":
            out["hint"] = "No reply yet. Call support_chat_wait again to keep holding; there is no time limit."
        elif not self.ended and reader.agent_left(heard):
            out["hint"] = ("The support agent left the chat. If nobody else joins, the chat is over: call "
                           "support_chat_end with what was reached.")
        return out

    # --- sending -------------------------------------------------------------------------

    def _type(self, message: str) -> None:
        self.page.send(message)
        self.sent.append(message)
        if message != HOLD and not _just_asking(message):
            self.guard.answered = len(self.guard.heard)
        self.turns.append(Turn("you", message))
        self.progress(f"Sent: {message}")
        self.save()

    def gate(self, message: str, intent: str, ask: Optional[Ask]) -> tuple[bool, dict]:
        """Run the guard; ask the user when it finds something. Returns (send_it, info).
        Silence accepts nothing: no answer means nothing is sent."""
        blocked = self.guard.hard_block(message)
        if blocked:
            return False, {"status": "refused", "reason": _reasons(blocked),
                           "detail": "Card details are never typed into a chat. Tell the support agent the "
                                     "account holder will pay themselves, and tell the user."}
        offer = self.open_offer()
        accepting = intent in ("agree", "pay") or bool(self.guard.commitments(message))
        findings = self.guard.check(message, intent)
        accepted = bool(offer) and self.decided.get(offer) == "accept" and accepting
        if accepted:
            # The user's yes covers agreeing to this offer, and paying only when the offer itself is a payment.
            paid = bool(PAYMENT.search(offer))
            findings = [f for f in findings if f.kind != "commitment" or (f.pay and not paid)]
        if offer and ask is not None and offer not in self.decided and not _just_asking(message):
            return self._offer_call(offer, message, accepting, findings, ask)
        if not findings:
            if accepted:
                self.decided.pop(offer)
            return True, {}
        if ask is None:
            return False, {
                "status": "needs_user",
                "reason": _reasons(findings),
                "detail": "Nobody can be asked for approval here (unattended run), so nothing was sent. "
                          "Tell the user what the support agent wants and stop.",
            }
        last = next((t.text for t in reversed(self.turns) if t.who == "support"), "")
        self._hold()
        question = _question(last, message, findings)
        self.progress("Waiting for your OK before replying.")
        answer = ask(question, [APPROVE, DENY, STOP])
        self.approvals.append({"asked": question, "answer": answer})
        self.turns.append(Turn("note", f"Asked you: {_reasons(findings)}. You answered: {answer or 'no answer'}"))
        # Whatever support said while the user decided goes into the transcript first.
        self.wait_for_reply(max_wait_s=0)
        if answer == APPROVE:
            for f in findings:
                if f.kind == "detail":
                    self.guard.allowed[f"approved {len(self.guard.allowed) + 1}"] = f.text
            if accepted:
                self.decided.pop(offer, None)
            return True, {"approved_by_user": True}
        self.save()
        if answer == STOP:
            return False, {"status": "user_stopped",
                           "detail": "The user asked to stop. Call support_chat_end with what was reached so far."}
        if answer in (None, ""):
            return False, {"status": "no_answer",
                           "detail": "The user did not answer, so nothing was agreed or shared. Tell the support "
                                     "agent you cannot confirm today, then call support_chat_end."}
        if answer == DENY:
            return False, {"status": "user_declined",
                           "detail": "The user does not agree. Nothing was sent. Keep working toward the goal "
                                     "without agreeing to this."}
        return False, {"status": "user_instruction", "instruction": answer,
                       "detail": "The user typed an instruction instead of approving. Nothing was sent; follow it."}

    def before_approval(self, message: str, intent: str) -> tuple[str, str]:
        """For Hermes's approval gate, which runs before the tool: ("block", why), ("ask", question),
        ("turn_down", question) or ("", ""). On "ask" and "turn_down" the support agent is told to hold on
        while the user decides. A "turn_down" turns an open offer down: it never needs anyone's OK, only
        the user's call on the offer when someone can be asked."""
        if self.ended:
            return "", ""
        blocked = self.guard.hard_block(message)
        if blocked:
            self.pending = None
            return "block", ("Not sent: " + _reasons(blocked) + ". Card details are never typed into a chat; tell "
                             "support the account holder will pay themselves, and tell the user.")
        findings = self.guard.check(message, intent)
        offer = self.open_offer()
        if not findings:
            if not offer or offer in self.decided or _just_asking(message):
                self.pending = None
                return "", ""
            self._hold()
            self.pending, self.pending_offer = message, (offer, "decline")
            self.progress("Support made an offer: waiting for your call.")
            self.save()
            return "turn_down", "\n".join(_offer_lines(offer, message, False)
                                           + ["Allow sends this reply; deny sends nothing and the offer stays open."])
        accepting = intent in ("agree", "pay") or bool(self.guard.commitments(message))
        self._hold()
        self.pending = message
        self.pending_offer = (offer, "accept") if offer and accepting else None
        last = next((t.text for t in reversed(self.turns) if t.who == "support"), "")
        self.progress("Waiting for your OK before replying.")
        self.save()
        return "ask", _question(last, message, findings)

    def approval_refused(self, message: str, reason: str) -> Optional[str]:
        """Hermes's gate did not let ``message`` through (it runs after the hook, so only the block
        tells us). A no keeps the chat going; no answer ends it the way a call does: support hears the
        account holder will follow up, and nothing is agreed. Any other block only forgets the reply.
        Returns "declined", "blocked", "no_answer" or None."""
        if self.pending is None or self.pending != message:
            return None
        self.pending = None
        offer, call = self.pending_offer or (None, None)
        low = reason.lower()
        # Hermes's own hook timeout ("plugin callback timed out") means nobody was asked, not that nobody answered.
        if "timed out" not in low or "callback" in low:
            declined = "denied" in low
            if declined and call == "accept":
                self.decided[offer] = "decline"  # no to accepting is no to the offer
            what = "turn-down" if call == "decline" else "reply"
            self.approvals.append({"asked": message, "answer": "declined" if declined else "blocked"})
            self.turns.append(Turn("note", f"You declined this {what}; the {what} was not sent." if declined
                                   else f"Hermes blocked this {what}; the {what} was not sent."))
            self.save()
            return "declined" if declined else "blocked"
        self.approvals.append({"asked": message, "answer": None})
        try:
            self._type(FOLLOW_UP)
            told = True
        except Exception:
            told = False
        if told:
            self.turns.append(Turn("note", "No answer to the approval; told support you will follow up."))
            said = "Support was told the account holder will follow up"
        else:
            self.turns.append(Turn("note", "No answer to the approval; support could not be told you will follow "
                                           "up (the message did not go through)."))
            said = "Support could not be told the account holder will follow up (the message did not go through)"
        self.finish(f"Nobody answered the approval for: \"{message}\". {said}; nothing was agreed.", resolved=False)
        return "no_answer"

    def _hold(self) -> None:
        """Like a person would: tell the agent to hold on while the account holder decides. Never twice in a row."""
        if self.sent and self.sent[-1] == HOLD:
            return
        try:
            self._type(HOLD)
        except Exception:
            self.turns.append(Turn("note", "Support could not be told to hold on (the message did not go through); "
                                           "asked you anyway."))

    def open_offer(self) -> Optional[str]:
        """Support's latest offer since our last real reply (our hold line and a question back do not answer it)."""
        for t in reversed(self.turns):
            if t.who == "you" and t.text != HOLD and not _just_asking(t.text):
                return None
            if t.who == "support" and is_offer(t.text):
                return t.text
        return None

    def _offer_call(self, offer: str, message: str, accepting: bool, findings: list, ask: Ask) -> tuple[bool, dict]:
        """Every offer is the user's call, whichever way the model leans: ask about the offer itself."""
        self._hold()
        offered, reply = _offer_lines(offer, message, accepting)
        lines = [offered, "Accept it?", reply]
        details = [f for f in findings if f.kind == "detail"]
        if details:
            lines.append("That reply also shares " + _reasons(details) + ".")
        self.progress("Support made an offer: waiting for your call.")
        answer = ask("\n".join(lines), [ACCEPT_OFFER, DECLINE_OFFER, STOP])
        self.approvals.append({"asked": "\n".join(lines), "answer": answer})
        self.turns.append(Turn("note", f"Asked you about the offer. You answered: {answer or 'no answer'}"))
        self.wait_for_reply(max_wait_s=0)
        if answer == ACCEPT_OFFER:
            if accepting:
                return True, {"approved_by_user": True}
            self.decided[offer] = "accept"
            self.save()
            return False, {"status": "user_accepts_offer",
                           "detail": "The user accepts this offer. Nothing was sent: reply accepting it now "
                                     "(intent 'agree'); it goes straight through."}
        if answer == DECLINE_OFFER:
            self.decided[offer] = "decline"
            if not accepting:
                return True, {"user_declined_offer": True}
            self.save()
            return False, {"status": "user_declined_offer",
                           "detail": "The user turns this offer down. Nothing was sent: tell support politely and "
                                     "keep working toward the goal."}
        self.save()
        if answer == STOP:
            return False, {"status": "user_stopped",
                           "detail": "The user asked to stop. Call support_chat_end with what was reached so far."}
        if answer in (None, ""):
            return False, {"status": "no_answer",
                           "detail": "The user did not answer, so nothing was agreed. Tell support you cannot "
                                     "confirm today, then call support_chat_end."}
        return False, {"status": "user_instruction", "instruction": answer,
                       "detail": "The user typed an instruction instead. Nothing was sent; follow it."}

    def send(self, message: str, intent: str = "inform", ask: Optional[Ask] = None,
             max_wait_s: float = 240.0) -> dict:
        if self.ended:
            return {"status": "chat_ended", "detail": "The chat has ended. Call support_chat_end."}
        if self.pending is not None and self.pending == message:
            # Hermes's approval gate asked the user about exactly this message and they said yes:
            # the tool only runs after that yes.
            self.pending = None
            offer, call = self.pending_offer or (None, None)
            if call == "decline":
                # Allowing a turn-down is the user's no to the offer, not an OK to agree to anything.
                self.decided[offer] = "decline"
                self.approvals.append({"asked": message, "answer": DECLINE_OFFER})
                self.turns.append(Turn("note", f"Asked you about the offer. You answered: {DECLINE_OFFER}"))
                info = {"user_declined_offer": True}
            else:
                self.approvals.append({"asked": message, "answer": "approved"})
                self.turns.append(Turn("note", "You approved this reply."))
                info = {"approved_by_user": True}
            self.wait_for_reply(max_wait_s=0)  # what support said while you decided
        else:
            ok, info = self.gate(message, intent, ask)
            if not ok:
                return info
        self._type(message)
        reply = self.wait_for_reply(max_wait_s=max_wait_s)
        reply.update(info)
        reply["sent"] = message
        if info.get("approved_by_user"):
            # Hermes's gate can answer yes with nobody asked (yolo), and the plugin cannot tell: say it every time.
            reply["tell_user"] = ("This reply needed the account holder's OK and has been sent. In your next message "
                                  f"to the user, say plainly what it agreed to, paid, cancelled or shared: \"{message}\"")
        return reply

    # --- ending --------------------------------------------------------------------------

    def transcript(self) -> str:
        names = {"support": "Support", "you": "Hermes, for you", "note": "Approval"}
        return "\n".join(f"[{t.at[11:19]}] {names[t.who]}: {t.text}" for t in self.turns)

    def finish(self, outcome: str, resolved: bool) -> dict:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / "transcript.md"
        body = [
            f"# Support chat {self.id}",
            "",
            f"- **Chat:** {self.url}",
            f"- **Goal:** {self.task}",
            f"- **Outcome:** {outcome}",
            f"- **Resolved:** {'yes' if resolved else 'no'}",
            f"- **Approvals asked:** {len(self.approvals)}",
            "",
            "## Transcript",
            "",
            "```",
            self.transcript(),
            "```",
            "",
        ]
        path.write_text("\n".join(body), encoding="utf-8")
        self.closed = True
        self.save()
        if self.page is not None:
            self.page.close()
        return {"status": "closed", "outcome": outcome, "resolved": resolved, "transcript_file": str(path),
                "transcript": self.transcript(), "approvals": self.approvals}

    def _shoot(self) -> Optional[str]:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._shots += 1
        return self.page.screenshot(self.folder / f"{self._shots:02d}.png")


def _just_asking(message: str) -> bool:
    """A question back to support (no yes, no no) does not answer the offer yet."""
    return asks_back(message)


def _offer_lines(offer: str, message: str, accepting: bool) -> list[str]:
    return [f"Support offered: \"{offer}\"",
            f"If you {'accept' if accepting else 'turn it down'}, I'll reply: \"{message}\""]


def _reasons(findings: list[Finding]) -> str:
    return "; ".join(f"{f.why} ({f.text})" for f in findings)


def _question(last: str, message: str, findings: list[Finding]) -> str:
    lines = []
    if last:
        lines.append(f"Support said: \"{last}\"")
    lines.append(f"I want to reply: \"{message}\"")
    lines.append("This needs your OK because " + _reasons(findings) + ".")
    return "\n".join(lines)
