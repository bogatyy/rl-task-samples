#!/usr/bin/env bash
set -euo pipefail

repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
audit_root=$(mktemp -d /tmp/rl-adversarial.XXXXXX)
cleanup() {
  case "$audit_root" in /tmp/rl-adversarial.*) rm -rf -- "$audit_root" ;; esac
}
trap cleanup EXIT

expect_rejection() {
  local task_id=$1 fixture=$2 description=$3
  local log="$audit_root/${task_id}-${fixture}.log"
  if SMOKE_EXPLOIT_PATH="$repo_root/scripts/fixtures/$fixture" \
      "$repo_root/scripts/smoke_test.sh" "$task_id" >"$log" 2>&1; then
    echo "[adversarial] accepted: $description" >&2
    return 1
  fi
  echo "[adversarial] rejected: $description"
}

for task_id in notional-v1 trusted-volumes projekt-reward-vault rwa-vault prxvt aztec-v2 bunni-v2; do
  expect_rejection "$task_id" NoopExploit.sol "$task_id no-op submission"
done

expect_rejection prxvt PrxvtRoundTripExploit.sol \
  "ordinary constructor stake followed by ordinary withdrawal"

echo "[adversarial] grader regression controls passed"
