"""Importing the wake path must not drag in scipy or scikit-learn.

openwakeword/__init__.py eagerly imports custom_verifier_model, which
imports scipy and sklearn at top level (~25 MB RSS, ~2.5 s on a Pi) for a
training-only feature the node never uses. core.oww_import stubs it, and
the decimator in core.resample replaced scipy.signal.resample_poly.

Each check runs in a fresh interpreter: sys.modules in the pytest process
is already polluted by whatever other tests imported.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _require(module: str) -> None:
    """Skip unless ``module`` is really installed.

    Not importorskip: importing openwakeword without the stub is exactly
    what fails on a venv that lacks scikit-learn. And the check runs in a
    subprocess because other tests leave fake modules (no __spec__) in this
    process's sys.modules, which makes find_spec raise here.
    """
    probe = f"import importlib.util, sys; sys.exit(importlib.util.find_spec({module!r}) is None)"
    if subprocess.run([sys.executable, "-c", probe], cwd=REPO).returncode != 0:
        pytest.skip(f"{module} not installed")


def _modules_after(code: str) -> dict:
    probe = (
        "import sys, json\n"
        f"{code}\n"
        "print(json.dumps({'scipy': 'scipy' in sys.modules, 'sklearn': 'sklearn' in sys.modules,"
        " 'oww': 'openwakeword' in sys.modules}))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_stub_then_openwakeword_model_skips_scipy_and_sklearn() -> None:
    _require("openwakeword")
    mods = _modules_after(
        "import core.oww_import\nfrom openwakeword.model import Model"
    )
    assert mods == {"scipy": False, "sklearn": False, "oww": True}


def test_wake_loop_and_barge_in_do_not_import_scipy() -> None:
    _require("pyaudio")
    mods = _modules_after("import core.wake_loop, core.barge_in")
    assert mods["scipy"] is False
    assert mods["sklearn"] is False


def test_voice_listener_import_skips_scipy_and_sklearn() -> None:
    _require("openwakeword")
    _require("pyaudio")
    mods = _modules_after("import scripts.voice_listener")
    assert mods == {"scipy": False, "sklearn": False, "oww": True}


def test_stub_is_idempotent_and_keeps_existing_module() -> None:
    import types

    import core.oww_import as oww_import

    name = "openwakeword.custom_verifier_model"
    saved = sys.modules.get(name)
    sentinel = types.ModuleType(name)
    sys.modules[name] = sentinel
    try:
        oww_import.install_stub()
        assert sys.modules[name] is sentinel  # never replaces a real/loaded module
    finally:
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


def test_stubbed_train_custom_verifier_raises_clearly() -> None:
    import core.oww_import as oww_import

    stub = oww_import.make_stub()
    with pytest.raises(RuntimeError, match="not available on the node"):
        stub.train_custom_verifier()


WAKE_PATH_MODULES = [
    "core/wake_loop.py",
    "core/barge_in.py",
    "core/resample.py",
    "core/wake_models.py",
    "scripts/voice_listener.py",
]


@pytest.mark.parametrize("rel_path", WAKE_PATH_MODULES)
def test_wake_path_never_imports_scipy_even_lazily(rel_path: str) -> None:
    """The old code imported scipy lazily on the first audio chunk, so an
    import-time check can't see it. Walk the AST for any scipy import."""
    import ast

    tree = ast.parse((REPO / rel_path).read_text())
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            offenders += [a.name for a in node.names if a.name.split(".")[0] == "scipy"]
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "scipy":
            offenders.append(node.module)
    assert offenders == [], f"{rel_path} imports {offenders}"
