#!/usr/bin/env bash
# Submits a harmless sample to the API and checks each step of the intake:
# accepted and queued, a duplicate returns the existing record, the job is
# waiting in RabbitMQ and the file is in object storage.
# Runs on the workstation; `make api-test` calls it.
set -euo pipefail

API="${1:?usage: api-test.sh <api url>}"
NS=detonator
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# A unique, harmless script, so each run submits a new sample.
printf '#!/bin/sh\necho "api-test %s"\n' "$(date +%s%N)" > "$tmp/sample.sh"
sha="$(sha256sum "$tmp/sample.sh" | cut -d' ' -f1)"

fail=0
check() { if eval "$2"; then echo "PASS  $1"; else echo "FAIL  $1"; fail=1; fi; }

code="$(curl -s -o "$tmp/first.json" -w '%{http_code}' -F "file=@$tmp/sample.sh" "$API/samples")"
check "new sample accepted (202) and queued" \
  "[[ $code == 202 ]] && grep -q '\"status\":\"queued\"' $tmp/first.json"

code="$(curl -s -o "$tmp/second.json" -w '%{http_code}' -F "file=@$tmp/sample.sh" "$API/samples")"
check "duplicate returns the existing record (200)" "[[ $code == 200 ]]"

check "status lookup by SHA-256" "curl -fs $API/samples/$sha | grep -q '\"sha256\":\"$sha\"'"

# The job is either still in the queue or already taken by a worker. RabbitMQ
# refreshes queue statistics every few seconds, so poll briefly.
for _ in $(seq 10); do
  depth="$(kubectl -n $NS exec rabbitmq-0 -- rabbitmqctl -q list_queues name messages \
    | awk '$1 == "analysis.jobs" {print $2}')"
  state="$(curl -fs "$API/samples/$sha" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
  [[ ${depth:-0} -ge 1 || $state != queued ]] && break
  sleep 2
done
check "job delivered (queue depth ${depth:-0}, status $state)" "[[ ${depth:-0} -ge 1 || $state != queued ]]"

# Ask the API pod (it has the S3 client and credentials) for the stored object.
size="$(kubectl -n $NS exec deploy/api -- python -c "
import app
print(app.s3.head_object(Bucket=app.BUCKET, Key='samples/$sha')['ContentLength'])")"
check "sample stored in object storage under its hash ($size bytes)" \
  "[[ $size == $(stat -c %s "$tmp/sample.sh") ]]"

exit "$fail"
