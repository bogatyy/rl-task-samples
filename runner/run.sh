#!/usr/bin/env bash
set -euo pipefail

# Pier imports LiteLLM while loading agents. Use its bundled pricing metadata
# instead of making an unrelated GitHub request during CLI startup.
export LITELLM_LOCAL_MODEL_COST_MAP=True

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
default_task=notional-v1
task_id=$default_task

tasks=(
  notional-v1
  trusted-volumes
  projekt-reward-vault
  rwa-vault
  prxvt
  aztec-v2
  bunni-v2
)

is_task() {
  local candidate=$1
  local known
  for known in "${tasks[@]}"; do
    [[ "$candidate" == "$known" ]] && return 0
  done
  return 1
}

source_env_file() {
  local env_file=$1
  if [[ $- == *x* ]]; then
    set +x
    source "$env_file"
  else
    source "$env_file"
  fi
}

if [[ "${1:-}" == "--list" ]]; then
  printf '%s\n' "${tasks[@]}"
  exit 0
fi

if [[ "${1:-}" == "--smoke" ]]; then
  shift
  exec "$repo_root/scripts/smoke_test.sh" "${1:-all}"
fi

if [[ "${1:-}" == "--task" ]]; then
  [[ -n "${2:-}" ]] || { echo "--task requires a task ID" >&2; exit 2; }
  task_id=$2
  shift 2
fi

if ! is_task "$task_id"; then
  echo "unknown task: $task_id" >&2
  echo "use --list to show task IDs" >&2
  exit 2
fi

if [[ -f "$repo_root/.env" ]]; then
  source_env_file "$repo_root/.env"
elif [[ -f "$repo_root/../env.sh" ]]; then
  source_env_file "$repo_root/../env.sh"
fi

: "${ETHERSCAN_API_KEY:?set ETHERSCAN_API_KEY for verified-source access}"
export ETHERSCAN_API_KEY

case "$task_id" in
  prxvt)
    : "${BASE_RPC_URL:?set BASE_RPC_URL to a Base archive RPC}"
    export BASE_RPC_URL
    export RL_TASK_ARCHIVE_RPC=$BASE_RPC_URL
    ;;
  *)
    : "${ETH_RPC_URL:?set ETH_RPC_URL to an Ethereum archive RPC}"
    export ETH_RPC_URL
    export RL_TASK_ARCHIVE_RPC=$ETH_RPC_URL
    ;;
esac

task_path="$repo_root/tasks/$task_id"
docker build --tag rl-exploits-foundry-core:latest "$repo_root/docker/core"
docker build --tag "rl-exploits-${task_id}:latest" "$task_path/environment"

if command -v pier >/dev/null 2>&1; then
  cli_args=("$@")
  pier_args=("$@")
  agent_name=""
  model_name=""
  has_agent_version=0
  for ((i = 0; i < ${#cli_args[@]}; i++)); do
    case "${cli_args[$i]}" in
      --agent|-a)
        ((i + 1 < ${#cli_args[@]})) && agent_name=${cli_args[$((i + 1))]}
        ;;
      --agent=*|-a=*) agent_name=${cli_args[$i]#*=} ;;
      --model|-m)
        ((i + 1 < ${#cli_args[@]})) && model_name=${cli_args[$((i + 1))]}
        ;;
      --model=*|-m=*) model_name=${cli_args[$i]#*=} ;;
      --ak|--agent-kwarg)
        if ((i + 1 < ${#cli_args[@]})) && [[ "${cli_args[$((i + 1))]}" == version=* ]]; then
          has_agent_version=1
        fi
        ;;
      --ak=version=*|--agent-kwarg=version=*) has_agent_version=1 ;;
    esac
  done
  if [[ -n "$agent_name" && "$agent_name" != mini-swe-agent \
      && "$agent_name" != oracle && "$agent_name" != nop ]]; then
    echo "runner supports mini-swe-agent (plus trusted oracle/nop controls) so tool isolation remains enforced" >&2
    exit 2
  fi
  if [[ "$agent_name" == mini-swe-agent ]]; then
    if (( ! has_agent_version )); then
      pier_args+=(--agent-kwarg version=2.4.6)
    fi
    if [[ "$model_name" == deepseek/* || "$model_name" == deepseek-* ]]; then
      : "${DEEPSEEK_API_KEY:?set DEEPSEEK_API_KEY for direct DeepSeek inference}"
      export DEEPSEEK_API_KEY
    fi
    if [[ "$model_name" == openrouter/* ]]; then
      : "${OPENROUTER_API_KEY:?set OPENROUTER_API_KEY for OpenRouter inference}"
      export OPENROUTER_API_KEY
    fi
  fi
  export PYTHONPATH="$repo_root${PYTHONPATH:+:$PYTHONPATH}"
  exec pier run -p "$task_path" \
    --environment-import-path runner.pinned_environment:PinnedForkDockerEnvironment \
    "${pier_args[@]}"
fi
echo "runner requires pier for isolated agent networking; use --smoke for local verification" >&2
exit 127
