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


# The clean runtime ships no Node.js; the npm wrapper needs it. Install from
# apt first and fall back to NodeSource when apt's node is too old or absent.
if ! command -v npm >/dev/null 2>&1; then
  echo "[install] installing Node.js (npm not found)"
  export DEBIAN_FRONTEND=noninteractive
  if apt-get install -y -qq nodejs npm >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; then
    :
  else
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null 2>&1
    apt-get install -y -qq nodejs >/dev/null
  fi
  command -v node || { echo "node installation failed" >&2; exit 1; }
fi
node --version

echo "[install] npm install -g dimcode@${DIMCODE_VERSION}"
npm install -g "dimcode@${DIMCODE_VERSION}"

# The platform payload installs as an optionalDependency under the npm global
# tree; locate it via npm root -g (a first-run download cache is the fallback).
NPM_GLOBAL_ROOT=$(npm root -g)
BIN_DIR="${NPM_GLOBAL_ROOT}/dimcode/node_modules/dimcode-linux-x64/bin"
if [ ! -x "${BIN_DIR}/dimcode" ]; then
  BIN_DIR="/opt/dim-eval-home/binaries/dimcode-linux-x64/${DIMCODE_VERSION}/bin"
fi
if [ ! -x "${BIN_DIR}/dimcode" ]; then
  echo "dimcode linux-x64 binary not found under ${NPM_GLOBAL_ROOT}" >&2
  exit 1
fi

echo "[install] staging read-only mount at ${MOUNT_DIR}"
rm -rf "${MOUNT_DIR}"
mkdir -p "${MOUNT_DIR}"
cp -a "${BIN_DIR}/." "${MOUNT_DIR}/"
"${MOUNT_DIR}/dimcode" --version

# Pier builds a per-agent egress-proxy sidecar (squid) from ubuntu:24.04 when the
# agent declares network_allowlist(); the runtime egress allowlist does not
# include Docker Hub, so the base image must be present before trials. Same for
# the apt-based build steps, which reach *.ubuntu.com (allowed).
echo "[install] pre-pulling Pier egress-proxy base image (ubuntu:24.04)"
docker pull -q ubuntu:24.04

echo "[install] writing Harbor bind-mount overlay"
cat > /work/dim-mount-overlay.yaml <<'YAML'
services:
  main:
    volumes:
      - /opt/dim-agent:/opt/dim-agent:ro
      - /opt/dim-eval-home:/opt/dim-eval-home:ro
YAML

# Pre-provision the provider registry inside a dedicated home: the stub key
# (the egress proxy swaps it for the real one at request time), the model
# registration, and the reasoning-capability sqlite injection. Task containers
# may lack python3, so this runs host-side here; adapters copy the home onto
# the writable layer per trial.
PRESET_HOME="/opt/dim-eval-home"
echo "[install] pre-provisioning provider registry in ${PRESET_HOME}"
rm -rf "${PRESET_HOME}"
mkdir -p "${PRESET_HOME}"
export DIMCODE_HOME="${PRESET_HOME}"
dimcode provider add icecn --api-key "${ICECN_API_KEY:-runta-secret-stub}" \
  --base-url https://icecn.qwenkimi.com/v1 --model glm-5.3 --adapter openai >/dev/null
dimcode provider switch icecn --model glm-5.3 >/dev/null

python3 - <<'PYEOF'
import json, sqlite3, time, sys
db = sqlite3.connect('/opt/dim-eval-home/v2/dimcode.sqlite')
row = db.execute("SELECT models FROM providers WHERE providerId='icecn'").fetchone()
if row is None or not row[0]:
    sys.exit('no provider row')
models = json.loads(row[0])
hit = False
for m in models:
    if m.get('modelId') != 'glm-5.3':
        continue
    hit = True
    m.setdefault('capabilities', {})['reasoning'] = True
    m['capabilities']['maxOutputTokens'] = 131072
    m['capabilities']['contextWindow'] = 1000000
    m.setdefault('metadata', {})['reasoning'] = {
        'supported': True, 'defaultEnabled': True,
        'mode': 'effort', 'effort': 'high',
        'effortOptions': ['low', 'high', 'max'],
    }
    m['metadata']['maxTokens'] = 131072
if not hit:
    sys.exit('model entry not found')
now = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime()) + '.000Z'
db.execute(
    "UPDATE providers SET models=?, modelsUpdatedAt=? WHERE providerId='icecn'",
    (json.dumps(models), now),
)
db.commit()
print('capabilities injected')
PYEOF

dimcode provider list | head -3

echo "[install] adapters in place:"
ls -la /work/harness/dim_harbor_agent.py /work/harness/dim_pier_agent.py

echo "[install] done"
