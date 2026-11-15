"""A small client for bland.ai's call API (https://docs.bland.ai/api-v1/post/calls).

Only the four calls a support call needs: place it, read it, stop it, and the account balance.
The key is read through Hermes's profile-scoped secrets on every request; the base URL (the
``bland_api_url`` setting) can point at a stand-in (``demo/bland_standin.py``), which is how the
tests run.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.request
from typing import Optional

API = "https://api.bland.ai"


class BlandError(RuntimeError):
    """``maybe_sent``: a POST reached bland.ai but no usable answer came back, so whatever it asked
    for (a paid call) may have happened. Never retried."""

    def __init__(self, message: str, maybe_sent: bool = False) -> None:
        super().__init__(message)
        self.maybe_sent = maybe_sent


def _scoped_key() -> str:
    """BLAND_API_KEY for the profile this turn serves (``agent.secret_scope``), never another's."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:  # outside Hermes
        return ""
    try:
        return get_secret("BLAND_API_KEY") or ""
    except Exception as exc:
        raise BlandError(f"Could not read BLAND_API_KEY for this profile: {exc}") from None


class Bland:
    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, timeout_s: float = 30.0) -> None:
        self._key = api_key
        self.base = (base_url or API).rstrip("/")
        self.timeout = timeout_s

    @property
    def key(self) -> str:
        return self._key if self._key is not None else _scoped_key()

    def _request(self, method: str, path: str, body: Optional[dict] = None) -> dict:
        key = self.key
        if not key:
            raise BlandError("No bland.ai API key: add BLAND_API_KEY with `hermes config set BLAND_API_KEY ...` "
                             "or in this profile's .env.")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"authorization": key, "content-type": "application/json"})
        sent = method == "POST"
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except Exception:
                detail = ""
            raise BlandError(f"bland.ai {method} {path} answered {exc.code}: {detail}") from None
        except urllib.error.URLError as exc:  # raised only while connecting or sending: no whole request arrived
            raise BlandError(f"bland.ai is unreachable: {exc.reason}") from None
        except (OSError, http.client.HTTPException) as exc:  # sent, then a timeout or a dropped connection
            raise BlandError(f"bland.ai did not answer {method} {path}: {exc or type(exc).__name__}",
                             maybe_sent=sent) from None
        try:
            out = json.loads(raw or b"{}")
        except ValueError:
            out = None
        if not isinstance(out, dict):
            raise BlandError(f"bland.ai answered {method} {path} with something that is not a JSON object.",
                             maybe_sent=sent)
        return out

    def send_call(self, payload: dict) -> str:
        out = self._request("POST", "/v1/calls", payload)
        if out.get("status") != "success" or not out.get("call_id"):
            raise BlandError(f"bland.ai did not place the call: {out.get('message') or out.get('errors') or out}")
        return str(out["call_id"])

    def call(self, call_id: str) -> dict:
        return self._request("GET", f"/v1/calls/{call_id}")

    def stop(self, call_id: str) -> dict:
        return self._request("POST", f"/v1/calls/{call_id}/stop")

    def balance(self) -> Optional[float]:
        return (self._request("GET", "/v1/me").get("billing") or {}).get("current_balance")
