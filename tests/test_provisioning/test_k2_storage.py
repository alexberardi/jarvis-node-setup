"""
Unit tests for K2 key storage in encryption_utils.

TDD: These tests are written first, before the full implementation.
"""

import base64
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from utils.encryption_utils import (
    clear_k2,
    get_k2,
    has_k2,
    initialize_encryption_key,
    save_k2,
)


@pytest.fixture
def temp_secret_dir(tmp_path):
    """Create a temporary secret directory with K1 initialized."""
    secret_dir = tmp_path / ".jarvis"
    secret_dir.mkdir()
    with patch("utils.encryption_utils.get_secret_dir", return_value=secret_dir):
        # Initialize K1 so encryption works
        initialize_encryption_key()
        yield secret_dir


def make_valid_k2_base64url() -> str:
    """Generate a valid 32-byte K2 as base64url."""
    return base64.urlsafe_b64encode(b"A" * 32).decode()


class TestSaveK2:
    """Test saving K2 key."""

    def test_save_creates_encrypted_files(self, temp_secret_dir):
        k2_b64 = make_valid_k2_base64url()
        created_at = datetime(2026, 2, 1, 13, 0, 0, tzinfo=timezone.utc)

        save_k2(k2_b64, "k2-2026-01", created_at)

        # Check files exist
        assert (temp_secret_dir / "k2.enc").exists()
        assert (temp_secret_dir / "k2_metadata.json").exists()

    def test_save_encrypts_key(self, temp_secret_dir):
        k2_raw = b"B" * 32
        k2_b64 = base64.urlsafe_b64encode(k2_raw).decode()
        created_at = datetime(2026, 2, 1, 13, 0, 0, tzinfo=timezone.utc)

        save_k2(k2_b64, "k2-2026-01", created_at)

        # Verify the raw key is not stored in plaintext
        enc_file = temp_secret_dir / "k2.enc"
        content = enc_file.read_bytes()
        assert k2_raw not in content

    def test_save_rejects_wrong_size(self, temp_secret_dir):
        # 16 bytes instead of 32
        k2_b64 = base64.urlsafe_b64encode(b"A" * 16).decode()
        created_at = datetime.now(timezone.utc)

        with pytest.raises(ValueError, match="must be exactly 32 bytes"):
            save_k2(k2_b64, "k2-test", created_at)

    def test_save_rejects_invalid_base64(self, temp_secret_dir):
        created_at = datetime.now(timezone.utc)

        # Use truly invalid base64 with wrong padding
        with pytest.raises(ValueError):
            save_k2("not-valid!!!", "k2-test", created_at)

    def test_save_overwrites_existing(self, temp_secret_dir):
        created_at = datetime.now(timezone.utc)

        # Save first key
        k2_first = base64.urlsafe_b64encode(b"1" * 32).decode()
        save_k2(k2_first, "k2-first", created_at)

        # Save second key
        k2_second = base64.urlsafe_b64encode(b"2" * 32).decode()
        save_k2(k2_second, "k2-second", created_at)

        # Should load the second key
        result = get_k2()
        assert result is not None
        assert result.kid == "k2-second"
        assert result.k2 == b"2" * 32


class TestGetK2:
    """Test loading K2 key."""

    def test_get_returns_none_if_not_exists(self, temp_secret_dir):
        result = get_k2()
        assert result is None

    def test_get_returns_saved_key(self, temp_secret_dir):
        k2_raw = b"C" * 32
        k2_b64 = base64.urlsafe_b64encode(k2_raw).decode()
        created_at = datetime(2026, 2, 1, 13, 0, 0, tzinfo=timezone.utc)

        save_k2(k2_b64, "k2-2026-01", created_at)

        result = get_k2()
        assert result is not None
        assert result.k2 == k2_raw
        assert result.kid == "k2-2026-01"
        assert result.created_at == created_at

    def test_get_returns_none_if_only_enc_exists(self, temp_secret_dir):
        # Only create k2.enc without metadata
        (temp_secret_dir / "k2.enc").write_bytes(b"fake data")

        result = get_k2()
        assert result is None

    def test_get_returns_none_if_only_metadata_exists(self, temp_secret_dir):
        # Only create metadata without k2.enc
        import json
        metadata = {"kid": "k2-test", "created_at": "2026-02-01T13:00:00+00:00"}
        (temp_secret_dir / "k2_metadata.json").write_text(json.dumps(metadata))

        result = get_k2()
        assert result is None


class TestHasK2:
    """Test checking if K2 exists."""

    def test_has_k2_false_when_not_stored(self, temp_secret_dir):
        assert has_k2() is False

    def test_has_k2_true_when_stored(self, temp_secret_dir):
        save_k2(make_valid_k2_base64url(), "k2-test", datetime.now(timezone.utc))
        assert has_k2() is True


class TestClearK2:
    """Test clearing K2 key."""

    def test_clear_removes_files(self, temp_secret_dir):
        save_k2(make_valid_k2_base64url(), "k2-test", datetime.now(timezone.utc))
        assert has_k2() is True

        clear_k2()
        assert has_k2() is False
        assert not (temp_secret_dir / "k2.enc").exists()
        assert not (temp_secret_dir / "k2_metadata.json").exists()

    def test_clear_no_error_if_not_exists(self, temp_secret_dir):
        # Should not raise
        clear_k2()


class TestK2RoundTrip:
    """Test full round-trip of K2 storage."""

    def test_full_flow(self, temp_secret_dir):
        # Initially empty
        assert has_k2() is False
        assert get_k2() is None

        # Save
        k2_raw = b"D" * 32
        k2_b64 = base64.urlsafe_b64encode(k2_raw).decode()
        created_at = datetime(2026, 2, 1, 13, 0, 0, tzinfo=timezone.utc)
        save_k2(k2_b64, "k2-2026-01", created_at)

        # Has
        assert has_k2() is True

        # Load
        result = get_k2()
        assert result is not None
        assert result.k2 == k2_raw
        assert result.kid == "k2-2026-01"
        assert result.created_at == created_at

        # Clear
        clear_k2()
        assert has_k2() is False
        assert get_k2() is None
