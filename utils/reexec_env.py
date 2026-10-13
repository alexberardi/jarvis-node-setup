"""The environment to re-exec scripts.main with after provisioning.

scripts.main seeds JARVIS_CONFIG_URL (and, from it, JARVIS_CONFIG_URL_STYLE)
from config.json only when they are unset. A factory reset keeps
``jarvis_config_service_url``, so the provisioning-mode process seeds the
previous server's URL. Provisioning then writes the new server's URL to
config.json and re-execs; a plain ``os.execv`` inherits the seeded variables,
so the new process skips config.json and keeps resolving every service from
the old server (jarvis-dev, 2026-10-08: 401s, no MQTT credentials, broker
rc=5, no tools reported).

Re-exec with the environment the process started with instead: the new
process then seeds from the freshly written config.json exactly like a cold
start, while anything systemd or the shell really set (a deliberate
JARVIS_CONFIG_URL override included) is kept.
"""

import os
from typing import Optional

_startup_env: Optional[dict[str, str]] = None


def snapshot() -> dict[str, str]:
    """A copy of the current environment."""
    return dict(os.environ)


def remember_startup_env() -> None:
    """Record the environment before main changes it. Call first thing."""
    global _startup_env
    _startup_env = snapshot()


def env_for_reexec() -> dict[str, str]:
    """The startup environment, or the current one if none was recorded."""
    if _startup_env is None:
        return snapshot()
    return dict(_startup_env)
