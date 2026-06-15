"""Tests for services.routine_sync_service.pull_routines().

Verifies the pull-on-nudge contract:
  - server routines are written to the DB layer
  - DB routines the server no longer has are pruned
  - hardcoded defaults / Pantry custom routines are NEVER pruned
  - an unreachable CC leaves the local store untouched (no prune)
"""

from unittest.mock import MagicMock, patch

import services.routine_sync_service as sync


class FakeRepo:
    """In-memory stand-in for CommandDataRepository."""

    def __init__(self, existing):
        # existing: {slug: definition}
        self.store = dict(existing)
        self.saved = []
        self.deleted = []

    def save(self, command_name, data_key, data):
        self.store[data_key] = data
        self.saved.append(data_key)

    def get_all(self, command_name):
        return [{"_data_key": k, **(v if isinstance(v, dict) else {})} for k, v in self.store.items()]

    def delete(self, command_name, data_key):
        self.store.pop(data_key, None)
        self.deleted.append(data_key)
        return True


def _run(server_payload, existing=None, protected=None, cc_returns=True):
    existing = existing or {}
    protected = protected or {"good_morning"}
    repo = FakeRepo(existing)

    fake_session = MagicMock()
    with patch.object(sync.Config, "get_str", return_value="test-node"), \
         patch.object(sync, "get_command_center_url", return_value="http://cc"), \
         patch.object(sync.RestClient, "get",
                      return_value=({"routines": server_payload} if cc_returns else None)), \
         patch.object(sync, "SessionLocal", return_value=fake_session), \
         patch.object(sync, "CommandDataRepository", return_value=repo), \
         patch.object(sync, "_protected_slugs", return_value=set(protected)):
        count = sync.pull_routines()
    return count, repo


def test_writes_server_routines():
    payload = {
        "good_morning": {"trigger_phrases": ["good morning"], "steps": [], "response_length": "short"},
        "bedtime": {"trigger_phrases": ["bedtime"], "steps": [], "response_length": "short"},
    }
    count, repo = _run(payload, existing={})
    assert count == 2
    assert set(repo.saved) == {"good_morning", "bedtime"}
    assert "bedtime" in repo.store


def test_prunes_server_removed_routine():
    # Server only has good_morning now; the node DB also has a stale "old_one".
    payload = {"good_morning": {"trigger_phrases": ["gm"], "steps": []}}
    existing = {"good_morning": {}, "old_one": {}}
    _, repo = _run(payload, existing=existing, protected={"good_morning"})
    assert "old_one" in repo.deleted
    assert "old_one" not in repo.store


def test_never_prunes_protected_defaults():
    # Server has no routines; a default (good_night) lives in the DB and must stay.
    payload = {}
    existing = {"good_night": {}}
    _, repo = _run(payload, existing=existing, protected={"good_morning", "good_night"})
    assert "good_night" not in repo.deleted
    assert "good_night" in repo.store


def test_unreachable_cc_no_prune():
    existing = {"good_morning": {}, "stale": {}}
    count, repo = _run({}, existing=existing, cc_returns=False)
    assert count == 0
    assert repo.deleted == []  # nothing pruned when CC is unreachable
    assert "stale" in repo.store
