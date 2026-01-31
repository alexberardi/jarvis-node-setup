import json
import requests
from typing import Any, Dict, Optional, Union
from utils.config_service import Config


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
            print(f"[RestClient] Error during POST to {url}: {e}")
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
            print(f"[RestClient] Error during GET to {url}: {e}")
            return None
