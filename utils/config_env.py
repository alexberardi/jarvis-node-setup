"""Config-service bootstrap shared by both node entrypoints.

A node resolves every service URL through config-service, and the *style* of
that resolution depends on this node's vantage relative to the stack. That
choice must be identical no matter how the node was started:

  * Pi / bare-metal → systemd runs ``python -m scripts.main`` directly.
  * Container       → ``scripts.entrypoint`` runs first, then hands off to main.

The Pi path never touches ``entrypoint.py``, so the style logic cannot live
only there (that gap shipped a Pi that forced ``remote`` and couldn't reach
container-name HTTP rows). Keep it here and call it from both.
"""

import os
from collections.abc import MutableMapping
from urllib.parse import urlparse

# ``remote`` is the retired predecessor of ``external``: it only rewrites
# localhost-registered rows, leaving container-name HTTP rows (command-center)
# unreachable off-box. Any node still carrying it — from a stale systemd unit
# that a code-only update didn't regenerate — should be re-pointed at the style
# its vantage actually implies. Treated as "unset" so we recompute over it.
_RETIRED_STYLES = ("", "remote")


def config_url_style_for_host(host: str) -> str | None:
    """The ``JARVIS_CONFIG_URL_STYLE`` a config-service host implies.

    * ``host.docker.internal`` → this node is a Docker peer of the stack →
      ``dockerized`` (localhost rows rewrite to ``host.docker.internal``).
    * a real IP / hostname → this node is off-box (a LAN Pi, or a node on
      another host) → ``external``: uses each service's published coordinates,
      rewriting BOTH the localhost-registered broker AND container-name HTTP
      rows to the server host. (``remote`` only fixes the localhost rows, so it
      leaves container-name services unreachable off-box — that was the bug.)
    * ``localhost`` / ``127.0.0.1`` / empty → same box → ``None`` (config-service
      returns on-box URLs already; no rewrite wanted).
    """
    if host == "host.docker.internal":
        return "dockerized"
    if host and host not in ("localhost", "127.0.0.1"):
        return "external"
    return None


def config_url_style_for_url(config_url: str) -> str | None:
    """``config_url_style_for_host`` applied to a full config-service URL."""
    return config_url_style_for_host(urlparse(config_url).hostname or "")


def apply_config_url_style(
    config_url: str, environ: MutableMapping[str, str] | None = None
) -> str | None:
    """Set ``JARVIS_CONFIG_URL_STYLE`` in the environment from the config-service
    host's vantage, and return the effective style.

    An explicit ``dockerized``/``external`` already in the env wins. But an unset
    style — or the retired ``remote`` left behind by a stale systemd unit — is
    (re)computed from ``config_url``, so a node self-heals on a code update
    without needing its unit regenerated.
    """
    if environ is None:
        environ = os.environ
    computed = config_url_style_for_url(config_url)
    current = environ.get("JARVIS_CONFIG_URL_STYLE", "")
    if computed and current in _RETIRED_STYLES:
        environ["JARVIS_CONFIG_URL_STYLE"] = computed
        return computed
    return current or None
