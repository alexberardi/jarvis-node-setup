"""Smoke tests against the real Jarvis stack running in
`docker-compose.ci.yaml`'s `core` profile (Postgres + auth + config-service +
the service-under-test).

Lives at `tests/` (not `tests/integration/`) for the same reason as
`test_loop_smoke.py`: the `tests/integration/` subtree's conftest imports
the production codebase, which depends on `jarvis_command_sdk`. Putting
this here keeps the smoke suite SDK-free.

Skipped when `CC_URL` is unset — the v1 fakes-only loop and the v2.1+ full
compose loop coexist, and only the latter sets `CC_URL`. Local runs that
don't bring up the stack still pass these as "skipped" rather than failing.

URLs default to where docker-compose.ci.yaml maps each service's port,
overridable via env so the same test can run against any compose layout.
"""

from __future__ import annotations

import os

import httpx
import pytest

CC_URL = os.environ.get("CC_URL")
AUTH_URL = os.environ.get("AUTH_URL", "http://localhost:7701")
CONFIG_URL = os.environ.get("CONFIG_URL", "http://localhost:7700")
SKIP_REASON = "CC_URL unset — skipping real-stack smoke tests (v1 fakes-only mode)"


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-101")
def test_cc_health_endpoint_responds_200():
    response = httpx.get(f"{CC_URL}/health", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    assert body.get("status") == "healthy", (
        f"expected status=healthy, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-102")
def test_cc_root_responds():
    """CC's root path should at minimum return a response (not 5xx).

    Whatever shape — JSON, HTML, 404 with body — confirms uvicorn is
    serving and the app didn't crash on startup.
    """
    response = httpx.get(f"{CC_URL}/", timeout=10.0)
    assert response.status_code < 500, (
        f"expected non-5xx, got {response.status_code} body={response.text[:200]}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-103")
def test_config_service_health_responds():
    """jarvis-config-service /health responds.

    Proves the ghcr.io :dev image pulled, alembic migrations ran, and the
    service bound to its port. CC's _setup_service_config() targets this
    service; if it's not up, CC's service-discovery path silently falls
    back to legacy env vars.
    """
    response = httpx.get(f"{CONFIG_URL}/health", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    assert body.get("status") == "ok", (
        f"expected status=ok, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-104")
def test_auth_service_health_responds():
    """jarvis-auth /health responds.

    Proves the ghcr.io :dev image pulled, the auth schema migrated (auth's
    Dockerfile CMD chains alembic), and the service is serving. CC's
    node-auth and app-auth paths all depend on this.
    """
    response = httpx.get(f"{AUTH_URL}/health", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    assert body.get("status") == "ok", (
        f"expected status=ok, got body={body}"
    )
