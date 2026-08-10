#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
requested=${1:-all}
exploit_override=${SMOKE_EXPLOIT_PATH:-}
next_port=18544
rpc_url=http://127.0.0.1:18545
private_key=0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80
anvil_pid=""

tasks=(
  notional-v1
  trusted-volumes
  projekt-reward-vault
  rwa-vault
  prxvt
  aztec-v2
  bunni-v2
)

source_env_file() {
  local env_file=$1
  if [[ $- == *x* ]]; then
    set +x
    source "$env_file"
  else
    source "$env_file"
  fi
}

if [[ -f "$repo_root/.env" ]]; then
  source_env_file "$repo_root/.env"
elif [[ -f "$repo_root/../env.sh" ]]; then
  source_env_file "$repo_root/../env.sh"
fi
if [[ "$requested" == "all" || "$requested" == "prxvt" ]]; then
  : "${BASE_RPC_URL:?set BASE_RPC_URL to a Base archive RPC}"
fi
if [[ "$requested" != "prxvt" ]]; then
  : "${ETH_RPC_URL:?set ETH_RPC_URL to an Ethereum archive RPC}"
fi

if [[ "$requested" != "all" ]]; then
  valid=0
  for task_id in "${tasks[@]}"; do [[ "$requested" == "$task_id" ]] && valid=1; done
  (( valid == 1 )) || { echo "unknown smoke task: $requested" >&2; exit 2; }
fi

smoke_root=$(mktemp -d /tmp/rl-smoke.XXXXXX)
stop_anvil() {
  if [[ -n "$anvil_pid" ]]; then
    kill "$anvil_pid" 2>/dev/null || true
    wait "$anvil_pid" 2>/dev/null || true
    anvil_pid=""
    for _ in $(seq 1 40); do
      cast block-number --rpc-url "$rpc_url" >/dev/null 2>&1 || break
      sleep 0.1
    done
  fi
}
cleanup() {
  stop_anvil
  if [[ "${SMOKE_KEEP_TMP:-0}" == 1 ]]; then
    echo "[smoke] kept diagnostics at $smoke_root" >&2
  else
    case "$smoke_root" in /tmp/rl-smoke.*) rm -rf -- "$smoke_root" ;; esac
  fi
}
trap cleanup EXIT

prepare_project() {
  local task_id=$1
  local task_dir="$repo_root/tasks/$task_id"
  local project_dir="$smoke_root/$task_id"
  local exploit_source="$task_dir/solution/Exploit.sol"
  if [[ -n "$exploit_override" ]]; then
    exploit_source=$exploit_override
  fi
  [[ -f "$exploit_source" ]]
  mkdir -p "$project_dir"
  cp -R "$repo_root/docker/core/project/." "$project_dir/"
  cp -R "$task_dir/environment/project/." "$project_dir/"
  cp "$exploit_source" "$project_dir/src/Exploit.sol"
  cp "$task_dir/tests/ExploitGrader.sol" "$project_dir/src/ExploitGrader.sol"
  mkdir -p "$project_dir/script"
  cp "$repo_root/docker/core/Grade.s.sol" "$project_dir/script/Grade.s.sol"
  forge build --root "$project_dir" --offline >/dev/null
  printf '%s\n' "$project_dir"
}

start_fork() {
  local base_block=$1
  local expected_block=$2
  local target_timestamp=${3:-}
  local replay_transactions=${4:-}
  local storage_patches=${5:-}
  local archive_url=${6:-${ETH_RPC_URL:-}}
  local chain_id=${7:-1}
  local hardfork=${8:-osaka}
  [[ -n "$archive_url" ]]
  stop_anvil
  next_port=$((next_port + 1))
  rpc_url="http://127.0.0.1:$next_port"

  anvil --fork-url "$archive_url" --fork-block-number "$base_block" \
    --chain-id "$chain_id" --host 127.0.0.1 --port "$next_port" --hardfork "$hardfork" \
    --base-fee 0 --gas-price 0 --gas-limit 100000000 --silent \
    >"$smoke_root/anvil.log" 2>&1 &
  anvil_pid=$!

  for _ in $(seq 1 120); do
    cast block-number --rpc-url "$rpc_url" >/dev/null 2>&1 && break
    if ! kill -0 "$anvil_pid" 2>/dev/null; then
      echo "[smoke] Anvil failed to start; backend details were withheld to protect the archive credential" >&2
      return 1
    fi
    sleep 0.25
  done

  current=$(cast block-number --rpc-url "$rpc_url")
  [[ "$current" == "$base_block" ]] || {
    echo "expected base block $base_block, got $current" >&2
    return 1
  }

  if [[ -n "$storage_patches" ]]; then
    IFS=',' read -r -a patches <<< "$storage_patches"
    local patch target slot value extra
    for patch in "${patches[@]}"; do
      IFS=':' read -r target slot value extra <<< "$patch"
      [[ -n "$target" && -n "$slot" && -n "$value" && -z "${extra:-}" ]]
      cast rpc --rpc-url "$rpc_url" anvil_setStorageAt "$target" "$slot" "$value" >/dev/null
      [[ "$(cast storage "$target" "$slot" --rpc-url "$rpc_url")" == "$value" ]]
    done
  fi

  if [[ -n "$replay_transactions" ]]; then
    cast rpc --rpc-url "$rpc_url" evm_setAutomine false >/dev/null
    IFS=',' read -r -a replay_hashes <<< "$replay_transactions"
    local transaction_hash raw_transaction
    for transaction_hash in "${replay_hashes[@]}"; do
      raw_transaction=$(cast rpc --rpc-url "$archive_url" eth_getRawTransactionByHash "$transaction_hash")
      cast rpc --rpc-url "$rpc_url" eth_sendRawTransaction "$raw_transaction" >/dev/null
    done
  fi
  if [[ -n "$target_timestamp" ]]; then
    cast rpc --rpc-url "$rpc_url" evm_setNextBlockTimestamp "$target_timestamp" >/dev/null
  fi
  if [[ -n "$replay_transactions" || -n "$target_timestamp" ]]; then
    cast rpc --rpc-url "$rpc_url" evm_mine >/dev/null
  fi
  if [[ -n "$replay_transactions" ]]; then
    cast rpc --rpc-url "$rpc_url" evm_setAutomine true >/dev/null
  fi

  current=$(cast block-number --rpc-url "$rpc_url")
  [[ "$current" == "$expected_block" ]] || {
    echo "expected fork block $expected_block, got $current" >&2
    return 1
  }
}

run_grade_script() {
  local project_dir=$1
  (
    cd "$project_dir"
    RPC_URL="$rpc_url" forge script script/Grade.s.sol:GradeScript --offline --broadcast \
      --rpc-url "$rpc_url" --private-key "$private_key" \
      --gas-price 0 --priority-gas-price 0 --gas-estimate-multiplier 300 -v
  )
}

run_notional() {
  local project_dir
  project_dir=$(prepare_project notional-v1)
  start_fork 25541000 25541000
  run_grade_script "$project_dir"
  echo "[smoke] notional-v1 passed"
}

run_trusted() {
  local project_dir
  project_dir=$(prepare_project trusted-volumes)
  start_fork 25039669 25039669
  run_grade_script "$project_dir"
  echo "[smoke] trusted-volumes passed"
}

run_projekt() {
  local project_dir replay
  project_dir=$(prepare_project projekt-reward-vault)
  replay=0xe4b70d3c3e745237b92212750763e69da88a79cd061cf9cc8f050b02cb1f0892,0x65c0732bac3590f8581669f49a55a0e32079ed77e050f243d772a8984c82641a
  start_fork 25606411 25606412 "" "$replay"
  run_grade_script "$project_dir"
  echo "[smoke] projekt-reward-vault passed"
}

run_rwa_vault() {
  local project_dir
  project_dir=$(prepare_project rwa-vault)
  start_fork 24979315 24979316 1777388411
  run_grade_script "$project_dir"
  echo "[smoke] rwa-vault passed"
}

run_prxvt() {
  local project_dir
  project_dir=$(prepare_project prxvt)
  start_fork 40229652 40229652 "" "" "" "$BASE_RPC_URL" 8453 cancun
  run_grade_script "$project_dir"
  echo "[smoke] prxvt passed"
}

run_aztec() {
  local project_dir storage_patches
  project_dir=$(prepare_project aztec-v2)
  storage_patches=0x737901bea3eeb88459df9ef1BE8fF3Ae1B42A2ba:0x1:0x2708a627d38d74d478f645ec3b4e91afa325331acf1acebe9077891146b75e39,0x737901bea3eeb88459df9ef1BE8fF3Ae1B42A2ba:0x2:0x2694dbe3c71a25d92213422d392479e7b8ef437add81e1e17244462e6edca9b1,0x737901bea3eeb88459df9ef1BE8fF3Ae1B42A2ba:0x3:0x2d264e93dc455751a721aead9dba9ee2a9fef5460921aeede73f63f6210e6851,0x737901bea3eeb88459df9ef1BE8fF3Ae1B42A2ba:0x4:0x0000000000000000000000000000000000000000000000000000000000000000,0x737901bea3eeb88459df9ef1BE8fF3Ae1B42A2ba:0x5:0x0000000000000000000000000000000000000000000000000000000000000000
  start_fork 25295800 25295800 "" "" "$storage_patches"
  run_grade_script "$project_dir"
  echo "[smoke] aztec-v2 passed"
}

run_bunni() {
  local project_dir deployment deploy_status grader exploit inventory
  project_dir=$(prepare_project bunni-v2)
  start_fork 23273097 23273098 1756787903

  set +e
  deployment=$(cd "$project_dir" && forge create src/ExploitGrader.sol:ExploitGrader \
    --offline --broadcast --rpc-url "$rpc_url" --private-key "$private_key" \
    --value 1ether --gas-limit 100000000 --gas-price 0 --priority-gas-price 0 -vvvv 2>&1)
  deploy_status=$?
  set -e
  grader=$(printf '%s\n' "$deployment" | awk '/Deployed to:/ {print $3}' | tail -1)
  if (( deploy_status != 0 )) || [[ ! "$grader" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
    printf '%s\n' "$deployment" >&2
    echo "[smoke] bunni-v2 grader deployment failed" >&2
    return 1
  fi
  exploit=$(cast call "$grader" 'exploit()(address)' --rpc-url "$rpc_url" | awk '{print $1}')

  local holder=0x657D8BcCDD9C6e1Da8DA1e7d331CFdeA8357AdBc
  local lp=0xc92c2ba90213Fc3048A527052B0b4FeBFA716763
  inventory=$(cast call "$lp" 'balanceOf(address)(uint256)' "$holder" --rpc-url "$rpc_url" | awk '{print $1}')
  cast rpc --rpc-url "$rpc_url" anvil_impersonateAccount "$holder" >/dev/null
  cast send "$lp" 'transfer(address,uint256)' "$exploit" "$inventory" --from "$holder" --unlocked \
    --rpc-url "$rpc_url" --gas-price 0 --priority-gas-price 0 >/dev/null
  cast send "$grader" 'grade()' --rpc-url "$rpc_url" --private-key "$private_key" \
    --gas-limit 100000000 --gas-price 0 --priority-gas-price 0 >/dev/null
  if [[ "$(cast call "$grader" 'passed()(bool)' --rpc-url "$rpc_url" | awk '{print $1}')" != "true" ]]; then
    echo "[smoke] bunni-v2 grader did not pass" >&2
    return 1
  fi
  echo "[smoke] bunni-v2 passed"
}

for task_id in "${tasks[@]}"; do
  if [[ "$requested" == "all" || "$requested" == "$task_id" ]]; then
    case "$task_id" in
      notional-v1) run_notional ;;
      trusted-volumes) run_trusted ;;
      projekt-reward-vault) run_projekt ;;
      rwa-vault) run_rwa_vault ;;
      prxvt) run_prxvt ;;
      aztec-v2) run_aztec ;;
      bunni-v2) run_bunni ;;
    esac
  fi
done

echo "[smoke] requested reference controls passed"
