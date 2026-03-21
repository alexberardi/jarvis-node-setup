#!/bin/bash
# Build a self-contained tarball for jarvis-node-setup.
# Runs inside a QEMU-emulated ARM container in CI.
#
# Usage: ./build-tarball.sh <version> <arch>
#   version: e.g. "0.1.0" (from git tag, without leading v)
#   arch:    "armv7l" or "arm64"
#
# Output: /output/jarvis-node-<version>-<arch>.tar.gz

set -euo pipefail

VERSION="${1:?Usage: build-tarball.sh <version> <arch>}"
ARCH="${2:?Usage: build-tarball.sh <version> <arch>}"

INSTALL_DIR="/opt/jarvis-node"
BUILD_ROOT="/build"
STAGE="${BUILD_ROOT}/stage${INSTALL_DIR}"

echo "==> Building jarvis-node ${VERSION} for ${ARCH}"

# --- Install build dependencies ---
apt-get update
apt-get install -y --no-install-recommends \
  build-essential \
  git \
  portaudio19-dev \
  libsqlcipher-dev \
  libopenblas-dev \
  libffi-dev \
  libssl-dev

# --- Create directory structure ---
mkdir -p "${STAGE}"

# --- Create venv at the final install path ---
# We build in a chroot-like layout so shebangs and pyvenv.cfg have the
# correct paths when extracted to /opt/jarvis-node on the target.
python3 -m venv "${INSTALL_DIR}/.venv"

echo "==> Upgrading pip"
"${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip --quiet

# --- Install Python dependencies ---
PIP_EXTRA_ARGS=""
if [ "${ARCH}" = "armv7l" ]; then
  PIP_EXTRA_ARGS="--extra-index-url https://www.piwheels.org/simple"
fi

echo "==> Installing requirements"
"${INSTALL_DIR}/.venv/bin/python" -m pip install \
  -r /src/requirements-pi.txt \
  ${PIP_EXTRA_ARGS} \
  --quiet

echo "==> Installing openwakeword (--no-deps)"
"${INSTALL_DIR}/.venv/bin/python" -m pip install \
  openwakeword --no-deps \
  ${PIP_EXTRA_ARGS} \
  --quiet

echo "==> Installing jarvis-command-sdk"
"${INSTALL_DIR}/.venv/bin/python" -m pip install \
  /src/jarvis-command-sdk \
  --quiet

# --- Copy application source ---
echo "==> Copying application source"

# Directories to include in the tarball
SOURCE_DIRS=(
  agents
  alembic
  clients
  commands
  constants
  core
  database
  device_families
  device_managers
  exceptions
  ha_shared
  integrations
  jarvis_services
  models
  provisioning
  repositories
  scripts
  services
  sounds
  stt_providers
  tts_providers
  utils
  vendor
  wake_response_providers
)

for dir in "${SOURCE_DIRS[@]}"; do
  if [ -d "/src/${dir}" ]; then
    cp -r "/src/${dir}" "${INSTALL_DIR}/${dir}"
  fi
done

# Top-level files
cp /src/__init__.py "${INSTALL_DIR}/"
cp /src/db.py "${INSTALL_DIR}/"
cp /src/alembic.ini "${INSTALL_DIR}/"
cp /src/pyproject.toml "${INSTALL_DIR}/"
cp /src/config.example.json "${INSTALL_DIR}/"
cp /src/.env.example "${INSTALL_DIR}/"
cp /src/requirements-pi.txt "${INSTALL_DIR}/"
cp /src/requirements-base.txt "${INSTALL_DIR}/"
cp /src/requirements-provisioning.txt "${INSTALL_DIR}/"

# --- Write version file ---
echo "${VERSION}" > "${INSTALL_DIR}/VERSION"

# --- Move venv into staging area ---
mv "${INSTALL_DIR}/.venv" "${STAGE}/.venv"

# --- Move source into staging area ---
for item in "${INSTALL_DIR}"/*; do
  [ "$(basename "$item")" = ".venv" ] && continue
  mv "$item" "${STAGE}/"
done

# --- Create tarball ---
echo "==> Creating tarball"
mkdir -p /output
TARBALL="jarvis-node-${VERSION}-${ARCH}.tar.gz"
tar czf "/output/${TARBALL}" -C "${BUILD_ROOT}/stage" opt/jarvis-node

echo "==> Built /output/${TARBALL}"
ls -lh "/output/${TARBALL}"
