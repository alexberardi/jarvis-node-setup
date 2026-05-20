"""Tests for the install_sudoers() HEREDOC in install.sh.

The install.sh function install_sudoers() generates /etc/sudoers.d/jarvis-node.
Pins:
  - the new jarvis-apt-install NOPASSWD line is present
  - the rendered sudoers passes visudo validation (where visudo is available)
  - the * wildcard sits in the argv position, never in the binary-path position

See install.sh:787-820 for the function under test.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = _REPO_ROOT / "install.sh"


def _render_sudoers_heredoc() -> str:
    """Extract the install_sudoers() HEREDOC body from install.sh and substitute SERVICE_USER."""
    text = INSTALL_SH.read_text()

    default_user_match = re.search(r'^SERVICE_USER="([^"]+)"', text, re.MULTILINE)
    assert default_user_match, "install.sh missing SERVICE_USER= default"
    service_user = default_user_match.group(1)

    func_match = re.search(r"^install_sudoers\(\)\s*\{", text, re.MULTILINE)
    assert func_match, "install_sudoers() function not found in install.sh"

    rest = text[func_match.end():]
    heredoc_start = re.search(r'cat\s+>\s+"\$tmp"\s*<<EOF\n', rest)
    assert heredoc_start, "HEREDOC marker not found in install_sudoers()"

    body_start = heredoc_start.end()
    end_match = re.search(r'^EOF\s*$', rest[body_start:], re.MULTILINE)
    assert end_match, "Closing EOF marker not found in HEREDOC"

    body = rest[body_start:body_start + end_match.start()]
    return body.replace("${SERVICE_USER}", service_user)


class TestSudoersHeredoc:
    def test_sudoers_template_includes_apt_install_wrapper_line(self):
        body = _render_sudoers_heredoc()
        # Anchor on the binary path + wildcard, tolerant of extra whitespace.
        assert "/usr/local/sbin/jarvis-apt-install" in body
        assert re.search(
            r"NOPASSWD:\s*/usr/local/sbin/jarvis-apt-install\s+\*", body
        ), f"Expected sudoers line not found. Body:\n{body}"

    @pytest.mark.skipif(shutil.which("visudo") is None, reason="visudo not available")
    def test_sudoers_template_passes_visudo_validation(self, tmp_path):
        body = _render_sudoers_heredoc()
        sudoers_file = tmp_path / "jarvis-node"
        sudoers_file.write_text(body)
        sudoers_file.chmod(0o440)
        result = subprocess.run(
            ["visudo", "-cf", str(sudoers_file)],
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"visudo rejected the rendered sudoers:\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )

    def test_sudoers_apt_install_wildcard_is_in_argv_not_path(self):
        body = _render_sudoers_heredoc()
        # Locate every sudoers line referencing jarvis-apt-install
        lines = [l for l in body.splitlines() if "jarvis-apt-install" in l]
        assert lines, "No jarvis-apt-install line found"
        for line in lines:
            m = re.search(r"NOPASSWD:\s*(\S+)", line)
            assert m, f"Line missing NOPASSWD: target — {line!r}"
            binary_path = m.group(1)
            assert binary_path == "/usr/local/sbin/jarvis-apt-install", (
                f"Binary path must be the exact wrapper, not a glob — got {binary_path!r}"
            )
            assert "*" not in binary_path

    def test_sudoers_template_includes_self_update_wrapper_line(self):
        """Mobile-triggered updates use /usr/local/sbin/jarvis-self-update.

        Without this NOPASSWD entry the update path hits a sudo password
        prompt with no TTY and silently dies (prod kitchen v0.1.49→0.1.50,
        May 2026). Pin the grant so it can't be quietly removed.
        """
        body = _render_sudoers_heredoc()
        assert "/usr/local/sbin/jarvis-self-update" in body
        assert re.search(
            r"NOPASSWD:\s*/usr/local/sbin/jarvis-self-update\s+\*", body
        ), f"Expected jarvis-self-update sudoers line not found. Body:\n{body}"
