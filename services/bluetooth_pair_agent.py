"""Persistent BlueZ pairing agent.

Spawned at service startup so incoming pair requests (phone → Pi) and
profile authorizations (A2DP, AVRCP) complete without user interaction.

Why we need this: bluez requires an agent registered on its D-Bus
interface for any pair / profile-auth involving Secure Simple Pairing.
``bluetoothctl <cmd>`` invocations are one-shot processes — the agent
they register dies with the process. So the agent has to live in a
long-running process; this module is that process.

Capability: ``DisplayYesNo`` (the default for ``bluetoothctl``). iOS
refuses ``NoInputNoOutput`` ("Just Works") pairing for security
reasons, so we need a capability that can participate in the passkey
comparison flow. We auto-confirm by writing ``yes`` to bluetoothctl's
stdin whenever it prompts.
"""

import errno
import os
import pty
import re
import subprocess
import threading

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

_agent_proc: subprocess.Popen | None = None
_agent_master_fd: int | None = None
_agent_lock = threading.Lock()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# bluetoothctl emits `[CHG] Device <MAC> Paired: yes` after the SSP
# handshake completes for an iPhone-initiated pair (the "discoverable"
# flow that goes mobile app → MQTT → bluetoothctl `discoverable on`).
# That path doesn't hit the scan-flow's provider.trust() call, so
# without auto trust+connect here, the iPhone shows as Connected at
# the BlueZ control level but the A2DP profile never fully negotiates
# and the bond can drift out of sync with iOS.
_PAIRED_RE = re.compile(
    r"Device\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+Paired:\s+yes"
)


def _read_pty(fd: int) -> None:
    """Watch bluetoothctl PTY output, auto-confirm any (yes/no) prompts.

    bluetoothctl emits agent prompts (``[agent] Confirm passkey N
    (yes/no): ``) WITHOUT a trailing newline because they expect
    interactive user input. They also only show the full prompt when
    bluetoothctl detects a tty on stdout — otherwise it elides the
    interactive part. So this module gives bluetoothctl a real PTY so
    it behaves as it does for a human user, then reads the master end
    in raw chunks (a line-iterating reader would block until bluez
    cancels the request and emits a newline).
    """
    buffer = ""
    try:
        while True:
            try:
                data = os.read(fd, 4096)
            except OSError as e:
                # EIO on the master end means the bluetoothctl subprocess
                # closed its slave fd — happens every shutdown when the
                # bluez agent tears down. Not a failure; the reader thread
                # just exits cleanly.
                if e.errno == errno.EIO:
                    logger.debug("BT agent PTY closed (subprocess exited)")
                else:
                    logger.error("BT agent PTY read failed", error=str(e))
                return
            if not data:
                return
            buffer += data.decode("utf-8", errors="replace")

            # Drain complete lines, surfacing only the ones worth logging.
            # bluetoothctl is chatty (prompt redraws, [CHG] status, etc.)
            # so we filter to the events that matter for diagnosing pair
            # / agent issues.
            while "\n" in buffer:
                line, _, buffer = buffer.partition("\n")
                clean = _ANSI_RE.sub("", line).strip("\r ").strip()
                if not clean:
                    continue
                lower = clean.lower()
                if ("agent registered" in lower
                    or "agent unregistered" in lower
                    or "default agent" in lower
                    or "request confirmation" in clean
                    or "request canceled" in clean
                    or "authorize service" in lower
                    or "pairing successful" in lower
                    or "failed to" in lower
                    or "rejected" in lower):
                    logger.info("BT", line=clean[:200])

                # On `Paired: yes`, send trust+connect through the same
                # PTY so the iPhone-initiated discoverable flow gets the
                # post-pair handling that the scan-initiated MQTT flow
                # already does via provider.trust(). Both commands are
                # idempotent against repeat events from bluetoothctl.
                m = _PAIRED_RE.search(clean)
                if m is not None:
                    mac = m.group(1)
                    logger.info("BT: trust+connect after pair", mac=mac)
                    try:
                        os.write(fd, f"trust {mac}\n".encode("ascii"))
                        os.write(fd, f"connect {mac}\n".encode("ascii"))
                    except OSError as e:
                        logger.error("BT: trust/connect write failed", error=str(e))

            # Agent prompt has no newline — check the unterminated tail.
            # With NoInputNoOutput capability bluez doesn't normally
            # prompt us at all (just-works auto-pair), but this is a
            # safety net in case bluez ever falls back to interactive
            # confirmation.
            clean_tail = _ANSI_RE.sub("", buffer)
            if "(yes/no)" in clean_tail:
                logger.info("BT auto-yes", prompt=clean_tail.strip()[-80:])
                try:
                    os.write(fd, b"yes\n")
                except OSError as e:
                    logger.error("BT agent PTY write failed", error=str(e))
                    return
                buffer = ""

            if len(buffer) > 65536:
                buffer = buffer[-4096:]
    except Exception as e:
        logger.error("BT agent reader crashed", error=str(e))


def start_agent() -> None:
    """Spawn a persistent ``bluetoothctl`` registered as the default pair agent.

    Uses a PTY so bluetoothctl behaves the same as it does for a human
    user — without one it elides the interactive prompt content, and
    we'd never see the ``(yes/no)`` sentinel to auto-confirm.

    Idempotent — a second call is a no-op while the process is alive.
    The subprocess runs as a child of this Python process, so systemd
    SIGTERM tears it down via the service cgroup; explicit
    ``stop_agent`` isn't required for normal shutdown.
    """
    global _agent_proc, _agent_master_fd
    with _agent_lock:
        if _agent_proc is not None and _agent_proc.poll() is None:
            return

        master_fd, slave_fd = pty.openpty()

        try:
            proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                close_fds=True,
            )
        except FileNotFoundError:
            os.close(master_fd)
            os.close(slave_fd)
            logger.error("BT agent: bluetoothctl not found, agent NOT started")
            return

        # Parent only needs the master end.
        os.close(slave_fd)

        # bluetoothctl auto-registers a DisplayYesNo agent on startup.
        # Unregister it and re-register as NoInputNoOutput so bluez
        # handles the whole pair + profile auth flow without prompting
        # us — we have no UI to confirm anything. With the adapter's
        # Class of Device set to audio (0x240404) iOS accepts Just
        # Works pairing for this capability.
        try:
            os.write(master_fd, b"agent off\n")
            os.write(master_fd, b"agent NoInputNoOutput\n")
            os.write(master_fd, b"default-agent\n")
            os.write(master_fd, b"pairable on\n")
        except OSError as e:
            logger.error("BT agent: setup write failed", error=str(e))
            os.close(master_fd)
            proc.kill()
            return

        _agent_proc = proc
        _agent_master_fd = master_fd

        threading.Thread(
            target=_read_pty,
            args=(master_fd,),
            name="bt-agent-reader",
            daemon=True,
        ).start()

        logger.info("Bluetooth pair agent started", pid=proc.pid)


def stop_agent() -> None:
    """Stop the pair agent. Only needed for clean-shutdown logging.

    systemd SIGTERM tears down the subprocess via the cgroup anyway.
    """
    global _agent_proc, _agent_master_fd
    with _agent_lock:
        proc = _agent_proc
        fd = _agent_master_fd
        _agent_proc = None
        _agent_master_fd = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    logger.info("Bluetooth pair agent stopped")


def is_alive() -> bool:
    return _agent_proc is not None and _agent_proc.poll() is None
