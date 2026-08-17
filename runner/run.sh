#!/usr/bin/env bash
set -euo pipefail

# Pier imports LiteLLM while loading agents. Use its bundled pricing metadata
# instead of making an unrelated GitHub request during CLI startup.
export LITELLM_LOCAL_MODEL_COST_MAP=True

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
default_task=2026-07-15-notional-v1
task_id=$default_task

tasks=()
for task_file in "$repo_root"/tasks/*/task.toml; do
  tasks+=("$(basename "$(dirname "$task_file")")")
done

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

task_path="$repo_root/tasks/$task_id"
chain=$(awk -F'"' '/^chain = / { print $2; exit }' "$task_path/task.toml")
case "$chain" in
  ethereum) rpc_variable=ETH_RPC_URL; alchemy_network=eth-mainnet ;;
  bsc) rpc_variable=BSC_RPC_URL; alchemy_network=bnb-mainnet ;;
  arbitrum) rpc_variable=ARBITRUM_RPC_URL; alchemy_network=arb-mainnet ;;
  base) rpc_variable=BASE_RPC_URL; alchemy_network=base-mainnet ;;
  polygon) rpc_variable=POLYGON_RPC_URL; alchemy_network=polygon-mainnet ;;
  avalanche) rpc_variable=AVALANCHE_RPC_URL; alchemy_network=avax-mainnet ;;
  optimism) rpc_variable=OPTIMISM_RPC_URL; alchemy_network=opt-mainnet ;;
  linea) rpc_variable=LINEA_RPC_URL; alchemy_network=linea-mainnet ;;
  blast) rpc_variable=BLAST_RPC_URL; alchemy_network=blast-mainnet ;;
  gnosis) rpc_variable=GNOSIS_RPC_URL; alchemy_network=gnosis-mainnet ;;
  mantle) rpc_variable=MANTLE_RPC_URL; alchemy_network=mantle-mainnet ;;
  sei) rpc_variable=SEI_RPC_URL; alchemy_network=sei-mainnet ;;
  *) echo "unsupported task chain: $chain" >&2; exit 2 ;;
esac
if [[ -n "${ALCHEMY_API_KEY:-}" ]]; then
  rpc_url="https://${alchemy_network}.g.alchemy.com/v2/${ALCHEMY_API_KEY}"
else
  rpc_url=${!rpc_variable:-}
fi
[[ -n "$rpc_url" ]] || {
  echo "set ALCHEMY_API_KEY or $rpc_variable for $chain archive access" >&2
  exit 2
}
printf -v "$rpc_variable" '%s' "$rpc_url"
export "$rpc_variable"
export RL_TASK_ARCHIVE_RPC=$rpc_url

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
