"""Serialized, atomic read-modify-write access to config.json.

config.json has several concurrent writers in this process (the plaintext
``update_node_config`` MQTT handler, the K2 ``node_config`` dispatcher,
volume persist, MQTT-credential persist), all running on thread pools.
Unsynchronized read-modify-write between them loses updates — a stale
read spanning another writer's commit silently restores the old value,
which for the update-policy consent gates violates a security invariant
(a broker-reachable volume write could resurrect a revoked
``allow_updates``). Worse, plain ``open(path, "w")`` truncates before it
writes, so a concurrent reader can catch an empty file, fall back to
``{}``, and then REWRITE config.json missing the node's identity keys —
bricking the node until re-provisioning.

``update_config_file`` fixes both: one process-wide lock serializes all
read-modify-write cycles, and the write goes to a temp file in the same
directory followed by ``os.replace`` so readers only ever see a complete
old or complete new file.
"""
import json
import os
import tempfile
import threading
from typing import Any, Callable

# Process-wide: every config.json writer must hold this across its whole
# read-modify-write cycle. (All writers live in this process — install.sh
# only touches the file while the service is stopped.)
_CONFIG_FILE_LOCK = threading.Lock()


def config_path() -> str:
    return os.path.expandvars(os.path.expanduser(
        os.environ.get("CONFIG_PATH", "config.json")
    ))


def update_config_file(mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Atomically apply ``mutate`` to config.json under the process lock.

    ``mutate`` receives the freshly-read config dict and edits it in place.
    Returns the config as written. Raises OSError on write failure —
    callers keep their existing error handling.
    """
    path = config_path()
    with _CONFIG_FILE_LOCK:
        try:
            with open(path) as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            config = {}
        mutate(config)
        # Write-then-rename: readers never observe a truncated file.
        directory = os.path.dirname(os.path.abspath(path))
        fd, tmp_path = tempfile.mkstemp(prefix=".config.json.", dir=directory)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp_path, path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return config
