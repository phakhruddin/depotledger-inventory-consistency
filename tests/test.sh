#!/usr/bin/env bash
set -Eeuo pipefail

mkdir -p /logs/verifier

has_valid_reward() {
  [[ -s /logs/verifier/reward.json ]] \
    && jq -e '
      (.reward | type) == "number"
      and (.score | type) == "number"
      and .reward >= 0
      and .reward <= 1
    ' /logs/verifier/reward.json >/dev/null 2>&1
}

# Harbor parses every top-level value in reward.json as a numeric reward
# component, so this file stays a flat {reward, score} object. Diagnostics
# belong in report.json and summary.json.
ensure_result() {
  local rc="$1" line="$2"
  if ! has_valid_reward; then
    printf '{"trial_valid":false,"error":"verifier exited before producing a result","exit_code":%s,"line":%s}\n' \
      "$rc" "$line" >/logs/verifier/report.json
    printf '{"reward":0,"score":0}\n' >/logs/verifier/reward.json
    printf '0\n' >/logs/verifier/reward.txt
  fi
}
trap 'rc=$?; ensure_result "$rc" "$LINENO"' EXIT

rm -f /logs/verifier/reward.txt /logs/verifier/reward.json \
      /logs/verifier/report.json /logs/verifier/summary.json

export PYTHONPATH="/tests${PYTHONPATH:+:$PYTHONPATH}"
: "${DEPOTLEDGER_SUBMISSION_DIR:?DEPOTLEDGER_SUBMISSION_DIR is required}"

printf 'DepotLedger verifier: starting\n'

set +e
python -m pytest \
  -p no:cacheprovider \
  --tb=short \
  -rA \
  /tests/suite/test_declared.py \
  /tests/suite/test_live.py \
  /tests/suite/test_behavior.py \
  /tests/suite/test_lifecycle.py
pytest_rc=$?
set -e

if has_valid_reward; then
  jq -r '.reward' /logs/verifier/reward.json >/logs/verifier/reward.txt
  exit 0
fi
exit "${pytest_rc:-3}"
