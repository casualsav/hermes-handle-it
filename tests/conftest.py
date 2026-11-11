"""Load the plugin directory as the package ``support_chat_plugin``, the way Hermes's loader
imports a directory plugin, so its relative imports resolve. Hermes core is not needed."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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
