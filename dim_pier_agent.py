"""Pier agent adapter for the released dim CLI (npm dimcode@0.5.2).

Mirrors dim_harbor_agent.py for the Pier runner (DeepSWE tasks): the CLI is
bind-mounted read-only into the task container at /opt/dim-agent via
--mounts-json, the provider is registered in a fresh container-local
DIMCODE_HOME per trial, and `dim exec --trace=<dir>` is used because 0.5.2 has
no --provider/--model/--json exec flags.

Pier's BaseInstalledAgent has no ERROR_PATTERNS registry; stderr is still
replayed so infra failures remain visible in the runner log.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import override

from pier.agents.installed.base import BaseInstalledAgent, with_prompt_template
from pier.environments.base import BaseEnvironment
from pier.models.agent.context import AgentContext

#: Read-only bind mount of the CLI build under test (install-dimcode.sh).
MOUNT_PATH = "/opt/dim-agent"
DIM_BIN = f"{MOUNT_PATH}/dimcode"

#: Written inside the environment; synced back to ``logs_dir`` after the trial.
TRACE_DIR = "/logs/agent/trace"
STDERR_FILENAME = "dim-stderr.log"

#: The mount is read-only, so CLI state must live on the writable layer. A fresh
#: home per trial also keeps the provider registry unambiguous.
DIMCODE_HOME = "/tmp/dimcode-home"

#: Pinned provider identity (must match dim_harbor_agent.py).
PROVIDER_ID = "icecn"
PROVIDER_BASE_URL = "https://icecn.qwenkimi.com/v1"
PROVIDER_ADAPTER = "openai"
MODEL_ID = "glm-5.3"
KEY_ENV = "ICECN_API_KEY"

#: See dim_harbor_agent.py: GLM-5.3 defaults to effort "max" server-side and
#: exhausts the 0.5.2 continuation budget; the evaluation pins effort "high"
#: via the undocumented --reasoning-effort flag plus a sqlite capability
#: injection (custom-provider entries are born reasoning:false).
REASONING_EFFORT = "high"

#: Single-quoted python program run inside the task container after
#: `provider add` (must match dim_harbor_agent.py).
CAPABILITY_INJECTION = (
    "import json,sqlite3,time,sys\n"
    "db=sqlite3.connect('" + DIMCODE_HOME + "/v2/dimcode.sqlite')\n"
    "row=db.execute(\"SELECT models FROM providers WHERE providerId='"
    + PROVIDER_ID + "'\").fetchone()\n"
    "if row is None or not row[0]: sys.exit('no provider row')\n"
    "models=json.loads(row[0])\n"
    "hit=False\n"
    "for m in models:\n"
    "    if m.get('modelId')!='" + MODEL_ID + "': continue\n"
    "    hit=True\n"
    "    m.setdefault('capabilities',{})['reasoning']=True\n"
    "    m['capabilities']['maxOutputTokens']=131072\n"
    "    m['capabilities']['contextWindow']=1000000\n"
    "    m.setdefault('metadata',{})['reasoning']={'supported':True,"
    "'defaultEnabled':True,'mode':'effort','effort':'" + REASONING_EFFORT + "',"
    "'effortOptions':['low','high','max']}\n"
    "    m['metadata']['maxTokens']=131072\n"
    "if not hit: sys.exit('model entry not found')\n"
    "now=time.strftime('%Y-%m-%dT%H:%M:%S',time.gmtime())+'.000Z'\n"
    "db.execute(\"UPDATE providers SET models=?,modelsUpdatedAt=? WHERE "
    "providerId='" + PROVIDER_ID + "'\",(json.dumps(models),now))\n"
    "db.commit()\n"
    "print('capabilities injected')\n"
)


def api_key_env_var(provider: str) -> str:
    """Fallback name for the key's env var, e.g. icecn -> ICECN_API_KEY."""
    return f"{provider.upper().replace('-', '_')}_API_KEY"


class DimAgent(BaseInstalledAgent):
    """Runs ``dim exec`` inside a Pier environment."""

    SUPPORTS_ATIF = False
    SUPPORTS_WINDOWS = False

    @staticmethod
    @override
    def name() -> str:
        return "dim"

    def install_spec(self):
        """No build-time install: the binary arrives via the read-only bind mount."""
        return None

    @override
    def version(self) -> str | None:
        return "dimcode 0.5.2"

    @override
    def get_version_command(self) -> str | None:
        return f"{DIM_BIN} --version"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Verify the mounted build and put it on PATH."""
        await self.exec_as_agent(
            environment,
            command=(
                "set -eu; "
                f"test -x {DIM_BIN} "
                f'|| {{ echo "dim CLI not mounted at {MOUNT_PATH}" >&2; exit 1; }}; '
                f"mkdir -p $HOME/.local/bin && ln -sf {DIM_BIN} $HOME/.local/bin/dim; "
                f"{DIM_BIN} --version"
            ),
        )

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        if not self.model_name or "/" not in self.model_name:
            raise ValueError("model_name must be formatted as 'provider/model'")
        provider, model_id = self.model_name.split("/", 1)
        if provider != PROVIDER_ID or model_id != MODEL_ID:
            raise ValueError(
                f"model_name {self.model_name!r} does not match the pinned "
                f"provider/model {PROVIDER_ID}/{MODEL_ID}"
            )

        env = {
            "DIMCODE_DISABLE_AUTOUPDATE": "1",
            "DIMCODE_HOME": DIMCODE_HOME,
            **self.extra_env,
        }
        key_var = env.get("DIM_EVAL_API_KEY_ENV") or api_key_env_var(provider)

        # The provider registry (stub key + injected reasoning capabilities) is
        # pre-provisioned on the runtime host and bind-mounted into the task
        # container at /opt/dim-eval-home; copy it onto the writable layer.
        # Task containers may lack python3, so the sqlite capability injection
        # runs host-side at install time (see dim_harbor_agent.py).
        await self.exec_as_agent(
            environment,
            command=(
                "set -eu; "
                f'test -n "${{{key_var}:-}}" '
                f'|| {{ echo "{key_var} is empty in the task environment" >&2; exit 1; }}; '
                f"rm -rf {DIMCODE_HOME}; cp -a /opt/dim-eval-home {DIMCODE_HOME}; "
                "mkdir -p /logs/agent; "
                f"{DIM_BIN} provider list 2>/dev/null | grep -q '^icecn[[:space:]]' "
                f'|| {{ echo "icecn provider missing from the pre-provisioned home" >&2; exit 1; }}'
            ),
            env=env,
        )

        await self.exec_as_agent(
            environment,
            command=(
                f"{DIM_BIN} exec "
                f"--reasoning-effort {shlex.quote(REASONING_EFFORT)} "
                f"--trace={shlex.quote(TRACE_DIR)} "
                f"{shlex.quote(instruction)} "
                f">/logs/agent/dim-stdout.txt 2>/logs/agent/{STDERR_FILENAME}; "
                f"rc=$?; cat /logs/agent/{STDERR_FILENAME} >&2; exit $rc"
            ),
            env=env,
        )

    @override
    def populate_context_post_run(self, context: AgentContext) -> None:
        """Extract usage totals and turn counts from the CLI trace event stream.

        Identical accounting to dim_harbor_agent.py: each `model.request`
        phase=success event carries that call's usage; totals are summed across
        turns; `dim_usage_complete` stays false when subagent runs appear.
        """
        trace_root: Path = self.logs_dir / "trace"
        if not trace_root.exists():
            return

        run_files = sorted(trace_root.glob("*/run_*.jsonl"))
        if not run_files:
            return

        # Persist the main session's event stream under a fixed name for the
        # usage-details parser (see dim_harbor_agent.py).
        try:
            shutil.copyfile(run_files[0], self.logs_dir / "dim-trace.jsonl")
        except OSError:
            pass

        usage: dict[str, int] = {}
        turns = 0
        subagent_runs = 0
        end_reason: str | None = None
        seen_turns: set[int] = set()

        for run_file in run_files:
            with run_file.open(errors="ignore") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    name = event.get("event")
                    phase = event.get("phase")
                    metadata = event.get("metadata") or {}
                    if name == "model.request":
                        turn_id = metadata.get("turnId")
                        if turn_id is not None and turn_id not in seen_turns:
                            seen_turns.add(turn_id)
                            turns += 1
                        if phase == "success":
                            call_usage = metadata.get("usage") or {}
                            usage["promptTokens"] = (
                                usage.get("promptTokens", 0)
                                + int(call_usage.get("promptTokens") or 0)
                            )
                            usage["completionTokens"] = (
                                usage.get("completionTokens", 0)
                                + int(call_usage.get("completionTokens") or 0)
                            )
                            usage["cacheReadTokens"] = (
                                usage.get("cacheReadTokens", 0)
                                + int(call_usage.get("cacheReadTokens") or 0)
                            )
                    elif name == "session.run.end":
                        end_reason = metadata.get("reason") or metadata.get("status")
                    elif isinstance(name, str) and name.startswith("subagent"):
                        if phase in ("start", "started"):
                            subagent_runs += 1

        prompt_tokens = usage.get("promptTokens", 0)
        cache_read_tokens = usage.get("cacheReadTokens", 0)

        context.n_input_tokens = prompt_tokens
        context.n_output_tokens = usage.get("completionTokens", 0)
        context.n_cache_tokens = cache_read_tokens

        usage_complete = subagent_runs == 0

        context.metadata = {
            **(getattr(context, "metadata", None) or {}),
            "dim_turns": turns,
            "dim_uncached_input_tokens": max(prompt_tokens - cache_read_tokens, 0),
            "dim_end_reason": end_reason,
            "dim_subagent_runs": subagent_runs,
            "dim_usage_complete": usage_complete,
        }

        if not usage_complete:
            self.logger.warning(
                "%s subagent run(s) in this trial: cost and turns may exclude their "
                "provider calls and must not be compared as-is",
                subagent_runs,
            )
