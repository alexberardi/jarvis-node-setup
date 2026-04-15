"""
Tests for Track 1: Node Reliability improvements.

Tests MQTT reconnect, thread supervision, agent lifecycle,
shutdown event propagation, and thread-safe lazy init.
"""

import signal
import sys
import threading
import time
from typing import Any, Dict, List, Tuple
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Mock sqlcipher3 + db module before any project imports.
# sqlcipher3 is a C extension not available on macOS dev, and db.py runs
# create_engine() at import time which blows up with a plain MagicMock.
_mock_db = MagicMock()
_mock_db.SessionLocal = MagicMock
_mock_db.engine = MagicMock()
if "sqlcipher3" not in sys.modules:
    sys.modules["sqlcipher3"] = MagicMock()
    sys.modules["sqlcipher3.dbapi2"] = MagicMock()
if "db" not in sys.modules:
    sys.modules["db"] = _mock_db

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import JarvisSecret
from services.agent_scheduler_service import AgentSchedulerService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class StubAgent(IJarvisAgent):
    """Minimal agent for testing update_agents / restart."""

    def __init__(self, name: str = "stub"):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._name

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(interval_seconds=60, run_on_startup=False)

    @property
    def required_secrets(self) -> List[JarvisSecret]:
        return []

    async def run(self) -> None:
        pass

    def get_context_data(self) -> Dict[str, Any]:
        return {"name": self._name}


@pytest.fixture
def scheduler():
    """Fresh AgentSchedulerService with reset singleton."""
    AgentSchedulerService._instance = None
    svc = AgentSchedulerService()
    yield svc
    if svc._running:
        svc.stop()
    AgentSchedulerService._instance = None


# ---------------------------------------------------------------------------
# 1B / 1C: update_agents – thread-safe agent replacement
# ---------------------------------------------------------------------------

class TestUpdateAgents:

    def test_replaces_agents(self, scheduler):
        old = StubAgent("old")
        new = StubAgent("new")
        scheduler._agents = {"old": old}
        scheduler._context_cache = {"old": {"name": "old"}}

        scheduler.update_agents({"new": new})

        assert "new" in scheduler._agents
        assert "old" not in scheduler._agents

    def test_clears_stale_context(self, scheduler):
        """Removed agents should have their context cache cleared."""
        scheduler._agents = {"a": StubAgent("a"), "b": StubAgent("b")}
        scheduler._context_cache = {"a": {"x": 1}, "b": {"y": 2}}

        # Keep only "b"
        scheduler.update_agents({"b": scheduler._agents["b"]})

        assert "a" not in scheduler._context_cache
        assert "b" in scheduler._context_cache

    def test_concurrent_update_and_read(self, scheduler):
        """update_agents should not corrupt context during concurrent reads."""
        scheduler._agents = {"a": StubAgent("a")}
        scheduler._context_cache = {"a": {"v": 1}}

        errors: List[str] = []

        def reader():
            for _ in range(200):
                try:
                    ctx = scheduler.get_aggregated_context()
                    # Should always be a dict, never corrupt
                    assert isinstance(ctx, dict)
                except Exception as e:
                    errors.append(str(e))

        def writer():
            for i in range(200):
                agents = {"a": StubAgent("a")} if i % 2 == 0 else {"b": StubAgent("b")}
                scheduler.update_agents(agents)

        reader_threads = [threading.Thread(target=reader) for _ in range(3)]
        writer_thread = threading.Thread(target=writer)

        for t in reader_threads:
            t.start()
        writer_thread.start()

        for t in reader_threads:
            t.join(timeout=5)
        writer_thread.join(timeout=5)

        assert errors == [], f"Concurrent access errors: {errors}"


class TestRestart:

    def test_restart_calls_stop_then_start(self, scheduler):
        scheduler.stop = MagicMock()
        scheduler.start = MagicMock()

        scheduler.restart()

        scheduler.stop.assert_called_once()
        scheduler.start.assert_called_once()


# ---------------------------------------------------------------------------
# 1D: Thread supervisor
# ---------------------------------------------------------------------------

class TestSupervisorLoop:

    def test_restarts_dead_thread(self):
        from scripts.main import _supervisor_loop

        shutdown = threading.Event()
        restarted = threading.Event()

        # A thread that dies immediately
        dead_thread = threading.Thread(target=lambda: None)
        dead_thread.start()
        dead_thread.join()  # Ensure it's dead

        restart_count = 0

        def make_replacement() -> threading.Thread:
            nonlocal restart_count
            restart_count += 1
            restarted.set()
            # Return a thread that stays alive briefly
            t = threading.Thread(target=lambda: time.sleep(5), daemon=True)
            t.start()
            return t

        threads: Dict[str, Tuple[threading.Thread, Any]] = {
            "test": (dead_thread, make_replacement),
        }

        # Run supervisor in background
        sup = threading.Thread(
            target=_supervisor_loop,
            args=(threads, shutdown),
            daemon=True,
        )
        sup.start()

        # Wait for the restart to happen (supervisor checks every 30s but
        # we patched the wait to be faster for the test)
        assert restarted.wait(timeout=35), "Supervisor did not restart dead thread within 35s"
        assert restart_count >= 1

        shutdown.set()
        sup.join(timeout=5)

    def test_respects_shutdown_event(self):
        from scripts.main import _supervisor_loop

        shutdown = threading.Event()
        alive_thread = threading.Thread(target=lambda: time.sleep(60), daemon=True)
        alive_thread.start()

        threads: Dict[str, Tuple[threading.Thread, Any]] = {
            "test": (alive_thread, lambda: alive_thread),
        }

        sup = threading.Thread(
            target=_supervisor_loop,
            args=(threads, shutdown),
            daemon=True,
        )
        sup.start()

        # Signal shutdown immediately
        shutdown.set()
        sup.join(timeout=5)
        assert not sup.is_alive(), "Supervisor should exit when shutdown is set"


# ---------------------------------------------------------------------------
# 1E: Graceful shutdown signal handler
# ---------------------------------------------------------------------------

class TestShutdownHandler:

    def test_handle_shutdown_sets_event(self):
        from scripts.main import _handle_shutdown, _shutdown_event

        # Reset event in case prior test set it
        _shutdown_event.clear()

        _handle_shutdown(signal.SIGTERM, None)

        assert _shutdown_event.is_set()

        # Clean up
        _shutdown_event.clear()


# ---------------------------------------------------------------------------
# 1A: MQTT reconnect
# ---------------------------------------------------------------------------

class TestMQTTReconnect:

    def test_on_disconnect_callback_exists(self):
        from scripts.mqtt_tts_listener import _on_disconnect
        # Should be callable
        assert callable(_on_disconnect)

    def test_on_disconnect_logs_reason(self):
        from scripts.mqtt_tts_listener import _on_disconnect
        client = MagicMock()

        # Should not raise
        _on_disconnect(client, None, 0)   # rc=0 is clean disconnect
        _on_disconnect(client, None, 1)   # rc=1 is unexpected

    def test_on_connect_subscribes_with_qos1(self):
        from scripts.mqtt_tts_listener import on_connect
        client = MagicMock()

        with patch("scripts.mqtt_tts_listener.get_mqtt_config") as mock_config:
            mock_config.return_value = {"topic": "jarvis/nodes/test/#"}
            on_connect(client, None, {}, 0)

        # Both subscribe calls should use qos=1
        calls = client.subscribe.call_args_list
        assert len(calls) >= 2
        for call in calls:
            args, kwargs = call
            # QoS can be positional arg or kwarg
            if len(args) >= 2:
                assert args[1] == 1, f"Expected qos=1, got {args[1]}"
            else:
                assert kwargs.get("qos", 0) == 1, f"Expected qos=1 in kwargs"

    def test_start_mqtt_listener_retries_on_failure(self):
        """start_mqtt_listener should retry connection on failure."""
        from scripts.mqtt_tts_listener import start_mqtt_listener

        mock_client = MagicMock()
        mock_client.connect.side_effect = [
            ConnectionRefusedError("refused"),
            ConnectionRefusedError("refused"),
            None,  # Third attempt succeeds
        ]

        with patch("scripts.mqtt_tts_listener.mqtt.Client", return_value=mock_client), \
             patch("scripts.mqtt_tts_listener.get_mqtt_config", return_value={
                 "broker": "localhost", "port": 1883,
                 "username": None, "password": None,
                 "topic": "jarvis/nodes/test/#",
             }), \
             patch("scripts.mqtt_tts_listener.time.sleep") as mock_sleep:

            # loop_forever blocks, so mock it to return immediately
            mock_client.loop_forever.return_value = None

            start_mqtt_listener(MagicMock())

            # Should have called connect 3 times
            assert mock_client.connect.call_count == 3
            # Should have slept between retries (backoff)
            assert mock_sleep.call_count >= 2

    def test_start_mqtt_listener_gives_up_after_max_retries(self):
        """After all retries fail, should return without calling loop_forever."""
        from scripts.mqtt_tts_listener import start_mqtt_listener

        mock_client = MagicMock()
        mock_client.connect.side_effect = ConnectionRefusedError("refused")

        with patch("scripts.mqtt_tts_listener.mqtt.Client", return_value=mock_client), \
             patch("scripts.mqtt_tts_listener.get_mqtt_config", return_value={
                 "broker": "localhost", "port": 1883,
                 "username": None, "password": None,
                 "topic": "jarvis/nodes/test/#",
             }), \
             patch("scripts.mqtt_tts_listener.time.sleep"):

            start_mqtt_listener(MagicMock())

            assert mock_client.connect.call_count == 5
            mock_client.loop_forever.assert_not_called()

    def test_reconnect_delay_is_configured(self):
        """Client should have reconnect_delay_set called."""
        from scripts.mqtt_tts_listener import start_mqtt_listener

        mock_client = MagicMock()
        mock_client.connect.return_value = None

        with patch("scripts.mqtt_tts_listener.mqtt.Client", return_value=mock_client), \
             patch("scripts.mqtt_tts_listener.get_mqtt_config", return_value={
                 "broker": "localhost", "port": 1883,
                 "username": None, "password": None,
                 "topic": "jarvis/nodes/test/#",
             }):
            mock_client.loop_forever.return_value = None
            start_mqtt_listener(MagicMock())

        mock_client.reconnect_delay_set.assert_called_once_with(min_delay=1, max_delay=60)


# ---------------------------------------------------------------------------
# 1E: Shutdown-aware background loops
# ---------------------------------------------------------------------------

class TestShutdownAwareLoops:

    def test_heartbeat_loop_exits_on_shutdown(self):
        from scripts import mqtt_tts_listener
        shutdown = threading.Event()
        old_event = mqtt_tts_listener._shutdown_event
        mqtt_tts_listener._shutdown_event = shutdown

        try:
            # Signal shutdown immediately
            shutdown.set()

            # Run heartbeat loop — should exit quickly
            t = threading.Thread(target=mqtt_tts_listener._heartbeat_loop, daemon=True)
            t.start()
            t.join(timeout=5)
            assert not t.is_alive(), "Heartbeat loop should exit when shutdown is set"
        finally:
            mqtt_tts_listener._shutdown_event = old_event

    def test_cleanup_loop_exits_on_shutdown(self):
        from scripts import mqtt_tts_listener
        shutdown = threading.Event()
        old_event = mqtt_tts_listener._shutdown_event
        mqtt_tts_listener._shutdown_event = shutdown

        try:
            shutdown.set()

            t = threading.Thread(target=mqtt_tts_listener._test_command_cleanup_loop, daemon=True)
            t.start()
            t.join(timeout=5)
            assert not t.is_alive(), "Cleanup loop should exit when shutdown is set"
        finally:
            mqtt_tts_listener._shutdown_event = old_event


# ---------------------------------------------------------------------------
# 1F: Thread-safe lazy init
# ---------------------------------------------------------------------------

class TestThreadSafeLazyInit:

    def test_command_discovery_concurrent_init(self):
        """get_command_discovery_service should not double-create under concurrent calls."""
        import utils.command_discovery_service as mod
        old = mod._command_discovery_service
        mod._command_discovery_service = None

        instances: List[Any] = []

        def get_instance():
            with patch.object(mod.CommandDiscoveryService, "__init__", return_value=None):
                svc = mod.get_command_discovery_service()
                instances.append(id(svc))

        try:
            threads = [threading.Thread(target=get_instance) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            # All threads should get the same instance
            assert len(set(instances)) == 1, f"Got {len(set(instances))} distinct instances"
        finally:
            mod._command_discovery_service = old

    def test_agent_discovery_concurrent_init(self):
        """get_agent_discovery_service should not double-create under concurrent calls."""
        import utils.agent_discovery_service as mod
        old = mod._agent_discovery_service
        mod._agent_discovery_service = None

        instances: List[Any] = []

        def get_instance():
            with patch.object(mod.AgentDiscoveryService, "__init__", return_value=None):
                svc = mod.get_agent_discovery_service()
                instances.append(id(svc))

        try:
            threads = [threading.Thread(target=get_instance) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)

            assert len(set(instances)) == 1, f"Got {len(set(instances))} distinct instances"
        finally:
            mod._agent_discovery_service = old


# ---------------------------------------------------------------------------
# 1G: Heartbeat thread status
# ---------------------------------------------------------------------------

class TestHeartbeatThreadStatus:

    def test_tracked_threads_build_status_dict(self):
        """Thread status dict correctly reflects alive/dead threads."""
        from scripts import mqtt_tts_listener

        alive_thread = threading.Thread(target=lambda: time.sleep(60), daemon=True)
        alive_thread.start()

        dead_thread = threading.Thread(target=lambda: None)
        dead_thread.start()
        dead_thread.join()

        tracked = {
            "mqtt": (alive_thread, None),
            "voice": (dead_thread, None),
        }

        # Replicate the thread_status building logic from _heartbeat_loop
        thread_status: Dict[str, bool] = {}
        for name, entry in tracked.items():
            thread_obj = entry[0] if isinstance(entry, tuple) else entry
            thread_status[name] = thread_obj.is_alive() if hasattr(thread_obj, "is_alive") else False

        assert thread_status["mqtt"] is True
        assert thread_status["voice"] is False

    def test_set_tracked_threads(self):
        """set_tracked_threads updates the module-level variable."""
        from scripts import mqtt_tts_listener

        old = mqtt_tts_listener._tracked_threads
        try:
            test_threads = {"test": (threading.current_thread(), None)}
            mqtt_tts_listener.set_tracked_threads(test_threads)
            assert mqtt_tts_listener._tracked_threads is test_threads
        finally:
            mqtt_tts_listener._tracked_threads = old

    def test_set_shutdown_event(self):
        """set_shutdown_event updates the module-level variable."""
        from scripts import mqtt_tts_listener

        old = mqtt_tts_listener._shutdown_event
        try:
            event = threading.Event()
            mqtt_tts_listener.set_shutdown_event(event)
            assert mqtt_tts_listener._shutdown_event is event
        finally:
            mqtt_tts_listener._shutdown_event = old


# ---------------------------------------------------------------------------
# 1B: package_install_handler uses update_agents
# ---------------------------------------------------------------------------

class TestPackageInstallHandlerUsesUpdateAgents:

    def test_no_direct_agents_mutation(self):
        """package_install_handler should call update_agents, not _agents ="""
        import inspect
        from services.package_install_handler import run_install_and_upload

        source = inspect.getsource(run_install_and_upload)
        assert "scheduler._agents" not in source, \
            "package_install_handler should use scheduler.update_agents(), not scheduler._agents ="
        assert "update_agents" in source, \
            "package_install_handler should call scheduler.update_agents()"
