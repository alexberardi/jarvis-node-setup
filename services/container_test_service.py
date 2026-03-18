"""Container test service — runs commands in Docker for safety validation.

Builds a Docker container with the command and jarvis-command-sdk,
then runs test_harness.py inside it with strict resource and network limits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

# Path to the SDK and test harness
SDK_DIR = Path(__file__).resolve().parent.parent.parent / "jarvis-command-sdk"
HARNESS_SCRIPT = Path(__file__).resolve().parent / "test_harness.py"


@dataclass
class ContainerTestResult:
    """Result of running container tests on a command."""

    passed: bool
    summary: str
    test_count: int
    pass_count: int
    fail_count: int
    errors: list[str]
    raw_output: str


def _build_dockerfile(packages: list[str]) -> str:
    """Generate a Dockerfile for testing a command.

    Args:
        packages: Additional pip packages to install.

    Returns:
        Dockerfile content as string.
    """
    pip_install = ""
    if packages:
        pip_install = f'RUN pip install --no-cache-dir {" ".join(packages)}'

    return f"""\
FROM python:3.11-slim
WORKDIR /test
COPY sdk/ /test/sdk/
RUN pip install --no-cache-dir /test/sdk/
COPY command_files/ /test/command/
COPY test_harness.py /test/
{pip_install}
CMD ["python", "test_harness.py"]
"""


def _check_docker_available() -> bool:
    """Check if Docker is available."""
    try:
        result = subprocess.run(
            ["docker", "version"],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def run_container_tests(
    command_dir: Path,
    packages: list[str] | None = None,
    timeout_seconds: int = 60,
) -> ContainerTestResult:
    """Run container tests on a command directory.

    Args:
        command_dir: Path to directory containing command.py and manifest.
        packages: Additional pip packages to install in the container.
        timeout_seconds: Maximum time for the container to run.

    Returns:
        ContainerTestResult with test outcomes.
    """
    if not _check_docker_available():
        logger.warning("Docker not available, skipping container tests")
        return ContainerTestResult(
            passed=True,
            summary="SKIP - Docker not available",
            test_count=0,
            pass_count=0,
            fail_count=0,
            errors=[],
            raw_output="",
        )

    packages = packages or []
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-test-"))

    try:
        # Set up build context
        context_dir = tmpdir / "context"
        context_dir.mkdir()

        # Copy SDK
        sdk_dest = context_dir / "sdk"
        shutil.copytree(SDK_DIR, sdk_dest, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".venv", ".git", ".pytest_cache",
        ))

        # Copy command files
        cmd_dest = context_dir / "command_files"
        cmd_dest.mkdir()
        for f in command_dir.iterdir():
            if f.suffix == ".py" or f.name == "jarvis_command.yaml":
                shutil.copy2(f, cmd_dest / f.name)

        # Copy test harness
        shutil.copy2(HARNESS_SCRIPT, context_dir / "test_harness.py")

        # Write Dockerfile
        dockerfile = _build_dockerfile(packages)
        (context_dir / "Dockerfile").write_text(dockerfile)

        # Build image
        image_tag = "jarvis-cmd-test:latest"
        build_result = subprocess.run(
            ["docker", "build", "-t", image_tag, "."],
            cwd=context_dir,
            capture_output=True,
            text=True,
            timeout=120,
        )

        if build_result.returncode != 0:
            return ContainerTestResult(
                passed=False,
                summary=f"FAIL - Docker build failed",
                test_count=0,
                pass_count=0,
                fail_count=1,
                errors=[build_result.stderr],
                raw_output=build_result.stdout + build_result.stderr,
            )

        # Run container with strict limits
        run_cmd = [
            "docker", "run",
            "--rm",
            "--network=none",           # No network access
            "--memory=128m",            # Memory limit
            "--cpus=0.5",               # CPU limit
            "--read-only",              # Read-only filesystem
            "--tmpfs", "/tmp:rw,size=32m",  # Writable /tmp
            image_tag,
        ]

        run_result = subprocess.run(
            run_cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )

        # Parse output
        try:
            test_results = json.loads(run_result.stdout)
            return ContainerTestResult(
                passed=test_results.get("failed", 1) == 0,
                summary=test_results.get("summary", "Unknown"),
                test_count=test_results.get("passed", 0) + test_results.get("failed", 0),
                pass_count=test_results.get("passed", 0),
                fail_count=test_results.get("failed", 0),
                errors=test_results.get("errors", []),
                raw_output=run_result.stdout,
            )
        except json.JSONDecodeError:
            return ContainerTestResult(
                passed=False,
                summary="FAIL - Could not parse test output",
                test_count=0,
                pass_count=0,
                fail_count=1,
                errors=[run_result.stdout, run_result.stderr],
                raw_output=run_result.stdout + run_result.stderr,
            )

    except subprocess.TimeoutExpired:
        return ContainerTestResult(
            passed=False,
            summary=f"FAIL - Container timed out after {timeout_seconds}s",
            test_count=0,
            pass_count=0,
            fail_count=1,
            errors=["Container exceeded timeout"],
            raw_output="",
        )

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
        # Clean up image
        subprocess.run(
            ["docker", "rmi", "-f", "jarvis-cmd-test:latest"],
            capture_output=True,
            timeout=10,
        )
