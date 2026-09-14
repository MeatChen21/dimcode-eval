"""Harbor agent adapter for the released dim CLI (npm dimcode@0.5.2).

The CLI is bind-mounted read-only into the task container at /opt/dim-agent by
/work/dim-mount-overlay.yaml (see install-dimcode.sh). The 0.5.2 CLI has no
--provider/--model/--json exec flags, so the provider is registered in a fresh
container-local DIMCODE_HOME per trial and `dim exec --trace=<dir>` is used.

Usage accounting reads the trace JSONL event stream: each `model.request` event
with phase "success" carries per-call usage (promptTokens, completionTokens,
cacheReadTokens) in metadata. Totals are the sum across turns; turns are
distinct turnIds. `session.run.end` carries the final status/reason.
"""

from __future__ import annotations

import json
import shlex
import shutil
from pathlib import Path
from typing import ClassVar, override

from harbor.agents.installed.base import (
    ApiInternalServerError,
    ApiOverloadedError,
    ApiRateLimitError,
    ApiUsageLimitError,
    BaseInstalledAgent,
    ErrorPattern,
    NetworkConnectionError,
    UnknownApiError,
    with_prompt_template,
)
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

#: Read-only bind mount of the CLI build under test (install-dimcode.sh).
MOUNT_PATH = "/opt/dim-agent"
DIM_BIN = f"{MOUNT_PATH}/dimcode"

#: Written inside the environment; synced back to ``logs_dir`` after the trial.
TRACE_DIR = "/logs/agent/trace"
STDERR_FILENAME = "dim-stderr.log"

#: The mount is read-only, so CLI state must live on the writable layer. A fresh
#: home per trial also keeps the provider registry unambiguous: exactly one
#: provider with exactly one model (multi-provider model-id ambiguity was
#: observed on 0.5.2 — resolution is not switch-driven).
DIMCODE_HOME = "/tmp/dimcode-home"

#: Pinned provider identity. The wrapper repo pins these so the evaluation is
#: reproducible; the API key itself only ever enters as a shell expansion of the
#: stub env var injected by the Runta egress proxy.
PROVIDER_ID = "icecn"
PROVIDER_BASE_URL = "https://icecn.qwenkimi.com/v1"
PROVIDER_ADAPTER = "openai"
MODEL_ID = "glm-5.3"
KEY_ENV = "ICECN_API_KEY"

#: GLM-5.3 always reasons and defaults to effort "max" server-side, which
#: exhausts the 0.5.2 continuation budget (output_truncated). The evaluation
#: pins effort "high" per user decision: exec's undocumented --reasoning-effort
#: flag plus a sqlite capability injection (custom-provider entries are born
#: with reasoning:false and no effortOptions, which makes the flag a no-op).
REASONING_EFFORT = "high"

#: Single-quoted python program run inside the task container after
#: `provider add`. Injects reasoning capabilities into the provider's model
#: entry so buildHeadlessProviderInvocationConfig accepts the effort flag.
#:
#: 0.5.2 gates the flag on model.capabilities.reasoning === true and
#: metadata.reasoning.effortOptions containing the effort; custom-provider
#: entries are generated with reasoning:false. The models column must be
#: rewritten with a bumped modelsUpdatedAt for the CLI to pick it up.
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
    """Runs ``dim exec`` inside a Harbor environment."""

    SUPPORTS_ATIF = False
    SUPPORTS_WINDOWS = False

    # Infra failures must be counted separately from task failures, and rate limits
    # must be retryable rather than scored as a task loss. dim's wording differs
    # from other CLIs, so it is added ahead of the base class generic patterns.
    ERROR_PATTERNS: ClassVar[list[ErrorPattern]] = [
        ErrorPattern(r"429 Too Many Requests", ApiRateLimitError),
        ErrorPattern(r"insufficient_quota|Insufficient Balance", ApiUsageLimitError),
        ErrorPattern(r"50[023] (Internal|Bad Gateway|Service Unavailable)", ApiInternalServerError),
        ErrorPattern(r"server_overloaded|model is overloaded", ApiOverloadedError),
        ErrorPattern(r"ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN", NetworkConnectionError),
        ErrorPattern(r"provider .* request failed", UnknownApiError),
        *BaseInstalledAgent.ERROR_PATTERNS,
    ]

    @staticmethod
    @override
    def name() -> str:
        return "dim"

    @override
    def get_version_command(self) -> str | None:
        return f"{DIM_BIN} --version"

    @override
    def parse_version(self, stdout: str) -> str:
        return stdout.strip().splitlines()[-1].strip()

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Verify the mounted build and put it on PATH.

        Fails fast when the mount is missing: a silently absent CLI would surface
        as every task failing, which reads like a catastrophic regression.
        """
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
            # The build under test must not replace itself mid-trial.
            "DIMCODE_DISABLE_AUTOUPDATE": "1",
            "DIMCODE_HOME": DIMCODE_HOME,
            **self.extra_env,
        }
        key_var = env.get("DIM_EVAL_API_KEY_ENV") or api_key_env_var(provider)

        # The CLI reads credentials from its own store, not from the environment,
        # so the provider has to be registered before the run. The key is passed by
        # shell expansion rather than interpolated here: Harbor records the command
        # it runs in trial.log, and an interpolated key would be written to disk for
        # every trial. Inside the task container the value is the Runta secret stub;
        # the egress proxy swaps the Authorization header for the real key.
        #
        # Missing credentials otherwise surface as a bare "Failed to execute prompt"
        # on all tasks at once, which reads like a catastrophic regression rather
        # than a setup mistake, so check before running.
        await self.exec_as_agent(
            environment,
            command=(
                "set -eu; "
                f'test -n "${{{key_var}:-}}" '
                f'|| {{ echo "{key_var} is empty in the task environment" >&2; exit 1; }}; '
                f"mkdir -p {DIMCODE_HOME} /logs/agent; "
                f"{DIM_BIN} provider add {shlex.quote(PROVIDER_ID)} "
                f"--base-url {shlex.quote(PROVIDER_BASE_URL)} "
                f"--adapter {shlex.quote(PROVIDER_ADAPTER)} "
                f"--model {shlex.quote(MODEL_ID)} "
                f'--api-key "${key_var}" >/dev/null; '
                f"{DIM_BIN} provider switch {shlex.quote(PROVIDER_ID)} "
                f"--model {shlex.quote(MODEL_ID)} >/dev/null"
            ),
            env=env,
        )

        # No pipe: Harbor runs commands through `sh -c`, where the exit status of
        # a pipeline is tee's, not the agent's — a crashed run would report success.
        # Redirecting straight to the file keeps `$?` the agent's own status.
        #
        # stderr is captured *and* replayed to the real stderr, because Harbor
        # classifies failures by matching patterns against the command's output.
        await self.exec_as_agent(
            environment,
            command=(
                f"{DIM_BIN} exec "
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

        Each `model.request` phase=success event carries that call's usage, so
        totals are summed across turns. Subagent child sessions write their own
        trace directories; their usage is counted when present, but 0.5.2 does
        not guarantee subagent usage inclusion, so `dim_usage_complete` stays
        false whenever subagent runs appear.
        """
        trace_root: Path = self.logs_dir / "trace"
        if not trace_root.exists():
            return

        run_files = sorted(trace_root.glob("*/run_*.jsonl"))
        if not run_files:
            return

        # Persist the main session's event stream under a fixed name for the
        # usage-details parser: first-cold accounting needs the first call of
        # the main session. Sorted-first is the main session because session
        # ids embed creation timestamps.
        try:
            shutil.copyfile(run_files[0], self.logs_dir / "dim-trace.jsonl")
        except OSError:
            pass

        usage: dict[str, int] = {}
        turns = 0
        tool_calls = 0
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

        # Subagent provider calls may be absent from the event stream on 0.5.2;
        # totals are then a parent-only undercount, which must not be read as a
        # cost improvement.
        usage_complete = subagent_runs == 0

        context.metadata = {
            **(getattr(context, "metadata", None) or {}),
            "dim_turns": turns,
            "dim_tool_calls": tool_calls,
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
