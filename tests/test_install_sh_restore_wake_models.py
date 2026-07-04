"""Tests for restore_wake_models() in install.sh.

The openWakeWord models live INSIDE the venv (site-packages/openwakeword/
resources/models/), so every node update loses them: the CI tarball's
bundled venv never contains them and rebuild_venv wipes them. Since
v0.1.130 autodownload is opt-in (default OFF), nothing re-fetches them
and the voice listener boots headless — wake word dead while every
health signal stays green (Jul-2026 prod kitchen incident).

restore_wake_models() copies the model files forward from the pre-update
install at ${INSTALL_DIR}.bak, the same way restore_pantry_pip_deps
restores Pantry pip deps.

Pins:
  - models in the .bak venv are copied into the new venv (any python
    version on either side)
  - already-staged models are never clobbered
  - missing .bak / un-importable openwakeword degrade to a loud warning,
    never a failed install (function runs under `set -euo pipefail`)
  - the main install flow calls restore_wake_models after
    restore_pantry_pip_deps
"""

import re
import stat
import subprocess
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = _REPO_ROOT / "install.sh"

MODEL_FILES = [
    "hey_jarvis_v0.1.onnx",
    "embedding_model.onnx",
    "melspectrogram.onnx",
    "silero_vad.onnx",
]


def _extract_function(name: str) -> str:
    """Extract a top-level function body from install.sh (functions in this
    file close with a `}` at column 0)."""
    text = INSTALL_SH.read_text()
    match = re.search(rf"^{name}\(\)\s*\{{.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name}() function not found in install.sh"
    return match.group(0)


def _make_venv_python(install_dir: Path, models_dir: Path | None) -> None:
    """Stub the new venv's python. restore_wake_models asks it (via a
    heredoc on stdin) where openwakeword's models dir is; the stub drains
    stdin and prints the sandbox path — or fails when models_dir is None
    to simulate openwakeword not being importable."""
    bin_dir = install_dir / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    python = bin_dir / "python"
    if models_dir is None:
        python.write_text("#!/bin/sh\ncat >/dev/null\nexit 1\n")
    else:
        python.write_text(f'#!/bin/sh\ncat >/dev/null\necho "{models_dir}"\n')
    python.chmod(python.stat().st_mode | stat.S_IEXEC)


def _run_restore(tmp_path: Path) -> subprocess.CompletedProcess:
    """Run the extracted function in a bash sandbox under the same shell
    options as install.sh."""
    harness = "\n".join(
        [
            "set -euo pipefail",
            'info() { echo "INFO: $*"; }',
            'warn() { echo "WARN: $*"; }',
            'success() { echo "SUCCESS: $*"; }',
            f'INSTALL_DIR="{tmp_path}/jarvis-node"',
            _extract_function("restore_wake_models"),
            "restore_wake_models",
        ]
    )
    return subprocess.run(
        ["bash", "-c", harness], capture_output=True, text=True, timeout=30
    )


def _seed_backup_models(tmp_path: Path, py_ver: str = "python3.11") -> Path:
    models = (
        tmp_path
        / "jarvis-node.bak"
        / ".venv"
        / "lib"
        / py_ver
        / "site-packages"
        / "openwakeword"
        / "resources"
        / "models"
    )
    models.mkdir(parents=True)
    for name in MODEL_FILES:
        (models / name).write_bytes(b"fake-onnx-" + name.encode())
    return models


def _new_models_dir(tmp_path: Path) -> Path:
    # Deliberately a different python version than the backup: the whole
    # point is surviving a cross-version venv rebuild.
    return (
        tmp_path
        / "jarvis-node"
        / ".venv"
        / "lib"
        / "python3.13"
        / "site-packages"
        / "openwakeword"
        / "resources"
        / "models"
    )


class TestRestoreWakeModels:
    def test_copies_all_models_from_backup_venv(self, tmp_path):
        _seed_backup_models(tmp_path)
        dest = _new_models_dir(tmp_path)
        _make_venv_python(tmp_path / "jarvis-node", dest)

        result = _run_restore(tmp_path)

        assert result.returncode == 0, result.stderr
        for name in MODEL_FILES:
            assert (dest / name).exists(), f"{name} not restored"
        assert "Restored 4" in result.stdout

    def test_copies_tflite_models_too(self, tmp_path):
        src = _seed_backup_models(tmp_path)
        (src / "hey_jarvis_v0.1.tflite").write_bytes(b"fake-tflite")
        dest = _new_models_dir(tmp_path)
        _make_venv_python(tmp_path / "jarvis-node", dest)

        result = _run_restore(tmp_path)

        assert result.returncode == 0, result.stderr
        assert (dest / "hey_jarvis_v0.1.tflite").exists()

    def test_never_clobbers_already_staged_model(self, tmp_path):
        _seed_backup_models(tmp_path)
        dest = _new_models_dir(tmp_path)
        dest.mkdir(parents=True)
        (dest / "hey_jarvis_v0.1.onnx").write_bytes(b"freshly-downloaded")
        _make_venv_python(tmp_path / "jarvis-node", dest)

        result = _run_restore(tmp_path)

        assert result.returncode == 0, result.stderr
        assert (dest / "hey_jarvis_v0.1.onnx").read_bytes() == b"freshly-downloaded"
        # The other three still restore.
        assert (dest / "melspectrogram.onnx").exists()

    def test_missing_backup_warns_but_succeeds(self, tmp_path):
        dest = _new_models_dir(tmp_path)
        _make_venv_python(tmp_path / "jarvis-node", dest)

        result = _run_restore(tmp_path)

        assert result.returncode == 0, result.stderr
        assert "wake word will NOT work" in result.stdout
        assert "wake_word_model_autodownload_enabled" in result.stdout

    def test_no_warning_when_models_already_staged_and_no_backup(self, tmp_path):
        dest = _new_models_dir(tmp_path)
        dest.mkdir(parents=True)
        for name in MODEL_FILES:
            (dest / name).write_bytes(b"already-here")
        _make_venv_python(tmp_path / "jarvis-node", dest)

        result = _run_restore(tmp_path)

        assert result.returncode == 0, result.stderr
        assert "NOT work" not in result.stdout

    def test_unimportable_openwakeword_skips_gracefully(self, tmp_path):
        _seed_backup_models(tmp_path)
        _make_venv_python(tmp_path / "jarvis-node", models_dir=None)

        result = _run_restore(tmp_path)

        assert result.returncode == 0, result.stderr
        assert "skipping wake-model restore" in result.stdout

    def test_backup_with_different_python_version_still_found(self, tmp_path):
        _seed_backup_models(tmp_path, py_ver="python3.9")
        dest = _new_models_dir(tmp_path)
        _make_venv_python(tmp_path / "jarvis-node", dest)

        result = _run_restore(tmp_path)

        assert result.returncode == 0, result.stderr
        assert (dest / "hey_jarvis_v0.1.onnx").exists()


class TestInstallFlowWiring:
    def test_restore_wake_models_called_after_restore_pantry_pip_deps(self):
        text = INSTALL_SH.read_text()
        assert re.search(
            r"^\s*restore_pantry_pip_deps\s*\n\s*restore_wake_models\s*$",
            text,
            re.MULTILINE,
        ), "main flow must call restore_wake_models right after restore_pantry_pip_deps"
