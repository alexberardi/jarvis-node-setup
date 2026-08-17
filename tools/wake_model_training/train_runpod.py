#!/usr/bin/env python3
"""Provision a RunPod GPU pod, train hey_jarvis_music, download artifacts.

Follows the harness pattern in
jarvis-llm-proxy-api/scripts/runpod/train_date_adapters.py: SDK
provisioning → SSH/SCP upload → remote training → artifact download →
terminate.

openWakeWord's classifier head is tiny (a small DNN over frozen
melspec/embedding features) — it trains on modest GPUs. A 3090/4090
community pod (~$0.30-0.70/hr) is plenty; most wall time goes to
synthetic TTS generation + augmentation/feature extraction, not the
training loop. Budget ~$5-15 total.

Usage:

    # Full run (provisions pod, trains, downloads, terminates)
    python train_runpod.py --api-key <RUNPOD_KEY>

    # Show the plan without touching RunPod
    python train_runpod.py --dry-run

    # Resume on an existing pod / keep the pod afterwards
    python train_runpod.py --api-key <KEY> --pod-id <POD_ID> --keep-pod

Requires locally: pip install runpod paramiko scp
Artifacts land in tools/wake_model_training/artifacts/:
    hey_jarvis_music.onnx, hey_jarvis_music.tflite, metadata.json
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import DEFAULT_SNR_GRID_DB, MUSIC_MODEL_NAME  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
LOCAL_ARTIFACTS_DIR = SCRIPT_DIR / "artifacts"

# Files shipped to the pod — the whole pipeline package.
UPLOAD_FILES = [
    "common.py",
    "generate_positives.py",
    "augment_music.py",
    "remote_train_wake.py",
    "requirements-remote.txt",
]

# Artifacts pulled back after training.
DOWNLOAD_FILES = [
    f"{MUSIC_MODEL_NAME}.onnx",
    f"{MUSIC_MODEL_NAME}.tflite",
    "metadata.json",
]

# Modest GPU on purpose — see module docstring.
POD_CONFIG = {
    "name": "jarvis-wake-model-training",
    "image_name": "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04",
    "gpu_type_id": "NVIDIA GeForce RTX 4090",
    "gpu_count": 1,
    "volume_in_gb": 80,       # MUSAN + features + clips ≈ 40-50 GB
    "container_disk_in_gb": 40,
    "min_vcpu_count": 8,
    "min_memory_in_gb": 32,
    "ports": "22/tcp",
    "docker_args": "",
    # SECURE cloud: community hosts twice sat >15 min never starting the
    # container (huge -devel image on slow links) with SSH resets the whole
    # time. Secure-cloud hosts pull fast and are worth the small premium.
    "cloud_type": "SECURE",
    # PUBLIC_KEY is injected into the pod's authorized_keys by the runpod
    # pytorch template — this keeps the harness self-contained instead of
    # depending on SSH keys registered in the RunPod account settings
    # (which is how the first launch failed: account had no key for this
    # machine). Populated at runtime in main().
    "env": {},
}


def _local_ssh_public_key() -> str | None:
    """Best-effort public key matching a default private key.

    Reads the .pub sibling when present and non-empty (an empty .pub has
    been observed in the wild and silently disabled key injection); falls
    back to deriving the public key from the private key via ssh-keygen -y.
    """
    for name in ("id_ed25519.pub", "id_rsa.pub"):
        path = os.path.expanduser(f"~/.ssh/{name}")
        if os.path.exists(path):
            with open(path) as f:
                content = f.read().strip()
            if content:
                return content
    for name in ("id_ed25519", "id_rsa"):
        priv = os.path.expanduser(f"~/.ssh/{name}")
        if os.path.exists(priv):
            try:
                out = subprocess.run(
                    ["ssh-keygen", "-y", "-f", priv],
                    capture_output=True, text=True, timeout=10,
                    stdin=subprocess.DEVNULL,
                )
                if out.returncode == 0 and out.stdout.strip():
                    return out.stdout.strip()
            except (subprocess.TimeoutExpired, OSError):
                continue
    return None

FALLBACK_GPU_TYPES = [
    "NVIDIA GeForce RTX 3090",
    "NVIDIA RTX A5000",
    "NVIDIA RTX A4500",
    "NVIDIA L4",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--api-key", default=os.getenv("RUNPOD_API_KEY"),
                   help="RunPod API key (or env RUNPOD_API_KEY)")
    p.add_argument("--pod-id", default=None,
                   help="Resume on an existing pod instead of provisioning")
    p.add_argument("--n-positives", type=int, default=5000)
    p.add_argument("--steps", type=int, default=50000)
    p.add_argument("--snr-grid",
                   default=",".join(str(s) for s in DEFAULT_SNR_GRID_DB))
    p.add_argument("--stages", default=None,
                   help="Pass through to remote_train_wake.py --stages")
    p.add_argument("--keep-pod", action="store_true",
                   help="Don't terminate the pod after training")
    p.add_argument("--ssh-key", default=None,
                   help="SSH private key path (default: ~/.ssh/id_ed25519)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan; no RunPod calls, no SSH")
    return p.parse_args()


def wait_for_pod_ready(runpod, pod_id: str, timeout: int = 1500) -> dict:
    """Wait for the CONTAINER to be up, not just the pod to be scheduled.

    desiredStatus=RUNNING + mapped ports only means the host accepted the
    pod — the container may still be pulling the (large) image, during
    which SSH connects are reset. uptimeInSeconds > 0 is the signal that
    the container actually started.
    """
    print(f"   Waiting for pod {pod_id} to be ready...")
    start = time.time()
    while time.time() - start < timeout:
        pod = runpod.get_pod(pod_id)
        status = pod.get("desiredStatus", "unknown")
        runtime = pod.get("runtime") or {}
        # NOTE: runtime.uptimeInSeconds is NOT populated by the SDK's
        # get_pod query (observed None on a live pod with sshd answering) —
        # do not gate readiness on it. Ports-mapped + the SSH retry ring in
        # get_ssh_connection is the real readiness check.
        if status == "RUNNING" and runtime.get("ports"):
            print(f"   ✅ Pod ready! Status: {status}")
            return pod
        print(f"   ⏳ Status: {status}, waiting...", end="\r")
        time.sleep(10)
    raise TimeoutError(f"Pod {pod_id} not ready after {timeout}s")


def get_ssh_connection(pod: dict, ssh_key_path: str | None):
    import paramiko

    ssh_host = ssh_port = None
    for port_info in pod.get("runtime", {}).get("ports", []):
        if port_info.get("privatePort") == 22:
            ssh_host = port_info.get("ip")
            ssh_port = port_info.get("publicPort")
            break
    if not ssh_host or not ssh_port:
        raise ConnectionError("Could not find SSH port in pod info")

    print(f"   Connecting to {ssh_host}:{ssh_port}...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    key_paths = [p for p in [
        ssh_key_path,
        os.path.expanduser("~/.ssh/id_ed25519"),
        os.path.expanduser("~/.ssh/id_rsa"),
    ] if p and os.path.exists(p)]

    # The pod reports RUNNING (with the port mapped) before the container's
    # start script has written the PUBLIC_KEY env into authorized_keys and
    # (sometimes) before sshd accepts connections — a single-shot auth
    # attempt lands in that gap and fails spuriously. Retry the whole key
    # ring for a few minutes; AuthenticationException here usually means
    # "not provisioned yet", not "wrong key".
    deadline = time.time() + 900
    last_error: Exception | None = None
    while time.time() < deadline:
        for key_path in key_paths:
            try:
                client.connect(hostname=ssh_host, port=ssh_port,
                               username="root", key_filename=key_path,
                               timeout=30)
                print(f"   ✅ SSH connected via {key_path}")
                return client
            except (paramiko.AuthenticationException, paramiko.SSHException,
                    OSError) as e:
                last_error = e
        print("   ⏳ SSH not ready yet, retrying...", end="\r")
        time.sleep(15)
    raise ConnectionError(
        f"SSH failed for 900s with all available keys (last: {last_error})")


def ssh_exec(client, cmd: str, timeout: int = 3600) -> tuple[str, str, int]:
    print(f"   $ {cmd[:100]}{'...' if len(cmd) > 100 else ''}")
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out_lines = []
    for line in stdout:
        line = line.rstrip()
        out_lines.append(line)
        print(f"      {line}")
    err = stderr.read().decode()
    exit_code = stdout.channel.recv_exit_status()
    return "\n".join(out_lines), err, exit_code


def upload_files(client) -> None:
    from scp import SCPClient

    print("\n📤 Uploading pipeline...")
    ssh_exec(client, "mkdir -p /workspace/tools /workspace/out")
    with SCPClient(client.get_transport()) as scp:
        for fname in UPLOAD_FILES:
            local = SCRIPT_DIR / fname
            if not local.is_file():
                print(f"   ⚠️ missing {fname}, skipping")
                continue
            scp.put(str(local), f"/workspace/tools/{fname}")
            print(f"   ✅ {fname}")


def install_deps(client) -> None:
    print("\n📦 Installing dependencies...")
    ssh_exec(client,
             "pip install -r /workspace/tools/requirements-remote.txt",
             timeout=1200)


def run_training(client, args: argparse.Namespace) -> bool:
    print(f"\n🏋️ Training {MUSIC_MODEL_NAME}...")
    cmd = (
        "cd /workspace && python tools/remote_train_wake.py"
        " --workspace /workspace"
        " --output /workspace/out"
        f" --n-positives {args.n_positives}"
        f" --steps {args.steps}"
        f" --snr-grid '{args.snr_grid}'"
    )
    if args.stages:
        cmd += f" --stages {args.stages}"
    # TTS generation + augmentation + training: allow up to 6 hours.
    _, err, exit_code = ssh_exec(client, cmd, timeout=21600)
    if exit_code != 0:
        print(f"   ❌ Training failed (exit {exit_code})")
        if err:
            print(f"   stderr: {err[-800:]}")
        return False
    return True


def download_artifacts(client) -> list[str]:
    from scp import SCPClient

    print("\n📥 Downloading artifacts...")
    LOCAL_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    downloaded = []
    with SCPClient(client.get_transport()) as scp:
        for fname in DOWNLOAD_FILES:
            remote = f"/workspace/out/{fname}"
            _, _, code = ssh_exec(client, f"test -f {remote} && echo exists",
                                  timeout=10)
            if code != 0:
                print(f"   ⚠️ missing on pod: {fname}")
                continue
            local = LOCAL_ARTIFACTS_DIR / fname
            scp.get(remote, str(local))
            size_mb = local.stat().st_size / (1024 * 1024)
            print(f"   ✅ {fname} ({size_mb:.1f} MB)")
            downloaded.append(fname)
    return downloaded


def main() -> int:
    args = parse_args()

    print("=" * 60)
    print(f"RUNPOD WAKE-MODEL TRAINING: {MUSIC_MODEL_NAME}")
    print("=" * 60)
    print(f"  GPU:        {POD_CONFIG['gpu_type_id']} "
          f"(fallbacks: {', '.join(FALLBACK_GPU_TYPES)})")
    print(f"  positives:  {args.n_positives}")
    print(f"  steps:      {args.steps}")
    print(f"  SNR grid:   {args.snr_grid}")
    print(f"  est. cost:  ~$5-15 (3-8 hrs wall @ ~$0.30-0.70/hr; most of it")
    print("              TTS generation + feature extraction, not training)")
    print(f"  artifacts:  {LOCAL_ARTIFACTS_DIR}")

    if args.dry_run:
        print("\nDRY RUN — no pod provisioned, nothing uploaded.")
        print("Remote command that would run:")
        print(f"  python tools/remote_train_wake.py --workspace /workspace"
              f" --output /workspace/out --n-positives {args.n_positives}"
              f" --steps {args.steps} --snr-grid '{args.snr_grid}'"
              + (f" --stages {args.stages}" if args.stages else ""))
        return 0

    if not args.api_key:
        print("\n❌ --api-key (or RUNPOD_API_KEY) required for a real run")
        return 1

    try:
        import runpod
    except ImportError:
        print("Install runpod SDK: pip install runpod")
        return 1
    try:
        import paramiko  # noqa: F401
        from scp import SCPClient  # noqa: F401
    except ImportError:
        print("Install SSH tools: pip install paramiko scp")
        return 1

    runpod.api_key = args.api_key
    pod_id = args.pod_id
    created_pod = False
    success = False

    try:
        if pod_id:
            print(f"📡 Connecting to existing pod: {pod_id}")
            pod = wait_for_pod_ready(runpod, pod_id)
        else:
            print("🚀 Creating RunPod instance...")
            pub_key = _local_ssh_public_key()
            if pub_key:
                POD_CONFIG["env"] = {**POD_CONFIG["env"], "PUBLIC_KEY": pub_key}
            else:
                print("   ⚠️ no local SSH public key found — relying on "
                      "account-registered keys")
            try:
                pod = runpod.create_pod(**POD_CONFIG)
                pod_id = pod["id"]
                created_pod = True
                print(f"   Pod created: {pod_id}")
            except Exception as e:
                print(f"   Primary GPU unavailable: {e}")
                for gpu_type in FALLBACK_GPU_TYPES:
                    try:
                        config = {**POD_CONFIG, "gpu_type_id": gpu_type}
                        pod = runpod.create_pod(**config)
                        pod_id = pod["id"]
                        created_pod = True
                        print(f"   Pod created with {gpu_type}: {pod_id}")
                        break
                    except Exception:
                        continue
                else:
                    print("❌ No GPU available on RunPod")
                    return 1
            pod = wait_for_pod_ready(runpod, pod_id)

        client = get_ssh_connection(pod, args.ssh_key)
        upload_files(client)
        install_deps(client)

        t0 = time.time()
        success = run_training(client, args)
        if success:
            downloaded = download_artifacts(client)
            print("\n" + "=" * 60)
            print("TRAINING COMPLETE")
            print("=" * 60)
            print(f"  Wall time: {(time.time() - t0) / 60:.1f} min")
            print(f"  Artifacts: {downloaded}")
            print(f"  Next: python {SCRIPT_DIR}/evaluate.py "
                  f"--model {LOCAL_ARTIFACTS_DIR / (MUSIC_MODEL_NAME + '.onnx')}"
                  " --compare-stock ...")
        client.close()

    finally:
        if created_pod and not args.keep_pod and pod_id:
            print(f"\n🗑️ Terminating pod {pod_id}...")
            try:
                runpod.terminate_pod(pod_id)
                print("   Pod terminated")
            except Exception as e:
                print(f"   ⚠️ Failed to terminate pod: {e}")
                print("   Manually terminate: https://www.runpod.io/console/pods")

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
