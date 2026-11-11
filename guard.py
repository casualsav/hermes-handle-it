"""What the plugin will not send on the user's behalf without asking them first.

Two checks run on every outgoing chat message, whatever the model says it means:

* the details check: a personal detail (an email address, a phone number, a
  long number such as an account, card or order number, a secret like a
  password) may go out only if the user allowed it, the user wrote it in the
  task, or the support agent said it first;
* the commitment check: agreeing to an offer, paying, buying, cancelling or
  authorising anything needs the user's yes.

Both are deliberately strict: a false alarm costs the user one tap, a miss can
cost money or privacy.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
LONG_NUMBER = re.compile(r"(?<![\w$€£.,])[A-Za-z]{0,4}-?\d[\d-]{3,}\d?(?![\w%])")
SECRET_WORDS = re.compile(
    r"\b(password|passcode|pin|cvv|cvc|security code|social security|ssn|one[- ]time code|otp|"
    r"verification code|2fa code|mother'?s maiden name)\b",
    re.I,
)

AFFIRM = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok(?:ay)?|accept(?:ed)?|agree[sd]?|go ahead|please do|please proceed|"
    r"proceed|please process|process it|sounds good|that works|works for me|deal|i'?ll take|let'?s do)\b",
    re.I,
)
PAYMENT = re.compile(
    r"\b(pay|paying|charge|purchase|buy|order (?:it|one|the)|upgrade|subscribe|sign (?:me )?up|"
    r"authori[sz]e|renew|cancel (?:my|the) (?:account|subscription|plan|membership|order))\b",
    re.I,
)
OFFER = re.compile(
    r"(\boffer|\brefund|\bcredit|\bdiscount|\breplace|\bexchange|\bvoucher|\bcoupon|would that work|does that work|"
    r"shall i|should i|do you want|would you like|go ahead|\bprocess|\bcharge|\bfee\b|\bprice|\bcost|\bplan\b|"
    r"\bupgrade|\bcancel|\bsubscri|\bdeal\b|\bagree|\baccept|\$|€|£)",
    re.I,
)
CONSENT = re.compile(
    r"(would (?:that|this) work|does (?:that|this) work|works? for you|shall i|should i|do you want|would you like|"
    r"go ahead|is (?:that|this) (?:ok|okay|alright|acceptable)|sound good|accept|agree|deal\?|can i proceed|proceed\?)",
    re.I,
)


def is_offer(text: str) -> bool:
    """Support offers something and asks for a yes: "I can refund $36. Would that work?"."""
    return bool(OFFER.search(text) and CONSENT.search(text) and "?" in text)


REQUEST = re.compile(
    r"(can you (?:confirm|provide|share|give|send|tell)|could you (?:confirm|provide|share|give|send|tell)|"
    r"please (?:confirm|provide|share|send)|what(?:'s| is) (?:your|the)|may i have|i'?ll need|i need your|verify)",
    re.I,
)
CARD = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")
NEGATION = re.compile(r"\b(no|not|don'?t|do not|won'?t|can'?t|cannot|never|decline|rather not|instead)\b|n't\b", re.I)


@dataclass
class Finding:
    kind: str  # "detail" or "commitment"
    text: str  # what was found
    why: str  # one line for the user

    def as_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "why": self.why}


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9@.]", "", value.lower())


@dataclass
class Guard:
    """Holds what the user allowed and what the chat already contains."""

    allowed: dict = field(default_factory=dict)  # label -> value the user allows sharing
    private: list = field(default_factory=list)  # values the user said never to share
    task: str = ""  # the user's own words for the goal
    heard: list = field(default_factory=list)  # what the support agent has said

    def allowed_values(self) -> list[str]:
        values = [str(v) for v in self.allowed.values() if str(v).strip()]
        return [_norm(v) for v in values]

    def _known(self, found: str) -> bool:
        n = _norm(found)
        if not n:
            return True
        digits = re.sub(r"\D", "", found)
        pool = self.allowed_values() + [_norm(self.task)] + [_norm(h) for h in self.heard]
        for known in pool:
            if n in known:
                return True
            if digits and len(digits) >= 4 and digits in re.sub(r"\D", "", known):
                # "order 1234" vs "#1234": the digits are what identify it
                if digits == re.sub(r"\D", "", known) or digits in known:
                    return True
        return False

    def details(self, message: str) -> list[Finding]:
        out: list[Finding] = []
        seen: set[str] = set()

        def add(text: str, why: str) -> None:
            key = _norm(text)
            if key in seen:
                return
            seen.add(key)
            out.append(Finding("detail", text, why))

        for m in EMAIL.finditer(message):
            if not self._known(m.group()):
                add(m.group(), "an email address you did not list as shareable")
        emails = " ".join(m.group() for m in EMAIL.finditer(message))
        for m in PHONE.finditer(message):
            text = m.group().strip()
            if len(re.sub(r"\D", "", text)) >= 9 and text not in emails and not self._known(text):
                add(text, "a phone or card-length number you did not list as shareable")
        for m in LONG_NUMBER.finditer(message):
            text = m.group().strip("-")
            if sum(c.isdigit() for c in text) < 4 or text in emails:
                continue
            if any(_norm(text) in _norm(f.text) for f in out):
                continue
            if not self._known(text):
                add(text, "a number (account, card, order or ID) you did not list as shareable")
        flat = _norm(message)
        for value in self.private:
            if _norm(str(value)) and _norm(str(value)) in flat:
                add(str(value), "you said never to share this")
        for m in SECRET_WORDS.finditer(message):
            add(m.group(), "it looks like a password, code or other secret")
        return out

    def asked_unlisted(self, message: str) -> list[Finding]:
        """The agent asked for something and the reply gives none of the shareable details:
        whatever it gives instead is the user's call."""
        last = self.heard[-1] if self.heard else ""
        if not REQUEST.search(last) or NEGATION.search(message):
            return []
        flat = _norm(message)
        if any(v and v in flat for v in self.allowed_values()):
            return []
        return [Finding("detail", message.strip(), "the support agent asked for a detail that is not on your shareable list")]

    def hard_block(self, message: str) -> list[Finding]:
        """Never typed, even with a yes: payment card numbers. A charge is the user's to make."""
        out = []
        for m in CARD.finditer(message):
            digits = re.sub(r"\D", "", m.group())
            if 13 <= len(digits) <= 19 and _luhn(digits):
                out.append(Finding("blocked", m.group(), "a payment card number; paying is left to you"))
        return out

    def commitments(self, message: str) -> list[Finding]:
        """A "yes" counts as agreeing only when the agent's last message offered or asked to do
        something; paying, buying or cancelling counts always."""
        last = self.heard[-1] if self.heard else ""
        offer_pending = is_offer(last)
        out: list[Finding] = []
        for sentence in re.split(r"(?<=[.!?\n])\s+", message):
            if not sentence.strip():
                continue
            pay = PAYMENT.search(sentence)
            if pay and not NEGATION.search(sentence[: pay.start()]):
                out.append(Finding("commitment", sentence.strip(), "it pays, buys, cancels or authorises something"))
                continue
            affirm = AFFIRM.search(sentence)
            if affirm and offer_pending and not NEGATION.search(sentence):
                out.append(Finding("commitment", sentence.strip(), "it agrees to what the support agent offered"))
        return out

    def check(self, message: str, declared_intent: str = "") -> list[Finding]:
        found = self.details(message) + self.commitments(message)
        if not found:
            found = self.asked_unlisted(message)
        if declared_intent in ("agree", "pay") and not any(f.kind == "commitment" for f in found):
            found.append(Finding("commitment", message.strip(), f"you marked this message as '{declared_intent}'"))
        return found


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def summarize(findings: Iterable[Finding]) -> str:
    return "; ".join(f"{f.text!r}: {f.why}" for f in findings)
