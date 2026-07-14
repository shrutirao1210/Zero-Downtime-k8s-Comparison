#!/usr/bin/env bash
# ============================================================
# run-strategy-test.sh — Unified test runner for all 4 strategies.
#
# Works with standard wrk (not wrk2). No -R flag used.
# Uses 2 threads, 4 connections — conservative enough for VMware minikube.
# ============================================================
set -euo pipefail

STRATEGY="${1:-blue-green}"
DURATION="${2:-120}"
RATE="${3:-20}"   # kept for compatibility, not used with plain wrk
SWITCH_AT="${4:-40}"
RUN_ID="${5:-1}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANSIBLE_DIR="${PROJECT_ROOT}/ansible"
RESULTS_DIR="${PROJECT_ROOT}/experiment/raw"
mkdir -p "${RESULTS_DIR}"

MINIKUBE_IP=$(minikube ip)
TARGET="http://${MINIKUBE_IP}:30080/health"
OUT_PREFIX="${RESULTS_DIR}/${STRATEGY}_run${RUN_ID}"
SERVICES=(api-gateway catalog-service price-service inventory-service shipping-service)
REPLICA_COUNT=1

case "${STRATEGY}" in
  blue-green)
    IDLE_NS="green"
    ACTIVE_NS="blue"
    SWITCH_CMD="ansible-playbook -i ${ANSIBLE_DIR}/inventory.ini \
      ${ANSIBLE_DIR}/playbooks/04-switch-traffic.yml"
    ;;
  rolling)
    IDLE_NS=""
    ACTIVE_NS="rolling"
    SWITCH_CMD="kubectl set image deployment/api-gateway api-gateway=shrutimrao/api-gateway:v2 -n rolling && \
      kubectl set image deployment/catalog-service catalog-service=shrutimrao/catalog-service:v2 -n rolling && \
      kubectl set image deployment/price-service price-service=shrutimrao/price-service:v2 -n rolling && \
      kubectl set image deployment/inventory-service inventory-service=shrutimrao/inventory-service:v2 -n rolling && \
      kubectl set image deployment/shipping-service shipping-service=shrutimrao/shipping-service:v2 -n rolling"
    ;;
  canary)
    IDLE_NS=""
    ACTIVE_NS="blue"
    SWITCH_CMD="ansible-playbook -i ${ANSIBLE_DIR}/inventory.ini \
      ${ANSIBLE_DIR}/playbooks/09-trigger-canary.yml"
    ;;
  recreate)
    IDLE_NS=""
    ACTIVE_NS="recreate"
    SWITCH_CMD="kubectl set image deployment/api-gateway api-gateway=shrutimrao/api-gateway:v2 -n recreate && \
      kubectl set image deployment/catalog-service catalog-service=shrutimrao/catalog-service:v2 -n recreate && \
      kubectl set image deployment/price-service price-service=shrutimrao/price-service:v2 -n recreate && \
      kubectl set image deployment/inventory-service inventory-service=shrutimrao/inventory-service:v2 -n recreate && \
      kubectl set image deployment/shipping-service shipping-service=shrutimrao/shipping-service:v2 -n recreate"
    ;;
  *)
    echo "ERROR: Unknown strategy '${STRATEGY}'"
    exit 1
    ;;
esac

echo "============================================================"
echo "  Strategy : ${STRATEGY}  (run ${RUN_ID})"
echo "  Duration : ${DURATION}s  |  Switch at: ${SWITCH_AT}s"
echo "  Active NS: ${ACTIVE_NS}  |  Idle NS: ${IDLE_NS:-none}"
echo "============================================================"

# ── STEP 1: Ensure ACTIVE namespace is fully running ─────────────────────────
echo ">> Ensuring '${ACTIVE_NS}' pods are Ready..."
for svc in "${SERVICES[@]}"; do
  kubectl rollout status deployment/${svc} -n "${ACTIVE_NS}" --timeout=90s 2>/dev/null || true
done

# ── STEP 2: Scale idle namespace to 0 and wait until gone ────────────────────
if [ -n "${IDLE_NS}" ]; then
  echo ">> Scaling '${IDLE_NS}' to 0 replicas..."
  for svc in "${SERVICES[@]}"; do
    kubectl scale deployment/${svc} --replicas=0 -n "${IDLE_NS}" 2>/dev/null || true
  done

  echo ">> Waiting for '${IDLE_NS}' pods to terminate (max 90s)..."
  for i in $(seq 1 30); do
    COUNT=$(kubectl get pods -n "${IDLE_NS}" \
      -o jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' \
      2>/dev/null | grep -c "." || true)
    [ -z "${COUNT}" ] && COUNT=0
    [ "${COUNT}" -eq 0 ] && { echo ">> '${IDLE_NS}' is empty after $((i*3))s"; break; }
    [ "${i}" -eq 30 ] && echo ">> WARNING: pods still present after 90s, continuing anyway"
    echo ">>   ${COUNT} pod(s) still terminating..."
    sleep 3
  done
  echo ">> Sleeping 8s for CPU to settle..."
  sleep 8
fi

# ── STEP 3: Verify router returns 200 ────────────────────────────────────────
echo ">> Verifying nginx router..."
ROUTER_OK=0
for i in $(seq 1 20); do
  HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${TARGET}" 2>/dev/null || echo "000")
  if [ "${HTTP}" = "200" ]; then
    echo ">> Router OK (HTTP 200)"
    ROUTER_OK=1
    break
  fi
  echo ">>   attempt ${i}/20 — HTTP ${HTTP}, waiting 3s..."
  sleep 3
done
if [ "${ROUTER_OK}" -eq 0 ]; then
  echo "ERROR: Router not responding with HTTP 200 after 60s"
  echo "Run: cd ansible && ansible-playbook -i inventory.ini playbooks/05-rollback.yml"
  exit 1
fi

# ── STEP 4: Pre-restore idle namespace NOW before test starts ─────────────────
# KEY CHANGE: restore idle namespace BEFORE starting wrk, not during the test.
# This means both namespaces are running during the test — same as original design.
# But we scale down FIRST to free CPU, then restore, then start wrk.
# The switch fires at SWITCH_AT seconds into the test — by then green is warm.
if [ -n "${IDLE_NS}" ]; then
  echo ">> Pre-restoring '${IDLE_NS}' to ${REPLICA_COUNT} replicas BEFORE starting wrk..."
  for svc in "${SERVICES[@]}"; do
    kubectl scale deployment/${svc} --replicas=${REPLICA_COUNT} \
      -n "${IDLE_NS}" 2>/dev/null || true
  done
  echo ">> Waiting for '${IDLE_NS}' to be Ready..."
  for svc in "${SERVICES[@]}"; do
    kubectl rollout status deployment/${svc} \
      -n "${IDLE_NS}" --timeout=90s 2>/dev/null || true
  done
  echo ">> '${IDLE_NS}' is Ready. Starting test in 5s..."
  sleep 5
fi

# ── STEP 5: Clean old result files ───────────────────────────────────────────
rm -f /tmp/wrk2-results-*.csv /tmp/wrk2-summary.txt

# ── STEP 6: Start wrk (plain wrk, no -R flag) ────────────────────────────────
echo ">> Starting wrk: ${DURATION}s, 2 threads, 4 connections (conservative for VMware)"
wrk \
  -t2 -c4 \
  -d"${DURATION}s" \
  -R"${RATE}" \
  -T120s \
  --latency \
  -s "${PROJECT_ROOT}/wrk2/downtime-test.lua" \
  "${TARGET}" \
  > "${OUT_PREFIX}_wrk_stdout.txt" 2>&1 &
WRK_PID=$!
echo ">> wrk PID=${WRK_PID}"

# ── STEP 7: Sleep until switch time ──────────────────────────────────────────
echo ">> Sleeping ${SWITCH_AT}s until switch..."
sleep "${SWITCH_AT}"

# ── STEP 8: Trigger switch ────────────────────────────────────────────────────
SWITCH_TIME=$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)
python3 -c "import time; print(int(time.time()*1_000_000))" \
  > "${OUT_PREFIX}_switch_ts_us.txt"
DEPLOY_START=$(date +%s%3N)

echo ""
echo ">> ============================================================"
echo ">> SWITCH TRIGGERED: ${STRATEGY} at $(date --utc +%Y-%m-%dT%H:%M:%S.%3NZ)"
echo ">> ============================================================"

(
  cd "${ANSIBLE_DIR}"
  ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
  eval "${SWITCH_CMD}" 2>&1 | tee "${OUT_PREFIX}_switch_log.txt"
)

DEPLOY_END=$(date +%s%3N)
echo $(( DEPLOY_END - DEPLOY_START )) > "${OUT_PREFIX}_switch_duration_ms.txt"
echo ">> Switch done in $(( DEPLOY_END - DEPLOY_START ))ms"

# ── STEP 9: Wait for wrk to finish ───────────────────────────────────────────
echo ">> Waiting for wrk to finish..."
wait "${WRK_PID}" || true

# ── STEP 10: Collect results ──────────────────────────────────────────────────
CSV_COUNT=0
for f in /tmp/wrk2-results-*.csv; do
  [ -f "${f}" ] || continue
  cp "${f}" "${OUT_PREFIX}_responses_${CSV_COUNT}.csv"
  CSV_COUNT=$(( CSV_COUNT + 1 ))
done
[ -f /tmp/wrk2-summary.txt ] && cp /tmp/wrk2-summary.txt "${OUT_PREFIX}_summary.txt"

echo ""
echo ">> wrk output:"
cat "${OUT_PREFIX}_wrk_stdout.txt"
echo ""
echo "============================================================"
echo "  Results: ${OUT_PREFIX}_*.{csv,txt}  (${CSV_COUNT} CSV files)"
echo "============================================================"
