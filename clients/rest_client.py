import requests
from typing import Any, Dict, List, Optional

from jarvis_log_client import JarvisLogger

from utils.config_service import Config

logger = JarvisLogger(service="jarvis-node")

# Type alias for memory injection items
MemoryItem = Dict[str, Any]


class RestClient:
    """HTTP client for Jarvis service communication.

    Uses a persistent requests.Session to reuse TCP connections via
    HTTP keep-alive, preventing file descriptor exhaustion in long-running
    container deployments.
    """

    _session: Optional[requests.Session] = None

    @classmethod
    def _get_session(cls) -> requests.Session:
        """Get or create the shared HTTP session."""
        if cls._session is None:
            session = requests.Session()
            # Retry on CONNECTION errors with a fresh connection. After a long
            # idle, the pooled keep-alive socket to command-center (via the
            # Cloudflare tunnel) goes stale; the first request then fails with
            # ConnectionRefused/aborted, which silently swallowed the first
            # voice command after idle ("had to repeat myself"). connect=2 re-
            # establishes a fresh connection. read=0/status=0: do NOT retry
            # read-timeouts (that's the slow-CC path — must not be amplified).
            # allowed_methods=None retries all verbs incl POST (safe: a stale
            # connection fails before the request body is sent).
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry

            retry = Retry(
                total=2, connect=2, read=0, status=0, redirect=0,
                backoff_factor=0.2, allowed_methods=None, raise_on_status=False,
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            cls._session = session
        return cls._session

    @staticmethod
    def _build_auth_header() -> Dict[str, str]:
        """Build X-API-Key header in format 'node_id:node_key'."""
        node_id = Config.get_str("node_id", "") or ""
        api_key = Config.get_str("api_key", "") or ""
        if node_id and api_key:
            return {"X-API-Key": f"{node_id}:{api_key}"}
        return {}

    @staticmethod
    def post(
        url: str,
        data: Optional[Dict[str, Any]] = None,
        files: Optional[Dict[str, Any]] = None,
        timeout: int = 10
    ) -> Optional[Dict[str, Any]]:
        session = RestClient._get_session()
        headers: Dict[str, str] = RestClient._build_auth_header()

        request_args: Dict[str, Any] = {"headers": headers, "timeout": timeout}

        if data is not None:
            request_args["json"] = data

        if files is not None:
            request_args["files"] = files

        try:
            response = session.post(url, **request_args)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error("REST POST request failed", url=url, error=str(e))
            return None

    @staticmethod
    def get(
        url: str,
        timeout: int = 10
    ) -> Optional[Dict[str, Any]]:
        session = RestClient._get_session()
        headers: Dict[str, str] = RestClient._build_auth_header()

        request_args: Dict[str, Any] = {"headers": headers, "timeout": timeout}

        try:
            response = session.get(url, **request_args)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error("REST GET request failed", url=url, error=str(e))
            return None

    @staticmethod
    def put(
        url: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: int = 10
    ) -> Optional[Dict[str, Any]]:
        session = RestClient._get_session()
        headers: Dict[str, str] = RestClient._build_auth_header()

        request_args: Dict[str, Any] = {"headers": headers, "timeout": timeout}

        if data is not None:
            request_args["json"] = data

        try:
            response = session.put(url, **request_args)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error("REST PUT request failed", url=url, error=str(e))
            return None

    @staticmethod
    def post_stream(
        url: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: int = 30,
        chunk_size: int = 4096,
    ) -> Optional[requests.Response]:
        """POST request that returns a streaming response for iteration.

        Args:
            url: The URL to POST to
            data: JSON body to send
            timeout: Request timeout in seconds
            chunk_size: Size of chunks for iter_content

        Returns:
            Response object with stream=True, or None on error.
            Caller should iterate via response.iter_content(chunk_size).
        """
        session = RestClient._get_session()
        headers: Dict[str, str] = RestClient._build_auth_header()
        headers["Content-Type"] = "application/json"

        try:
            response = session.post(
                url, json=data, headers=headers, timeout=timeout, stream=True
            )
            # Allow 2xx range (200 audio, 202 JSON) — only raise on 3xx+
            if response.status_code >= 300:
                response.raise_for_status()
            return response
        except requests.RequestException as e:
            logger.error("REST POST stream request failed", url=url, error=str(e))
            return None

    @staticmethod
    def post_binary(
        url: str,
        data: Optional[Dict[str, Any]] = None,
        timeout: int = 30
    ) -> Optional[bytes]:
        """POST request that returns binary content (e.g., audio/wav).

        Args:
            url: The URL to POST to
            data: JSON body to send
            timeout: Request timeout in seconds

        Returns:
            Response content as bytes, or None on error
        """
        session = RestClient._get_session()
        headers: Dict[str, str] = RestClient._build_auth_header()
        headers["Content-Type"] = "application/json"

        try:
            response = session.post(url, json=data, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.content
        except requests.RequestException as e:
            logger.error("REST POST binary request failed", url=url, error=str(e))
            return None

    @staticmethod
    def inject_memories(
        memories: List[MemoryItem],
        household_id: str | None = None,
    ) -> Optional[Dict[str, Any]]:
        """Inject memories into the command center's memory system.

        Calls POST /api/v0/memories/inject with node auth (X-API-Key).
        The endpoint auto-derives household_id from the node's credentials
        when not provided.

        Each memory item should have:
            - content (str): The memory text
            - key (str): Stable dedup key (e.g. "news:general:2026-05-02:abc123")
            - category (str): Category like "news", "calendar", "weather"
            - ttl_hours (float): Hours until expiry (default 24)
            - source (str): Attribution (e.g. "news-agent", "calendar-agent")
            - user_id (int|None): Specific user, or None for household-wide

        Args:
            memories: List of memory item dicts
            household_id: Optional household override (auto-derived from node auth)

        Returns:
            Response dict with injected/updated/deduplicated counts, or None on error
        """
        if not memories:
            return {"injected": 0, "updated": 0, "deduplicated": 0, "errors": []}

        from utils.service_discovery import get_command_center_url

        cc_url = get_command_center_url()
        if not cc_url:
            logger.warning("Cannot inject memories — command center URL not configured")
            return None

        payload: Dict[str, Any] = {"memories": memories}
        if household_id:
            payload["household_id"] = household_id

        result = RestClient.post(
            f"{cc_url}/api/v0/memories/inject",
            data=payload,
            timeout=15,
        )

        if result:
            injected = result.get("injected", 0)
            updated = result.get("updated", 0)
            if injected or updated:
                logger.info(
                    "Injected memories into CC",
                    injected=injected,
                    updated=updated,
                    deduplicated=result.get("deduplicated", 0),
                )
        return result
