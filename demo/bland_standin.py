"""A stand-in for bland.ai's call API, so a support call can be tried without placing a real one.

It answers ``POST /v1/calls``, ``GET /v1/calls/<id>``, ``POST /v1/calls/<id>/stop`` and
``GET /v1/me`` in bland.ai's shapes, and plays one scripted call to Northwind Outfitters'
support line: a phone menu, then Dana, who offers a 20% partial refund and, if it is turned
down, the full $180. Whenever the script reaches an offer it calls the call's own
``CheckWithAccountHolder`` tool (its URL, headers and body template, as bland.ai would), and
keeps the support person holding while the answer is ``pending``.

    python demo/bland_standin.py            # http://127.0.0.1:8766, then BLAND_API_URL=http://127.0.0.1:8766
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PER_MINUTE = 0.14  # bland.ai's Start plan, per connected minute (docs.bland.ai/platform/billing)

PARTIAL = "I can offer a 20% partial refund, that's $36, and you keep the jacket. Would that work?"
FULL = "Okay, my supervisor approved the full $180 refund to the original card. Shall I go ahead?"


class Call:
    def __init__(self, payload: dict, step_s: float) -> None:
        self.id = str(uuid.uuid4())
        self.payload = payload
        self.transcripts: list[dict] = []
        self.completed = False
        self.stopped = False
        self.summary = ""
        self.step_s = step_s
        self.started = time.monotonic()
        self.tool_calls: list[dict] = []

    def say(self, user: str, text: str) -> None:
        self.transcripts.append({"id": len(self.transcripts) + 1, "user": user, "text": text,
                                 "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        time.sleep(self.step_s)

    def details(self) -> dict:
        minutes = round((time.monotonic() - self.started) / 60, 2)
        return {
            "call_id": self.id, "c_id": self.id, "to": self.payload.get("phone_number"), "from": "+15550000000",
            "status": "completed" if self.completed else "in-progress",
            "queue_status": "complete" if self.completed else "started", "completed": self.completed,
            "answered_by": "human", "call_length": minutes, "price": round(minutes * PER_MINUTE, 3),
            "summary": self.summary if self.completed else None, "metadata": self.payload.get("metadata"),
            "transcripts": list(self.transcripts), "error_message": None,
            "concatenated_transcript": "\n".join(f"{t['user']}: {t['text']}" for t in self.transcripts),
        }

    def ask(self, they_said: str, you_would_say: str) -> dict:
        """Call the call's tool the way bland.ai does: the tool's own URL, headers and body template."""
        tool = next(t for t in self.payload.get("tools", []) if isinstance(t, dict))
        values = {"input.they_said": they_said, "input.you_would_say": you_would_say}
        body = {k: re.sub(r"\{\{\s*([\w.]+)\s*\}\}", lambda m: values.get(m.group(1), ""), v)
                for k, v in tool["body"].items()}
        req = urllib.request.Request(tool["url"], data=json.dumps(body).encode(), method=tool.get("method", "POST"),
                                     headers={**tool.get("headers", {}), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=tool.get("timeout", 10000) / 1000) as resp:
            out = json.loads(resp.read())
        self.tool_calls.append({"sent": body, "got": out})
        self.say("agent-action", f"{tool['name']}: {out.get('decision')}")
        return out

    def decide(self, offer: str, yes: str) -> str:
        """The agent's answer to an offer: asked through the tool when it has one, else none."""
        if not any(isinstance(t, dict) for t in self.payload.get("tools", [])):
            self.say("assistant", "Could you note that offer on the case? The account holder will decide and "
                                  "follow up.")
            return "deferred"
        self.say("assistant", tool_speech(self.payload))
        while not self.stopped:
            out = self.ask(offer, yes)
            if out.get("decision") != "pending":
                return out.get("decision")
            self.say("assistant", "Still checking with the account holder, thank you for holding.")
            self.say("user", "No problem, I'll hold.")
        return "stop"

    def run(self) -> None:
        try:
            self.say("user", "Thank you for calling Northwind Outfitters. For orders and returns, press 1. "
                             "For billing, press 2.")
            self.say("agent-action", "Pressed 1")
            self.say("user", "Hi, this is Dana in customer care. How can I help?")
            self.say("assistant", "Hi Dana. I'm an AI assistant calling for the account holder about order 1234: "
                                  "the zipper on the Ridgeline rain jacket broke after two weeks. They'd like a full "
                                  "refund.")
            self.say("user", "I'm sorry about that. Can I have the name on the order?")
            self.say("assistant", "Sam Rivera.")
            self.say("user", PARTIAL)
            answer = self.decide(PARTIAL, "Yes, the $36 partial refund works.")
            if answer == "accepted":
                self.say("assistant", "Yes, the $36 partial refund works.")
                self.finish("Done. Refund RF-48001 for $36 is on its way.", "Partial refund of $36 accepted, RF-48001.")
                return
            if answer in ("declined", "instruction"):
                self.say("assistant", "Thanks, but the jacket is unusable after two weeks. The account holder would "
                                      "like the full $180 refund.")
                self.say("user", "Let me check with my supervisor, please hold.")
                self.say("user", FULL)
                answer = self.decide(FULL, "Yes please, go ahead with the full $180 refund.")
                if answer == "accepted":
                    self.say("assistant", "Yes please, go ahead with the full $180 refund.")
                    self.finish("Done. Refund RF-48213 for $180, five to seven business days.",
                                "Full $180 refund agreed, reference RF-48213.")
                    return
            self.say("assistant", "Thank you, the account holder will follow up. Goodbye.")
            self.finish("Okay, have a good day.", "No resolution agreed; the account holder will follow up.")
        except Exception as exc:  # the stand-in reports its own failures as the call's
            self.finish(f"(stand-in error: {exc})", f"Stand-in failed: {exc}")

    def finish(self, last: str, summary: str) -> None:
        self.say("user", last)
        self.summary = summary
        self.completed = True


def tool_speech(payload: dict) -> str:
    tool = next(t for t in payload.get("tools", []) if isinstance(t, dict))
    return tool.get("speech") or "One moment please."


class StandIn:
    def __init__(self, port: int = 0, step_s: float = 0.05, balance: float = 5.0) -> None:
        self.calls: dict[str, Call] = {}
        standin = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self) -> bool:
                if self.headers.get("authorization"):
                    return True
                self._send(401, {"status": "error", "message": "Missing authorization"})
                return False

            def do_GET(self):  # noqa: N802
                if not self._authorized():
                    return
                if self.path == "/v1/me":
                    return self._send(200, {"status": "active", "billing": {"current_balance": balance}})
                m = re.fullmatch(r"/v1/calls/([\w-]+)", self.path)
                call = standin.calls.get(m.group(1)) if m else None
                if call is None:
                    return self._send(404, {"status": "error", "message": "Call not found"})
                self._send(200, call.details())

            def do_POST(self):  # noqa: N802
                if not self._authorized():
                    return
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/v1/calls":
                    if not body.get("phone_number") or not body.get("task"):
                        return self._send(400, {"status": "error", "message": "phone_number and task are required"})
                    call = Call(body, step_s)
                    standin.calls[call.id] = call
                    threading.Thread(target=call.run, daemon=True).start()
                    return self._send(200, {"status": "success", "message": "Call successfully queued.",
                                            "call_id": call.id, "batch_id": None})
                m = re.fullmatch(r"/v1/calls/([\w-]+)/stop", self.path)
                call = standin.calls.get(m.group(1)) if m else None
                if call is None:
                    return self._send(404, {"status": "error", "message": "Call not found"})
                call.stopped = call.completed = True
                self._send(200, {"status": "success", "message": "Call ended successfully."})

            def log_message(self, *_a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


if __name__ == "__main__":
    s = StandIn(port=int(sys.argv[1]) if len(sys.argv) > 1 else 8766, step_s=1.5)
    print(f"bland.ai stand-in on {s.url}  (BLAND_API_URL={s.url})", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        s.close()
