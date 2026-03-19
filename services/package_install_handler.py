"""Handle package install requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/package-install with request_id + repo info
2. This handler runs install_from_github() from command_store_service
3. Results are POSTed back to CC for mobile to poll
"""

from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


def run_install_and_upload(
    request_id: str,
    command_name: str,
    github_repo_url: str,
    git_tag: str | None,
) -> None:
    """Run package install and upload results to CC. Meant to run in a background thread."""
    try:
        from services.command_store_service import install_from_github

        manifest = install_from_github(github_repo_url, version_tag=git_tag)
        logger.info(
            "Package installed successfully",
            request_id=request_id[:8],
            package=manifest.name,
            version=manifest.version,
        )
        _upload_result(request_id, success=True, details={
            "package_name": manifest.name,
            "version": manifest.version,
            "components": len(manifest.components),
        })
    except Exception as e:
        logger.error(
            "Package install failed",
            request_id=request_id[:8],
            command_name=command_name,
            error=str(e),
        )
        _upload_result(request_id, success=False, error=str(e))


def _upload_result(
    request_id: str,
    success: bool,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """POST install result to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload install result: CC URL not resolved")
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/package-install/{request_id}/results"

    payload: dict[str, Any] = {"success": success}
    if error:
        payload["error"] = error
    if details:
        payload["details"] = details

    result = RestClient.post(url, data=payload, timeout=15)
    if result is not None:
        logger.info("Install result uploaded to CC", request_id=request_id[:8], success=success)
    else:
        logger.error("Failed to upload install result to CC", request_id=request_id[:8])
