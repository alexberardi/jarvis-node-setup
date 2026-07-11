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

from urllib.parse import urlparse


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
