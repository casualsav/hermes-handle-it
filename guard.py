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
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
LONG_NUMBER = re.compile(r"(?<![\w$€£.,])[A-Za-z]{0,4}-?\d[\d-]{3,}\d?(?![\w%])")
# Short digit groups that add up to a number: "12 34 56 78", "555 01 42", a code read out "12 34 56".
GROUPED = re.compile(r"(?<![\w$€£.,])\d{1,4}(?:[ .-]\d{1,4})+(?![\w%])")
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
    r"\b(pay|paying|charge|purchase (?:it|one|the|this|that|an?|another)|buy|order (?:it|one|the)|upgrade|downgrade|subscribe|unsubscribe|sign (?:me )?up|"
    r"authori[sz]e|renew|cancel(?:l?ing)?|terminate|close (?:my |the |this )?(?:account|subscription|plan|membership)|"
    r"confirm (?:the |my |this )?(?:order|purchase|payment|booking|cancell?ation|subscription|upgrade|downgrade)"
    r"(?! ?(?:number|no\b|#|id\b)))\b",
    re.I,
)
# Where a "no" stops applying: "I have no other questions, so please upgrade me" upgrades.
CLAUSE = re.compile(r"[,;:]|\s(?:so|but|and|then)\s", re.I)
OFFER = re.compile(
    r"(\boffer|\brefund|\bcredit|\bdiscount|\breplace|\bexchange|\bvoucher|\bcoupon|would that work|does that work|"
    r"shall i|should i|do you want|would you like|go ahead|\bprocess|\bcharge|\bfee\b|\bprice|\bcost|\bplan\b|"
    r"\bupgrade|\bcancel|\bsubscri|\bdeal\b|\bagree|\baccept|\$|€|£)",
    re.I,
)
CONSENT = re.compile(
    r"(would (?:that|this) work|does (?:that|this) work|works? for you|shall i|should i|do you want|would you like|"
    r"go ahead|is (?:that|this) (?:ok|okay|alright|acceptable)|sound good|accept|agree|deal\?|can i proceed|proceed\?|"
    r"(?:ok|okay|alright|fine) (?:for (?:me|us) )?to proceed|(?:may|shall|can) (?:i|we) proceed|"
    r"do you (?:confirm|approve|authori[sz]e))",
    re.I,
)


PROPOSE = re.compile(
    r"(\b(?:i|we)(?:'?d| would)? (?:can|could|will|'?ll|am able to|are able to|be (?:happy|glad|able) to|happy to)"
    r"(?: also)? (?:offer|give|issue|provide|do|refund|credit|send|replace|exchange|waive|apply|extend|upgrade|"
    r"process|arrange|ship|reduce|discount|match)\b|\boffer(?:ing|ed)?\b|\bhow about\b|\bwhat about\b|"
    r"\balternatively\b|\boption\b|\bthe best (?:i|we) can\b)",
    re.I,
)
DONE = re.compile(
    r"\b(done|processed|has been|have been|is (?:now )?complete|completed|went through|all set|anything else)\b", re.I)


def is_offer(text: str) -> bool:
    """Support offers something: "I can offer a $36 store credit.", "I can refund $36. Would that work?".
    A consent question is one by itself ("Is it ok to proceed?"), and so is a question about money that
    is not a finished action: when unsure, it is an offer."""
    text = _fold(text)
    if CONSENT.search(text):
        return True
    if not OFFER.search(text):
        return False
    if PROPOSE.search(text):
        return True
    return "?" in text and not DONE.search(text)


def asks_back(message: str) -> bool:
    """A question back to support (no yes, no no, no counter-offer) does not answer an offer yet."""
    text = _fold(message)
    return (text.rstrip().endswith("?") and not AFFIRM.search(text) and not COUNTER.search(text)
            and not NEGATION.search(BENIGN.sub(lambda m: " " * len(m.group()), text)))


REQUEST = re.compile(
    r"(can you (?:confirm|provide|share|give|send|tell)|could you (?:confirm|provide|share|give|send|tell)|"
    r"please (?:confirm|provide|share|send)|what(?:'s| is) (?:your|the)|may i have|i'?ll need|i need your|verify)",
    re.I,
)
# A run of digits with up to three spaces, newlines, dots, hyphens or slashes between them; the card is
# looked for inside it, so a number written next to it does not hide it.
CARD = re.compile(r"\d(?:[\s./-]{0,3}\d)+")
NEGATION = re.compile(r"\b(no|not|don'?t|do not|won'?t|can'?t|cannot|never|decline|rather not|instead)\b|n't\b", re.I)
# Phrases with a "no" in them that do not turn anything down.
BENIGN = re.compile(r"\b(no problem|no worries|not a problem|no rush|don'?t worry|why not|can'?t wait|can'?t complain)\b", re.I)
# Words that take what was offered without a yes in them.
ACCEPTISH = re.compile(r"\b(fine|great|perfect|good|alright|happy|glad|lovely|excellent|take it|works?)\b", re.I)
# A reply that bargains: it turns the offer down for something else, even when it ends in a question.
COUNTER = re.compile(r"\b(prefer|rather|instead|hold off|full refund)\b", re.I)
# A reply that holds back or bargains rather than accepting.
HOLDING = re.compile(
    COUNTER.pattern + r"|\b(think about|let me (?:check|think)|not sure|"
    r"(?:one more|another|a quick|quick) question|before i decide)\b",
    re.I,
)


@dataclass
class Finding:
    kind: str  # "detail" or "commitment"
    text: str  # what was found
    why: str  # one line for the user
    pay: bool = False  # it pays, buys, cancels or authorises: an accepted offer of another kind does not cover it

    def as_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "why": self.why}


def _fold(text: str) -> str:
    """Fullwidth and other compatibility forms as plain characters, so "１２３４" reads as 1234."""
    return unicodedata.normalize("NFKC", text)


class _Folded:
    """``raw`` folded for the patterns, remembering where each folded character came from,
    so a finding shows the user what was really written."""

    def __init__(self, raw: str) -> None:
        self.raw = raw
        parts: list[str] = []
        self._starts: list[int] = []
        self._ends: list[int] = []
        i = 0
        while i < len(raw):
            j = i + 1
            while j < len(raw) and unicodedata.combining(raw[j]):
                j += 1
            piece = _fold(raw[i:j])
            parts.append(piece)
            self._starts += [i] * len(piece)
            self._ends += [j] * len(piece)
            i = j
        self.text = "".join(parts)

    def span(self, start: int, end: int) -> str:
        return self.raw[self._starts[start]:self._ends[end - 1]] if end > start else ""


def _norm(value: str) -> str:
    """The key details are compared by: accents dropped, case folded, letters and digits of any script, @ and ."""
    plain = "".join(c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)).casefold()
    return "".join(c for c in plain if c.isalnum() or c in "@.")


def _digits(value: str) -> str:
    return "".join(str(unicodedata.decimal(c)) for c in value if c.isdecimal())


def _sentences(text: str):
    start = 0
    for m in re.finditer(r"(?<=[.!?\n])\s+", text):
        yield start, m.start()
        start = m.end()
    yield start, len(text)


def _negated(sentence: str, at: int) -> bool:
    """A "no" before ``at`` in the same clause."""
    start = 0
    for m in CLAUSE.finditer(sentence, 0, at):
        start = m.end()
    return bool(NEGATION.search(BENIGN.sub(lambda m: " " * len(m.group()), sentence[start:at])))


@dataclass
class Guard:
    """Holds what the user allowed and what the chat already contains."""

    allowed: dict = field(default_factory=dict)  # label -> value the user allows sharing
    private: list = field(default_factory=list)  # values the user said never to share
    task: str = ""  # the user's own words for the goal
    heard: list = field(default_factory=list)  # what the support agent has said
    answered: int = 0  # how much of ``heard`` came before our last real reply

    def allowed_values(self) -> list[str]:
        values = [str(v) for v in self.allowed.values() if str(v).strip()]
        return [_norm(v) for v in values]

    def _known(self, found: str) -> bool:
        n = _norm(found)
        if not n:
            return True
        digits = _digits(n)
        pool = self.allowed_values() + [_norm(self.task)] + [_norm(h) for h in self.heard]
        for known in pool:
            if n in known:
                return True
            if digits and len(digits) >= 4 and digits in _digits(known):
                # "order 1234" vs "#1234": the digits are what identify it
                if digits == _digits(known) or digits in known:
                    return True
        return False

    def details(self, message: str) -> list[Finding]:
        out: list[Finding] = []
        seen: set[str] = set()
        folded = _Folded(message)
        text_ = folded.text

        def add(text: str, why: str) -> None:
            key = _norm(text)
            if key in seen:
                return
            seen.add(key)
            out.append(Finding("detail", text, why))

        for m in EMAIL.finditer(text_):
            if not self._known(m.group()):
                add(folded.span(*m.span()), "an email address you did not list as shareable")
        emails = " ".join(m.group() for m in EMAIL.finditer(text_))
        for m in PHONE.finditer(text_):
            text = m.group().strip()
            if len(_digits(text)) >= 9 and text not in emails and not self._known(text):
                add(folded.span(*m.span()).strip(), "a phone or card-length number you did not list as shareable")
        for m in LONG_NUMBER.finditer(text_):
            text = m.group().strip("-")
            if len(_digits(text)) < 4 or text in emails:
                continue
            if any(_norm(text) in _norm(f.text) for f in out):
                continue
            if not self._known(text):
                lead = len(m.group()) - len(m.group().lstrip("-"))
                add(folded.span(m.start() + lead, m.start() + lead + len(text)),
                    "a number (account, card, order or ID) you did not list as shareable")
        for m in GROUPED.finditer(text_):
            groups, seps = re.split(r"[ .-]", m.group()), re.findall(r"[ .-]", m.group())
            if seps.count(".") == 1 and seps[-1] == "." and len(groups[-1]) == 2:
                groups = groups[:-1]  # a price: "180.00", "1 180.00"
            # "order 1234 14 days ago": a group the user allowed does not count toward the number
            loose = [g for g in groups if not (len(g) >= 4 and self._known(g))]
            if sum(len(g) for g in loose) < 6:
                continue
            if any(_norm(m.group()) in _norm(f.text) for f in out) or self._known(m.group()):
                continue
            add(folded.span(*m.span()), "a number (account, phone, code or ID) you did not list as shareable")
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
        folded = _Folded(message)
        for m in CARD.finditer(folded.text):
            groups = [(g.start() + m.start(), g.end() + m.start()) for g in re.finditer(r"\d+", m.group())]
            i = 0
            while i < len(groups):
                end = None
                for j in range(i, len(groups)):
                    digits = _digits(folded.text[groups[i][0]:groups[j][1]])
                    if len(digits) > 19:
                        break
                    if len(digits) >= 13 and _luhn(digits):
                        end = j
                if end is None:
                    i += 1
                    continue
                out.append(Finding("blocked", folded.span(groups[i][0], groups[end][1]),
                                   "a payment card number; paying is left to you"))
                i = end + 1
        return out

    def commitments(self, message: str) -> list[Finding]:
        """A reply to an open offer counts as agreeing unless it clearly turns it down, holds back
        or asks back; paying, buying or cancelling counts always."""
        offer_pending = any(is_offer(h) for h in self.heard[self.answered:])
        folded = _Folded(message)
        text = folded.text
        out: list[Finding] = []
        for start, end in _sentences(text):
            sentence = text[start:end]
            if not sentence.strip():
                continue
            shown = folded.span(start, end).strip()
            if any(not _negated(sentence, pay.start()) for pay in PAYMENT.finditer(sentence)):
                out.append(Finding("commitment", shown, "it pays, buys, cancels or authorises something", pay=True))
                continue
            affirm = AFFIRM.search(sentence)
            negation = NEGATION.search(BENIGN.sub(lambda m: " " * len(m.group()), sentence))
            if affirm and offer_pending and (negation is None or affirm.start() < negation.start()):
                out.append(Finding("commitment", shown, "it agrees to what the support agent offered"))
        if offer_pending and not out and text.strip() and not asks_back(text):
            plain = BENIGN.sub("", text)
            if not NEGATION.search(plain) and not HOLDING.search(plain):
                out.append(Finding("commitment", message.strip(),
                                   "it answers the support agent's offer without turning it down"))
        return out

    def check(self, message: str, declared_intent: str = "") -> list[Finding]:
        found = self.details(message) + self.commitments(message)
        if not found:
            found = self.asked_unlisted(message)
        if declared_intent in ("agree", "pay") and not any(f.kind == "commitment" for f in found):
            found.append(Finding("commitment", message.strip(), f"you marked this message as '{declared_intent}'",
                                 pay=declared_intent == "pay"))
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
