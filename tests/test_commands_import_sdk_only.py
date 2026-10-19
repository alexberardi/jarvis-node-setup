"""Every built-in command module imports with only jarvis-command-sdk installed.

go-pi-node §5.2: the commands in ``commands/`` must not need the Python
node's internals (``core``, ``services``, ``utils``, ``db``, ``clients``, …)
or its third-party dependencies (``jarvis_log_client``, sqlalchemy, …) at
import time, so another runtime (the Go Pi node's sidecar, Pantry container
validation) can load them with the SDK alone. Node-only code is reached
lazily, at call time, or through an SDK backend.

Each module is imported in its own subprocess behind a meta-path finder that
refuses every top-level module except the standard library,
``jarvis_command_sdk`` and ``commands`` itself.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMAND_MODULES = sorted(
    f"commands.{p.stem}" for p in (REPO_ROOT / "commands").glob("*_command.py")
)

_PROBE = r"""
import importlib, importlib.abc, sys

ALLOWED = set(sys.stdlib_module_names) | {"jarvis_command_sdk", "commands"}

class _SdkOnly(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        top = name.partition(".")[0]
        # _sysconfigdata_* is stdlib but platform-named, so not in the list.
        if top not in ALLOWED and not top.startswith("_sysconfigdata"):
            raise ModuleNotFoundError(f"No module named {name!r} (SDK-only import)", name=name)
        return None

# Drop anything a sitecustomize/.pth may have pulled in, then block.
for mod in [m for m in sys.modules if m.partition(".")[0] not in ALLOWED]:
    del sys.modules[mod]
sys.meta_path.insert(0, _SdkOnly())
sys.path.insert(0, sys.argv[2])
importlib.import_module(sys.argv[1])
print("OK")
"""


def test_command_modules_found() -> None:
    assert len(COMMAND_MODULES) >= 20


@pytest.mark.parametrize("module", COMMAND_MODULES)
def test_command_imports_with_sdk_only(module: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, module, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0 and result.stdout.strip() == "OK", (
        f"{module} needs more than jarvis-command-sdk to import:\n"
        f"{result.stderr.strip().splitlines()[-1] if result.stderr.strip() else result.stdout}"
    )


# Commands whose node services now have an SDK backend (timer, audio control,
# alert queue, CC LLM): they must reach them through the SDK helpers, not
# through a call-time import of the node module. Each entry lists the node
# modules the command may still import lazily (no SDK backend for them yet).
_BACKEND_COMMANDS = {
    "timer_command": set(),
    "cancel_timer_command": set(),
    "check_timers_command": set(),
    "control_node_command": set(),
    "whats_up_command": set(),
    "routine_command": {"utils.command_discovery_service", "services.secret_service"},
}
_NODE_IMPORT_RE = re.compile(
    r"^\s*(?:from|import)\s+((?:core|services|utils|db|clients|repositories)(?:\.\w+)*)",
    re.MULTILINE,
)


@pytest.mark.parametrize("name", sorted(_BACKEND_COMMANDS))
def test_command_uses_sdk_backends_not_node_modules(name: str) -> None:
    source = (REPO_ROOT / "commands" / f"{name}.py").read_text()
    imported = set(_NODE_IMPORT_RE.findall(source))
    assert imported <= _BACKEND_COMMANDS[name], (
        f"commands/{name}.py imports node modules {sorted(imported - _BACKEND_COMMANDS[name])}; "
        "use the jarvis_command_sdk helpers (timer_*, audio_*, alerts_*, llm_chat_text)"
    )
