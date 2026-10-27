#!/usr/bin/env bash
# End-to-end test: submits every benign sample through the API, waits for the
# workers to analyse them in the sandbox VMs, and checks verdicts and IOCs.
# Runs on the workstation; `make e2e` calls it.
set -euo pipefail

API="${1:?usage: e2e.sh <api url>}"
SAMPLES="$(dirname "$0")/../samples/benign"
# Retry refused connections: right after a deploy the API port can flap briefly.
CURL=(curl -fsS --retry 10 --retry-delay 2 --retry-connrefused)
declare -A expected=([hello.sh]=clean [dropper.sh]=suspicious [persist.sh]=malicious [beacon.py]=malicious)

declare -A sha
for name in "${!expected[@]}"; do
  sha[$name]="$("${CURL[@]}" -F "file=@$SAMPLES/$name" "$API/samples?force=true" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["sha256"])')"
  echo "submitted $name ${sha[$name]:0:12}"
done

fail=0
for name in "${!expected[@]}"; do
  status=""
  for _ in $(seq 90); do
    status="$("${CURL[@]}" "$API/samples/${sha[$name]}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
    [[ $status == "done" || $status == "error" ]] && break
    sleep 2
  done
  report="$("${CURL[@]}" "$API/samples/${sha[$name]}/report" || echo '{}')"
  summary="$(python3 -c '
import json, sys
r = json.loads(sys.argv[1] or "{}")
sigs = ",".join(s["id"] for s in r.get("signatures", []))
print(r.get("verdict", "-"), r.get("score", "-"), sigs or "-")' "$report")"
  read -r verdict score sigs <<< "$summary"
  if [[ $status == "done" && $verdict == "${expected[$name]}" ]]; then
    echo "PASS  $name → $verdict ($score) [$sigs]"
  else
    echo "FAIL  $name → status=$status verdict=$verdict, expected ${expected[$name]} [$sigs]"
    fail=1
  fi
done

iocs="$("${CURL[@]}" "$API/samples/${sha[beacon.py]}/report" | python3 -c 'import json,sys; print(json.load(sys.stdin)["iocs"])')"
for ioc in update-check.example-c2.net http://update-check.example-c2.net/api/checkin 203.0.113.50; do
  if grep -qF "$ioc" <<< "$iocs"; then echo "PASS  beacon IOC $ioc"; else echo "FAIL  beacon IOC $ioc"; fail=1; fi
done
exit "$fail"
