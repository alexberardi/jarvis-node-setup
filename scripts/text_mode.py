"""Text-only node entry point — headless REST API for Docker/dev use.

Reuses the same initialization as main.py (DB migrations, service discovery,
timer service, agent scheduler, MQTT listener) but skips all audio I/O,
wake word detection, and Bluetooth.

Exposes:
  POST /api/v1/command   — send text, get response from CC
  GET  /health           — health check
  GET  /api/v1/info      — node info
"""

import os
import threading

# Set config service URL from config.json before any library imports
if not os.environ.get("JARVIS_CONFIG_URL"):
    try:
        import json
        _config_path = os.environ.get("CONFIG_PATH", "config.json")
        with open(_config_path) as _f:
            _url = json.load(_f).get("jarvis_config_service_url")
        if _url:
            os.environ["JARVIS_CONFIG_URL"] = _url
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        pass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import uvicorn

from jarvis_log_client import init as init_logging, JarvisLogger

from services.agent_scheduler_service import initialize_agent_scheduler
from services.timer_service import initialize_timer_service
from utils.config_service import Config
from utils.service_discovery import init as init_service_discovery

# Initialize logging
init_logging(
    app_id=os.getenv("JARVIS_APP_ID", "jarvis-node"),
    app_key=os.getenv("JARVIS_APP_KEY", ""),
)
logger = JarvisLogger(service="jarvis-node-text")


# ── FastAPI app ──────────────────────────────────────────────────────────

app = FastAPI(title="Jarvis Node (Text Mode)", version="1.0.0")


class CommandRequest(BaseModel):
    text: str


class CommandResponseModel(BaseModel):
    success: bool
    message: str
    data: dict | None = None


@app.get("/health")
def health() -> dict:
    return {
        "status": "healthy",
        "service": "jarvis-node",
        "mode": "text",
        "node_id": Config.get_str("node_id", "unknown"),
    }


_mqtt_thread_ref: threading.Thread | None = None

@app.get("/debug/mqtt")
def debug_mqtt() -> dict:
    from scripts.mqtt_tts_listener import _mqtt_client
    return {
        "thread_alive": _mqtt_thread_ref.is_alive() if _mqtt_thread_ref else False,
        "client_connected": _mqtt_client is not None and _mqtt_client.is_connected() if _mqtt_client else False,
    }

@app.get("/api/v1/info")
def node_info() -> dict:
    return {
        "node_id": Config.get_str("node_id", "unknown"),
        "room": Config.get_str("room", "unknown"),
        "mode": "text",
        "mqtt_enabled": Config.get_bool("mqtt_enabled", True) is not False,
    }


@app.post("/api/v1/command", response_model=CommandResponseModel)
def process_command(body: CommandRequest) -> CommandResponseModel:
    """Send a text command through the full CC pipeline."""
    if not body.text.strip():
        raise HTTPException(status_code=400, detail="Empty command text")

    try:
        from utils.command_execution_service import CommandExecutionService
        service = CommandExecutionService()
        result = service.process_voice_command(body.text.strip())

        return CommandResponseModel(
            success=True,
            message=result.get("assistant_message", "") if isinstance(result, dict) else str(result),
            data=result if isinstance(result, dict) else None,
        )
    except Exception as e:
        logger.error("Command processing failed", error=str(e))
        raise HTTPException(status_code=500, detail=f"Command processing failed: {e}")


# ── Startup (same init sequence as main.py, minus audio) ────────────────

@app.on_event("startup")
async def startup() -> None:
    """Initialize node services on startup."""
    import sys
    print("[startup] begin", flush=True)

    # DB migrations
    try:
        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command

        alembic_cfg = AlembicConfig(
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini")
        )
        alembic_command.upgrade(alembic_cfg, "head")
        logger.info("Database migrations complete")
    except Exception as e:
        logger.warning("Database migration failed (non-fatal)", error=str(e))

    print("[startup] migrations done", flush=True)

    # Service discovery
    if init_service_discovery():
        logger.info("Service discovery initialized")
    else:
        logger.info("Using JSON config for service URLs")

    print("[startup] service discovery done", flush=True)

    # Timer service and agent scheduler need pysqlcipher3 with a valid K1
    # encryption key. In Docker/text mode the encrypted DB is typically
    # unavailable, so skip these to avoid retry loops that leak FDs.
    skip_encrypted_db = os.getenv("JARVIS_SKIP_ENCRYPTED_DB", "").lower() in ("true", "1", "yes")
    if not skip_encrypted_db:
        try:
            timer_service = initialize_timer_service()
            restored = timer_service.restore_timers()
            if restored > 0:
                logger.info("Restored timers", count=restored)
        except Exception as e:
            logger.warning("Timer service unavailable", error=str(e))

        try:
            from services.alert_queue_service import get_alert_queue_service
            alert_queue = get_alert_queue_service()
            agent_scheduler = initialize_agent_scheduler()
            agent_scheduler.set_alert_queue(alert_queue)
            logger.info("Agent scheduler initialized")
        except Exception as e:
            logger.warning("Agent scheduler init failed (non-fatal)", error=str(e))

    print("[startup] encrypted db section done", flush=True)

    # MQTT listener (for package installs, TTS, etc.)
    mqtt_enabled: bool = Config.get_bool("mqtt_enabled", True) is not False
    if mqtt_enabled:
        try:
            from scripts.mqtt_tts_listener import start_mqtt_listener
            from utils.music_assistant_service import DummyMusicAssistantService
            ma_service = DummyMusicAssistantService()
            global _mqtt_thread_ref
            mqtt_thread = threading.Thread(
                target=start_mqtt_listener, args=(ma_service,), daemon=True
            )
            mqtt_thread.start()
            _mqtt_thread_ref = mqtt_thread
            logger.info("MQTT listener started")
        except Exception as e:
            logger.warning("MQTT listener failed (non-fatal)", error=str(e))

    print("[startup] MQTT section done", flush=True)

    # Skip LLM warmup in text mode — it's synchronous and blocks startup
    # if CC or LLM proxy is slow/down. Commands warm up on first use instead.
    print("[startup] complete", flush=True)


def main() -> None:
    port = int(os.getenv("JARVIS_NODE_PORT", "7771"))
    logger.info("Starting text-mode node", port=port)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
