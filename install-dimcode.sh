#!/usr/bin/env bash
# Install the harness under evaluation inside the FrontierHarness build runtime.
#
# The entity under test is the released npm package dimcode@0.5.2 (no source
# repository is published for it; this wrapper repo pins the version). The
# platform binary is materialized on the runtime host and exposed to every task
# container as a read-only bind mount at /opt/dim-agent.
#
# Must run from /work/harness (provision-golden-checkpoint.sh step 6) with
# network access still open: the wrapper downloads the linux-x64 payload on
# first execution.
set -euo pipefail

DIMCODE_VERSION="0.5.2"
MOUNT_DIR="/opt/dim-agent"
HOST_DIMCODE_HOME="/opt/dim-eval-home"

echo "[install] npm install -g dimcode@${DIMCODE_VERSION}"
npm install -g "dimcode@${DIMCODE_VERSION}"

echo "[install] materializing linux-x64 binary (first run downloads payload)"
export DIMCODE_HOME="${HOST_DIMCODE_HOME}"
mkdir -p "${DIMCODE_HOME}"
dimcode --version

BIN_DIR="${DIMCODE_HOME}/binaries/dimcode-linux-x64/${DIMCODE_VERSION}/bin"
if [ ! -x "${BIN_DIR}/dimcode" ]; then
  echo "dimcode linux-x64 binary not found at ${BIN_DIR}" >&2
  exit 1
fi

echo "[install] staging read-only mount at ${MOUNT_DIR}"
rm -rf "${MOUNT_DIR}"
mkdir -p "${MOUNT_DIR}"
cp -a "${BIN_DIR}/." "${MOUNT_DIR}/"
"${MOUNT_DIR}/dimcode" --version

echo "[install] writing Harbor bind-mount overlay"
cat > /work/dim-mount-overlay.yaml <<'YAML'
services:
  main:
    volumes:
      - /opt/dim-agent:/opt/dim-agent:ro
YAML

echo "[install] adapters in place:"
ls -la /work/harness/dim_harbor_agent.py /work/harness/dim_pier_agent.py

echo "[install] done"
