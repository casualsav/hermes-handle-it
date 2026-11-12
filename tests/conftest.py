"""Load the plugin directory as the package ``support_chat_plugin``, the way Hermes's loader
imports a directory plugin, so its relative imports resolve. Hermes core is not needed."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

PACKAGE = "support_chat_plugin"
ROOT = Path(__file__).resolve().parents[1]
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != ROOT]


def _load():
    if PACKAGE in sys.modules:
        return sys.modules[PACKAGE]
    spec = importlib.util.spec_from_file_location(PACKAGE, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE] = module
    spec.loader.exec_module(module)
    return module


_load()


class FakeCtx:
    """The parts of Hermes's PluginContext the plugin uses: settings, ``emit`` and ``dispatch_tool``
    (which runs Hermes's clarify tool with the callback given)."""

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.events = []
        self.dispatched = []

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def emit(self, event, payload=None):
        self.events.append((event, payload or {}))
        return 0

    def dispatch_tool(self, name, args, **kwargs):
        import json

        self.dispatched.append((name, args))
        reply = kwargs["callback"]([{"qid": "q0", **args["questions"][0], "multi_select": False}])
        answer = (reply.get("answers") or {}).get("q0")
        return json.dumps({"responses": [{"status": "answered" if answer else "no_answer",
                                          "user_response": answer}]})

    def progress(self):
        return [p["text"] for e, p in self.events if e == "progress"]


@pytest.fixture()
def make_ctx():
    return FakeCtx
