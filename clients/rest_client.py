import requests
from typing import Any, Dict, Optional

from jarvis_log_client import JarvisLogger

from utils.config_service import Config

logger = JarvisLogger(service="jarvis-node")


class RestClient:
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
        headers: Dict[str, str] = RestClient._build_auth_header()

        request_args: Dict[str, Any] = {"headers": headers, "timeout": timeout}

        if data is not None:
            request_args["json"] = data

        if files is not None:
            request_args["files"] = files

        try:
            response = requests.post(url, **request_args)
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
        headers: Dict[str, str] = RestClient._build_auth_header()

        request_args: Dict[str, Any] = {"headers": headers, "timeout": timeout}

        try:
            response = requests.get(url, **request_args)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error("REST GET request failed", url=url, error=str(e))
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
        headers: Dict[str, str] = RestClient._build_auth_header()
        headers["Content-Type"] = "application/json"

        try:
            response = requests.post(url, json=data, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.content
        except requests.RequestException as e:
            logger.error("REST POST binary request failed", url=url, error=str(e))
            return None
