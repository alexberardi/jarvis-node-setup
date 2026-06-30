"""device_state_handler surfaces a protocol's ``needs_pairing`` signal as a
``pairing`` ui_hint BEFORE domain normalization (which builds a fresh dict and
would drop the flag). This lets the mobile app show a Pair affordance for any
domain — e.g. an unpaired HomeKit thermostat whose ClimateControl panel can't
otherwise trigger pairing.
"""

import asyncio

import pytest

state_handler = pytest.importorskip("services.device_state_handler")


def test_needs_pairing_short_circuits_to_pairing_ui_hint(monkeypatch):
    captured: dict = {}

    async def fake_query_direct(details):
        return {"needs_pairing": True, "state": "unpaired"}

    monkeypatch.setattr(state_handler, "_query_direct_device", fake_query_direct)
    monkeypatch.setattr(
        state_handler, "_upload_result",
        lambda request_id, data: captured.update(data),
    )

    asyncio.run(state_handler._async_query_and_upload(
        "req-1", {"entity_id": "bedroom", "domain": "climate", "source": "direct"}
    ))

    assert captured["ui_hints"]["control_type"] == "pairing"
    assert captured["state"] == {"needs_pairing": True}
    assert captured["domain"] == "climate"


def test_normal_state_still_normalizes(monkeypatch):
    captured: dict = {}

    async def fake_query_direct(details):
        return {"current_temperature": 70, "state": "heat", "mode": "heat"}

    monkeypatch.setattr(state_handler, "_query_direct_device", fake_query_direct)
    monkeypatch.setattr(
        state_handler, "_upload_result",
        lambda request_id, data: captured.update(data),
    )

    asyncio.run(state_handler._async_query_and_upload(
        "req-2", {"entity_id": "bedroom", "domain": "climate", "source": "direct"}
    ))

    # The climate normalizer ran (thermostat hint), not the pairing short-circuit.
    assert captured["ui_hints"]["control_type"] == "thermostat"
    assert captured["state"].get("mode") == "heat"
