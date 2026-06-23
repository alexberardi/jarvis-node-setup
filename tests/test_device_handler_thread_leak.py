"""Regression test: device MQTT handlers must not leak executor threads.

The device handlers run protocol code that wraps sync API calls in
asyncio.to_thread, on a per-request event loop. The old
new_event_loop()/loop.close() pattern never shut down the loop's default
ThreadPoolExecutor, so every device request leaked worker threads parked in
futex_wait. They accumulated over ~1.5 days of uptime until the node hit
"RuntimeError: can't start new thread" and wake broke (2026-06-23 incident).
asyncio.run() shuts the executor down — these tests fail on the old pattern and
pass on the fix.
"""

import asyncio
import threading
import time

import pytest

state_handler = pytest.importorskip("services.device_state_handler")
scan_handler = pytest.importorskip("services.device_scan_handler")
list_handler = pytest.importorskip("services.device_list_handler")


def _assert_no_leak(call) -> None:
    time.sleep(0.1)  # let any pre-existing transient threads settle
    baseline = threading.active_count()
    for _ in range(6):
        call()
    time.sleep(0.3)  # allow shutdown_default_executor to join workers
    leaked = threading.active_count() - baseline
    assert leaked <= 0, f"leaked {leaked} thread(s) — loop default executor not shut down"


def test_state_handler_no_thread_leak(monkeypatch):
    async def fake(request_id, details):
        for _ in range(4):
            await asyncio.to_thread(time.sleep, 0)  # populates the default executor

    monkeypatch.setattr(state_handler, "_async_query_and_upload", fake)
    _assert_no_leak(
        lambda: state_handler.run_state_query_and_upload("req-abcd1234", {"entity_id": "x"})
    )


def test_scan_handler_no_thread_leak(monkeypatch):
    async def fake(request_id):
        for _ in range(4):
            await asyncio.to_thread(time.sleep, 0)

    monkeypatch.setattr(scan_handler, "_async_scan_and_upload", fake)
    _assert_no_leak(lambda: scan_handler.run_scan_and_upload("req-abcd1234"))


def test_list_handler_no_thread_leak(monkeypatch):
    async def fake(request_id, manager_name):
        for _ in range(4):
            await asyncio.to_thread(time.sleep, 0)

    monkeypatch.setattr(list_handler, "_async_collect_and_upload", fake)
    _assert_no_leak(
        lambda: list_handler.run_collect_and_upload("req-abcd1234", "some_manager")
    )
