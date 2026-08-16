"""Tests for core/wake_models.py — bundled-first wake-model resolution.

Contract under test:
  * a bundled repo model at <project_root>/models/wake/<name>.onnx wins,
    resolved to an absolute path;
  * absent a bundled model, resolution falls back to the package-resident
    name (openwakeword's own resources dir) — byte-for-byte the previous
    behavior, including the consent-gated autodownload;
  * a BUNDLED model short-circuits autodownload entirely (no egress even
    when the setting is on);
  * the single runtime openWakeWord construction site
    (scripts/voice_listener.py) goes through the resolver, and no other
    runtime module constructs its own model — the barge-in monitor shares
    the voice_listener instance, so resolver coverage there is structural;
  * the missing-model error path is unchanged: OWWModel raising still
    lands in voice_listener's keyboard/headless fallback.
"""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from core.wake_models import (
    SOURCE_BUNDLED,
    SOURCE_PACKAGE,
    ResolvedWakeModel,
    prepare_wake_model,
    resolve_wake_model,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def oww_download_stub(monkeypatch):
    """Stub openwakeword.utils.download_models; return the mock.

    Defensive against other test files having already stubbed
    openwakeword in this process (e.g. test_boot_warmup does so at
    import time) — reuse whatever module object is present and only
    patch the download_models attribute.
    """
    parent = sys.modules.get("openwakeword")
    if parent is None:
        parent = types.ModuleType("openwakeword")
        monkeypatch.setitem(sys.modules, "openwakeword", parent)
    utils_mod = sys.modules.get("openwakeword.utils")
    if utils_mod is None:
        utils_mod = types.ModuleType("openwakeword.utils")
        monkeypatch.setitem(sys.modules, "openwakeword.utils", utils_mod)
    download = MagicMock()
    monkeypatch.setattr(utils_mod, "download_models", download, raising=False)
    monkeypatch.setattr(parent, "utils", utils_mod, raising=False)
    return download


@pytest.fixture
def bundled_dir(tmp_path):
    d = tmp_path / "models" / "wake"
    d.mkdir(parents=True)
    return d


def _stage(bundled_dir: Path, name: str) -> Path:
    p = bundled_dir / f"{name}.onnx"
    p.write_bytes(b"fake-onnx")
    return p


# ---------------------------------------------------------------------------
# resolve_wake_model — resolution order
# ---------------------------------------------------------------------------

class TestResolveWakeModel:
    def test_bundled_hit_wins_with_absolute_path(self, bundled_dir):
        staged = _stage(bundled_dir, "hey_jarvis")

        resolved = resolve_wake_model("hey_jarvis", bundled_dir=bundled_dir)

        assert resolved.source == SOURCE_BUNDLED
        assert resolved.path == str(staged)
        assert Path(resolved.path).is_absolute()
        # openWakeWord gets the file path, not the bare name.
        assert resolved.model_ref == str(staged)

    def test_falls_back_to_package_when_absent(self, bundled_dir):
        resolved = resolve_wake_model("hey_jarvis", bundled_dir=bundled_dir)

        assert resolved.source == SOURCE_PACKAGE
        assert resolved.path is None
        # Package fallback hands openWakeWord the bare model name —
        # exactly the pre-resolver behavior.
        assert resolved.model_ref == "hey_jarvis"

    def test_name_must_match_exactly(self, bundled_dir):
        """hey_jarvis_music.onnx must not satisfy a hey_jarvis lookup."""
        _stage(bundled_dir, "hey_jarvis_music")

        assert (
            resolve_wake_model("hey_jarvis", bundled_dir=bundled_dir).source
            == SOURCE_PACKAGE
        )
        assert (
            resolve_wake_model(
                "hey_jarvis_music", bundled_dir=bundled_dir
            ).source
            == SOURCE_BUNDLED
        )

    def test_missing_bundled_dir_is_package(self, tmp_path):
        resolved = resolve_wake_model(
            "hey_jarvis", bundled_dir=tmp_path / "nonexistent"
        )
        assert resolved.source == SOURCE_PACKAGE

    def test_directory_named_like_model_is_not_a_hit(self, bundled_dir):
        (bundled_dir / "hey_jarvis.onnx").mkdir()
        resolved = resolve_wake_model("hey_jarvis", bundled_dir=bundled_dir)
        assert resolved.source == SOURCE_PACKAGE

    def test_default_dir_is_repo_models_wake(self):
        from core import wake_models

        repo_root = Path(wake_models.__file__).resolve().parent.parent
        assert (
            wake_models.BUNDLED_WAKE_MODEL_DIR == repo_root / "models" / "wake"
        )


# ---------------------------------------------------------------------------
# prepare_wake_model — autodownload interaction
# ---------------------------------------------------------------------------

class TestPrepareWakeModel:
    def test_bundled_short_circuits_autodownload(
        self, bundled_dir, oww_download_stub
    ):
        """No egress for bundled models, even with autodownload enabled."""
        _stage(bundled_dir, "hey_jarvis")

        resolved = prepare_wake_model(
            "hey_jarvis", autodownload_enabled=True, bundled_dir=bundled_dir
        )

        assert resolved.source == SOURCE_BUNDLED
        oww_download_stub.assert_not_called()

    def test_package_with_autodownload_enabled_downloads(
        self, bundled_dir, oww_download_stub
    ):
        resolved = prepare_wake_model(
            "hey_jarvis", autodownload_enabled=True, bundled_dir=bundled_dir
        )

        assert resolved.source == SOURCE_PACKAGE
        oww_download_stub.assert_called_once_with(model_names=["hey_jarvis"])

    def test_package_with_autodownload_disabled_does_not_download(
        self, bundled_dir, oww_download_stub
    ):
        resolved = prepare_wake_model(
            "hey_jarvis", autodownload_enabled=False, bundled_dir=bundled_dir
        )

        assert resolved.source == SOURCE_PACKAGE
        oww_download_stub.assert_not_called()

    def test_download_failure_propagates(self, bundled_dir, oww_download_stub):
        """A failing download raises — voice_listener's existing try/except
        turns it into the keyboard/headless fallback, unchanged."""
        oww_download_stub.side_effect = RuntimeError("offline")

        with pytest.raises(RuntimeError, match="offline"):
            prepare_wake_model(
                "hey_jarvis",
                autodownload_enabled=True,
                bundled_dir=bundled_dir,
            )


# ---------------------------------------------------------------------------
# Load sites — every openWakeWord construction goes through the resolver
# ---------------------------------------------------------------------------

_RUNTIME_DIRS = ("core", "scripts", "services", "commands", "agents", "utils")


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


class TestLoadSitesUseResolver:
    def test_single_runtime_construction_site(self):
        """scripts/voice_listener.py is the ONLY runtime module that
        constructs an openWakeWord Model. The barge-in monitor
        (core/barge_in.py) shares that instance — a second construction
        site would let the two detectors diverge on model source."""
        hits = []
        for d in _RUNTIME_DIRS:
            base = _repo_root() / d
            if not base.is_dir():
                continue
            for py in base.rglob("*.py"):
                text = py.read_text(encoding="utf-8", errors="replace")
                if "wakeword_models=" in text:
                    hits.append(str(py.relative_to(_repo_root())))
        assert hits == ["scripts/voice_listener.py"], (
            f"unexpected openWakeWord construction sites: {hits} — "
            "route new load sites through core.wake_models.prepare_wake_model"
        )

    def test_voice_listener_constructs_from_resolver_ref(self):
        src = (_repo_root() / "scripts" / "voice_listener.py").read_text(
            encoding="utf-8"
        )
        assert "prepare_wake_model(" in src
        collapsed = "".join(src.split())
        assert "wakeword_models=[resolved.model_ref]" in collapsed

    def test_barge_in_has_no_independent_loader(self):
        src = (_repo_root() / "core" / "barge_in.py").read_text(
            encoding="utf-8"
        )
        assert "wakeword_models" not in src
        assert "download_models" not in src


# ---------------------------------------------------------------------------
# start_voice_listener wiring — resolver output feeds OWWModel; the
# missing-model error path (OWWModel raising) is unchanged.
# ---------------------------------------------------------------------------

def _import_voice_listener():
    """Import scripts.voice_listener with C-ext/hardware deps stubbed
    (same pattern as tests/test_boot_warmup.py)."""
    _mock_db = MagicMock()
    _mock_db.SessionLocal = MagicMock
    _mock_db.engine = MagicMock()
    if "sqlcipher3" not in sys.modules:
        sys.modules["sqlcipher3"] = MagicMock()
        sys.modules["sqlcipher3.dbapi2"] = MagicMock()
    if "db" not in sys.modules:
        sys.modules["db"] = _mock_db
    for _mod in ("openwakeword", "openwakeword.model", "openwakeword.utils"):
        if _mod not in sys.modules:
            sys.modules[_mod] = types.ModuleType(_mod)
    if not hasattr(sys.modules["openwakeword"], "Model"):
        sys.modules["openwakeword"].Model = MagicMock()
    if not hasattr(sys.modules["openwakeword.model"], "Model"):
        sys.modules["openwakeword.model"].Model = MagicMock()

    import scripts.voice_listener as voice_listener

    return voice_listener


class TestStartVoiceListenerWiring:
    def test_oww_model_receives_resolved_ref_and_error_path_unchanged(self):
        """prepare_wake_model's model_ref is what OWWModel is constructed
        with; when OWWModel raises (missing model), start_voice_listener
        falls back without raising and never opens the audio bus."""
        voice_listener = _import_voice_listener()

        resolved = ResolvedWakeModel(
            name="hey_jarvis",
            source=SOURCE_BUNDLED,
            path="/opt/jarvis-node/models/wake/hey_jarvis.onnx",
        )
        with patch.object(
            voice_listener, "prepare_wake_model", return_value=resolved
        ) as prep, patch.object(
            voice_listener, "OWWModel", side_effect=RuntimeError("no model")
        ) as oww, patch.object(
            voice_listener, "_start_keyboard_listener"
        ) as kb, patch.object(
            voice_listener, "AudioBus"
        ) as bus:
            voice_listener.start_voice_listener(None)

        prep.assert_called_once()
        args, kwargs = prep.call_args
        assert args == (voice_listener.WAKE_WORD_MODEL,)
        assert "autodownload_enabled" in kwargs
        oww.assert_called_once_with(
            wakeword_models=["/opt/jarvis-node/models/wake/hey_jarvis.onnx"],
            inference_framework="onnx",
        )
        # Missing-model fallback: no audio bus, no wake loop.
        bus.assert_not_called()
        # Keyboard fallback only when a TTY exists — either way we
        # returned instead of raising, which is the contract.
        if sys.stdin and sys.stdin.isatty():
            kb.assert_called_once()
        else:
            kb.assert_not_called()

    def test_package_fallback_ref_is_bare_name(self):
        voice_listener = _import_voice_listener()

        resolved = ResolvedWakeModel(
            name="hey_jarvis", source=SOURCE_PACKAGE, path=None
        )
        with patch.object(
            voice_listener, "prepare_wake_model", return_value=resolved
        ), patch.object(
            voice_listener, "OWWModel", side_effect=RuntimeError("no model")
        ) as oww, patch.object(
            voice_listener, "_start_keyboard_listener"
        ):
            voice_listener.start_voice_listener(None)

        oww.assert_called_once_with(
            wakeword_models=["hey_jarvis"], inference_framework="onnx"
        )
