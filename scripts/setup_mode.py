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


def _resolve_mqtt_broker(config_service_url: str) -> tuple[str | None, int]:
    """Fetch MQTT broker host and port from config-service.

    Uses ?style=dockerized inside Docker so host.docker.internal is
    returned instead of localhost.

    Returns (host, port) or (None, 1884) if unavailable.
    """
    import os
    style_param = "?style=dockerized" if os.path.exists("/.dockerenv") else ""

    try:
        resp = httpx.get(
            f"{config_service_url.rstrip('/')}/services/jarvis-mqtt-broker{style_param}",
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            # Use the url field (respects ?style=dockerized) and parse host from it
            mqtt_url = data.get("url", "")
            if mqtt_url:
                parsed = urlparse(mqtt_url)
                host = parsed.hostname or data.get("host")
                port = parsed.port or data.get("port", 1884)
            else:
                host = data.get("host")
                port = data.get("port", 1884)
            if host:
                return host, int(port)
    except Exception:
        pass
    return None, 1884


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


@app.post("/setup/discover")
async def discover_config_service():
    """Scan the local network for jarvis-config-service on port 7700."""
    import socket
    import asyncio

    import asyncio

    async def probe(host: str) -> str | None:
        url = f"http://{host}:7700/info"
        try:
            async with httpx.AsyncClient(timeout=1.5) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("service") == "jarvis-config-service":
                        return f"http://{host}:7700"
        except (httpx.RequestError, Exception):
            pass
        return None

    # Try Docker-native hosts first
    for docker_host in ["host.docker.internal", "jarvis-config-service"]:
        result = await probe(docker_host)
        if result:
            return {"ok": True, "config_url": result}

    # Get the host's real IP (not the container's Docker bridge IP)
    # Try the default gateway which is typically the Docker host
    try:
        import subprocess
        gw = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=2
        )
        # "default via 172.18.0.1 dev eth0" -> extract gateway IP
        gateway_ip = ""
        for part in gw.stdout.split():
            if part.count(".") == 3:
                gateway_ip = part
                break
        if gateway_ip:
            result = await probe(gateway_ip)
            if result:
                return {"ok": True, "config_url": result}
    except Exception:
        pass

    # Fall back to subnet scan using the host's real network
    # Resolve host.docker.internal to get the host IP for subnet scanning
    try:
        host_ip = socket.gethostbyname("host.docker.internal")
    except socket.gaierror:
        host_ip = None

    if not host_ip:
        return {"ok": False, "error": "Could not determine host network. Enter the URL manually."}

    subnet = ".".join(host_ip.split(".")[:3])
    priority_hosts = [1, 2, 10, 50, 100, 103, 150, 200]

    # Probe priority hosts first
    for h in priority_hosts:
        result = await probe(f"{subnet}.{h}")
        if result:
            return {"ok": True, "config_url": result}

    # Scan remaining in batches of 20
    priority_set = set(priority_hosts)
    remaining = [i for i in range(1, 255) if i not in priority_set]

    for i in range(0, len(remaining), 20):
        batch = remaining[i:i + 20]
        tasks = [probe(f"{subnet}.{h}") for h in batch]
        results = await asyncio.gather(*tasks)
        for r in results:
            if r:
                return {"ok": True, "config_url": r}

    return {"ok": False, "error": "No Jarvis server found on your network"}


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

    # Fetch service list from config service.
    # Use ?style=dockerized when running inside Docker so URLs use
    # host.docker.internal instead of localhost.
    import os
    style_param = "?style=dockerized" if os.path.exists("/.dockerenv") else ""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{config_url}/services{style_param}")
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

    # Resolve MQTT broker from config-service, fall back to CC hostname
    mqtt_host, mqtt_port = _resolve_mqtt_broker(config_url)
    if mqtt_host:
        config["mqtt_broker"] = mqtt_host
        config["mqtt_port"] = mqtt_port
    else:
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
# Rooms
# ------------------------------------------------------------------


@app.get("/setup/households/{household_id}/rooms")
async def list_rooms(household_id: str):
    """List rooms for a household."""
    cc = _cc_url()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{cc}/api/v0/households/{household_id}/rooms",
                headers=_auth_headers(),
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))

    if resp.status_code != 200:
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
