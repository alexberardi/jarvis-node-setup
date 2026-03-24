#!/usr/bin/env python3
"""Setup mode — self-service web UI for node registration.

Serves a lightweight setup wizard on the node's HTTP port. The user
logs into jarvis-auth, selects a household, and the node registers
itself with the command center automatically.

All auth calls are proxied through this server to avoid CORS issues.
"""

import json
import os
import sys
import threading
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.json")
PORT = int(os.environ.get("JARVIS_NODE_PORT", "7771"))

app = FastAPI(title="Jarvis Node Setup")

# In-memory session state (single user setup flow)
_session: dict = {
    "jwt": None,
    "refresh_token": None,
    "auth_url": None,
    "cc_url": None,
}
_setup_lock = threading.Lock()
_setup_complete = False


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_config(config: dict) -> None:
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def _auth_url() -> str:
    if _session["auth_url"]:
        return _session["auth_url"].rstrip("/")
    config = _load_config()
    url = config.get("jarvis_auth_base_url", os.environ.get("JARVIS_AUTH_BASE_URL", ""))
    return url.rstrip("/") if url else ""


def _cc_url() -> str:
    if _session["cc_url"]:
        return _session["cc_url"].rstrip("/")
    config = _load_config()
    url = config.get("jarvis_command_center_api_url", os.environ.get("JARVIS_CC_URL", ""))
    return url.rstrip("/") if url else ""


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "setup_required", "mode": "setup"}


# ------------------------------------------------------------------
# Setup UI
# ------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    ui_path = Path(__file__).parent / "setup_ui" / "index.html"
    if ui_path.exists():
        return FileResponse(ui_path, media_type="text/html")
    return HTMLResponse("<h1>Setup UI not found</h1>", status_code=500)


# ------------------------------------------------------------------
# Service connectivity
# ------------------------------------------------------------------


@app.get("/setup/config")
def get_config():
    """Return known service URLs for pre-filling the UI."""
    config = _load_config()
    return {
        "config_url": config.get("jarvis_config_service_url", os.environ.get("JARVIS_CONFIG_URL", "")),
    }


@app.post("/setup/connect")
async def connect_services(request: Request):
    """Resolve auth + CC URLs from the config service, verify connectivity."""
    body = await request.json()
    config_url = body.get("config_url", "").rstrip("/")

    if not config_url:
        raise HTTPException(status_code=400, detail="config_url is required")

    # Fetch service list from config service
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config_url}/services")
            if resp.status_code != 200:
                return JSONResponse(
                    {"ok": False, "errors": {"config": f"Config service returned {resp.status_code}"}},
                    status_code=502,
                )
            services_data = resp.json()
    except httpx.RequestError as e:
        return JSONResponse(
            {"ok": False, "errors": {"config": str(e)}},
            status_code=502,
        )

    # Resolve auth and CC URLs from service list
    auth_url = ""
    cc_url = ""
    for svc in services_data.get("services", []):
        url = svc.get("url") or f"{svc['scheme']}://{svc['host']}:{svc['port']}"
        if svc["name"] == "jarvis-auth":
            auth_url = url.rstrip("/")
        elif svc["name"] == "jarvis-command-center":
            cc_url = url.rstrip("/")

    if not auth_url or not cc_url:
        return JSONResponse(
            {"ok": False, "errors": {"config": "Could not resolve auth and command center URLs from config service"}},
            status_code=502,
        )

    errors = {}
    auth_data: dict = {}

    # Verify auth service
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{auth_url}/auth/setup-status")
            if resp.status_code != 200:
                errors["auth"] = f"Auth returned {resp.status_code}"
            else:
                auth_data = resp.json()
    except httpx.RequestError as e:
        errors["auth"] = str(e)

    # Verify command center
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{cc_url}/health")
            if resp.status_code != 200:
                errors["cc"] = f"CC returned {resp.status_code}"
    except httpx.RequestError as e:
        errors["cc"] = str(e)

    if errors:
        return JSONResponse({"ok": False, "errors": errors}, status_code=502)

    # Save all URLs to session and config
    _session["auth_url"] = auth_url
    _session["cc_url"] = cc_url

    config = _load_config()
    config["jarvis_config_service_url"] = config_url
    config["jarvis_auth_base_url"] = auth_url
    config["jarvis_command_center_api_url"] = cc_url
    # Derive MQTT broker from CC host
    cc_host = urlparse(cc_url).hostname
    if cc_host:
        config["mqtt_broker"] = cc_host
    _save_config(config)

    return {
        "ok": True,
        "needs_setup": auth_data.get("needs_setup", False),
    }


# ------------------------------------------------------------------
# Auth proxy
# ------------------------------------------------------------------


@app.post("/setup/login")
async def login(request: Request):
    """Proxy login to jarvis-auth, store JWT in session."""
    body = await request.json()
    auth = _auth_url()
    if not auth:
        raise HTTPException(status_code=400, detail="Auth URL not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{auth}/auth/login", json=body)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if resp.status_code != 200:
        return JSONResponse(resp.json(), status_code=resp.status_code)

    data = resp.json()
    _session["jwt"] = data.get("access_token")
    _session["refresh_token"] = data.get("refresh_token")

    return {"ok": True, "user": data.get("user")}


@app.post("/setup/register")
async def register(request: Request):
    """Proxy registration to jarvis-auth, store JWT in session."""
    body = await request.json()
    auth = _auth_url()
    if not auth:
        raise HTTPException(status_code=400, detail="Auth URL not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{auth}/auth/register", json=body)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if resp.status_code not in (200, 201):
        return JSONResponse(resp.json(), status_code=resp.status_code)

    data = resp.json()
    _session["jwt"] = data.get("access_token")
    _session["refresh_token"] = data.get("refresh_token")

    return {"ok": True, "user": data.get("user"), "household_id": data.get("household_id")}


@app.post("/setup/initial-setup")
async def initial_setup(request: Request):
    """Proxy first-user setup to jarvis-auth."""
    body = await request.json()
    auth = _auth_url()
    if not auth:
        raise HTTPException(status_code=400, detail="Auth URL not configured")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{auth}/auth/setup", json=body)
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if resp.status_code not in (200, 201):
        return JSONResponse(resp.json(), status_code=resp.status_code)

    data = resp.json()
    _session["jwt"] = data.get("access_token")
    _session["refresh_token"] = data.get("refresh_token")

    return {"ok": True, "user": data.get("user"), "household_id": data.get("household_id")}


# ------------------------------------------------------------------
# Household management
# ------------------------------------------------------------------


def _auth_headers() -> dict:
    if not _session["jwt"]:
        raise HTTPException(status_code=401, detail="Not logged in")
    return {"Authorization": f"Bearer {_session['jwt']}"}


@app.get("/setup/households")
async def list_households():
    """List user's households."""
    auth = _auth_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{auth}/households", headers=_auth_headers())
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if resp.status_code != 200:
        return JSONResponse(resp.json(), status_code=resp.status_code)

    return resp.json()


@app.post("/setup/households")
async def create_household(request: Request):
    """Create a new household."""
    body = await request.json()
    auth = _auth_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{auth}/households", json=body, headers=_auth_headers())
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if resp.status_code not in (200, 201):
        return JSONResponse(resp.json(), status_code=resp.status_code)

    return resp.json()


# ------------------------------------------------------------------
# Node registration
# ------------------------------------------------------------------


@app.post("/setup/complete")
async def complete_setup(request: Request):
    """Register the node with the command center and save credentials."""
    global _setup_complete

    if _setup_complete:
        return JSONResponse({"ok": False, "error": "Setup already completed"}, status_code=409)

    with _setup_lock:
        if _setup_complete:
            return JSONResponse({"ok": False, "error": "Setup already completed"}, status_code=409)

        body = await request.json()
        household_id = body.get("household_id")
        room = body.get("room", "default")
        name = body.get("name")

        if not household_id:
            raise HTTPException(status_code=400, detail="household_id is required")

        jwt = _session.get("jwt")
        if not jwt:
            raise HTTPException(status_code=401, detail="Not logged in")

        cc = _cc_url()
        if not cc:
            raise HTTPException(status_code=400, detail="Command center URL not configured")

        # Step 1: Create provisioning token via CC (JWT auth)
        token_payload: dict = {"household_id": household_id}
        if room:
            token_payload["room"] = room
        if name:
            token_payload["name"] = name

        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{cc}/api/v0/provisioning/token",
                    json=token_payload,
                    headers={"Authorization": f"Bearer {jwt}"},
                )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"CC unreachable: {e}")

        if resp.status_code not in (200, 201):
            detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text
            raise HTTPException(status_code=resp.status_code, detail=f"Token creation failed: {detail}")

        token_data = resp.json()
        node_id = token_data["node_id"]
        prov_token = token_data["token"]

        # Step 2: Register node with token (no auth needed)
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    f"{cc}/api/v0/nodes/register",
                    json={"node_id": node_id, "provisioning_token": prov_token},
                )
        except httpx.RequestError as e:
            raise HTTPException(status_code=502, detail=f"Registration failed: {e}")

        if resp.status_code not in (200, 201):
            detail = resp.json().get("detail", resp.text) if resp.headers.get("content-type", "").startswith("application/json") else resp.text
            raise HTTPException(status_code=resp.status_code, detail=f"Node registration failed: {detail}")

        reg_data = resp.json()
        node_key = reg_data.get("node_key")

        if not node_key:
            raise HTTPException(status_code=500, detail="CC did not return node credentials")

        # Step 3: Save credentials to config
        config = _load_config()
        config["node_id"] = node_id
        config["api_key"] = node_key
        config["room"] = room
        if name:
            config["name"] = name
        _save_config(config)

        # Step 4: Mark as provisioned
        secret_dir = os.environ.get("JARVIS_SECRET_DIRECTORY", "/root/.jarvis")
        os.makedirs(secret_dir, exist_ok=True)
        provisioned_path = os.path.join(secret_dir, ".provisioned")
        Path(provisioned_path).touch(mode=0o600)

        _setup_complete = True

        # Schedule container exit so docker restarts into text mode
        def _delayed_exit():
            import time
            time.sleep(2)
            print("[setup] registration complete — restarting into normal mode", flush=True)
            os._exit(0)

        threading.Thread(target=_delayed_exit, daemon=True).start()

        return {
            "ok": True,
            "node_id": node_id,
            "room": room,
            "message": "Node registered. Restarting into normal mode...",
        }


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------


def main() -> None:
    print(f"[setup] starting setup server on port {PORT}", flush=True)
    print(f"[setup] open http://localhost:{PORT} in your browser to configure this node", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
