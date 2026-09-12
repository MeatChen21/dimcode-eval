# dimcode-eval

FrontierHarness Eval wrapper for the [dim-agent](https://www.npmjs.com/package/dimcode)
harness (`DimAgent` desktop coding agent). This repository exists because the
provisioning workflow of
[frontier-harness-eval/eval](https://github.com/frontier-harness-eval/eval)
requires a `git clone`-able repository and a pinned commit for the harness
under evaluation, while the harness itself is distributed as the npm package
`dimcode` (no public source repository).

## What is pinned

| Item | Value |
| --- | --- |
| Harness entity | npm [`dimcode@0.5.2`](https://www.npmjs.com/package/dimcode/v/0.5.2) (latest release at pin time) |
| Provider | `icecn` — OpenAI-compatible gateway `https://icecn.qwenkimi.com/v1`, adapter `openai` |
| Model | `glm-5.3` (**not** Kimi K3 → runs are non-comparable with the published leaderboard; disclosed in reports) |

## Layout

- `install-dimcode.sh` — provision step 6 script: `npm install -g dimcode@0.5.2`,
  materialize the linux-x64 binary to `/opt/dim-agent`, write the Harbor
  bind-mount overlay `/work/dim-mount-overlay.yaml`.
- `dim_harbor_agent.py` — Harbor custom agent (`dim_harbor_agent:DimAgent`),
  used for `terminal-bench/*` tasks.
- `dim_pier_agent.py` — Pier custom agent (`dim_pier_agent:DimAgent`), used for
  `datacurve/*` (DeepSWE) tasks.

## How the harness reaches task containers

Task containers do not contain the CLI. The build runtime bind-mounts
`/opt/dim-agent` read-only into each container:

- Harbor: `--extra-docker-compose /work/dim-mount-overlay.yaml` (in addition to
  the Runta CA overlay).
- Pier: `--mounts-json '[{"type":"bind","source":"/opt/dim-agent","target":"/opt/dim-agent","read_only":true}]'`.

Each trial registers the provider into a fresh container-local
`DIMCODE_HOME=/tmp/dimcode-home` (0.5.2 resolves `provider/model` by id; a clean
home with exactly one provider avoids the multi-provider ambiguity observed on
0.5.2). The API key is never interpolated: inside containers it is the Runta
secret stub (`runta-secret-stub`), swapped for the real key by the egress proxy.

## Usage accounting

0.5.2 has no `--json` exec flag. `dim exec --trace=<dir>` writes a JSONL event
stream; each `model.request` event with `phase: "success"` carries that call's
usage (`promptTokens`, `completionTokens`, `cacheReadTokens`). Adapters sum
usage across turns and report totals through the runner's `AgentContext`.

Known limitation (disclosed in every report): subagent child sessions may not
include their provider usage in the event stream, so totals can undercount when
subagents are used. Trials flag this via `dim_usage_complete=false`.

## Reproduce

```bash
bash provision-golden-checkpoint.sh \
  --runtime fh-build --checkpoint fh-golden-dim-v1 \
  --harness dim \
  --provider custom --model icecn/glm-5.3 \
  --secret-name ICECN_API_KEY --secret-host icecn.qwenkimi.com \
  --repo <this repo> --commit <pinned sha> \
  --cpus 4 --memory 8192 --disk-size-gib 50 --keep-runtime \
  --install-script install-dimcode.sh
```
