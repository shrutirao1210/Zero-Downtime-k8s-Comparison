#!/usr/bin/env bash
# Same as run-downtime-test.sh but triggers rollback (green → blue) mid-test.
set -euo pipefail

DURATION="${1:-120}"
RATE="${2:-100}"
SWITCH_AT="${3:-40}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MINIKUBE_IP=$(minikube ip)
TARGET="http://${MINIKUBE_IP}:30080/health"

echo ">> [PRE-CHECK] Verifying nginx router is UP..."
for i in $(seq 1 30); do
  STATUS=$(curl -s -o /dev/null -w "%{http_code}" "${TARGET}" 2>/dev/null || echo "000")
  if [ "$STATUS" = "200" ]; then
    echo ">> Router is responding. Starting rollback test."
    break
  fi
  [ "$i" -eq 30 ] && { echo "ERROR: Router not responding."; exit 1; }
  echo ">> Waiting... (attempt $i/30)"
  sleep 1
done

echo ">> Waiting for blue api-gateway to be Ready..."
kubectl wait deployment/api-gateway -n blue --for=condition=Available --timeout=120s

echo ">> Cleaning up old result files"
rm -f /tmp/wrk2-results-*.csv /tmp/wrk2-summary.txt /tmp/wrk2-stdout.txt

echo ">> Starting wrk2 ROLLBACK test: ${DURATION}s @ ${RATE} req/s"
wrk -t2 -c20 -d"${DURATION}s" -R"${RATE}" \
    --latency \
    -s "${PROJECT_ROOT}/wrk2/downtime-test.lua" \
    "${TARGET}" \
    > /tmp/wrk2-stdout.txt 2>&1 &

WRK_PID=$!
echo ">> Sleeping ${SWITCH_AT}s before triggering ROLLBACK..."
sleep "${SWITCH_AT}"

echo ">> TRIGGERING ROLLBACK NOW: $(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)"
(
  cd "${PROJECT_ROOT}/ansible"
  ANSIBLE_CONFIG="${PROJECT_ROOT}/ansible/ansible.cfg" \
  ansible-playbook -i inventory.ini playbooks/05-rollback.yml
)

echo ">> Waiting for wrk2 to finish..."
wait "${WRK_PID}"
cat /tmp/wrk2-stdout.txt
python3 "${PROJECT_ROOT}/wrk2/parse_results.py" --rate "${RATE}"
