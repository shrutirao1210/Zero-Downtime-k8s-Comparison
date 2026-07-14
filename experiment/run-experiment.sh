#!/usr/bin/env bash
# ============================================================
# run-experiment.sh — N runs per strategy, randomized order.
# Usage: ./experiment/run-experiment.sh [N] [rate] [duration_s] [switch_at_s]
# ============================================================
set -euo pipefail

N="${1:-10}"
RATE="${2:-5}"
DURATION="${3:-120}"
SWITCH_AT="${4:-40}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANSIBLE_DIR="${PROJECT_ROOT}/ansible"
RAW_DIR="${PROJECT_ROOT}/experiment/raw"
LOG_DIR="${PROJECT_ROOT}/experiment/logs"
mkdir -p "${RAW_DIR}" "${LOG_DIR}"

STRATEGIES=(blue-green rolling canary recreate)
SERVICES=(api-gateway catalog-service price-service inventory-service shipping-service)

# ── Build shuffled schedule ───────────────────────────────────────────────────
SCHEDULE="${LOG_DIR}/run-schedule.txt"
if [ ! -f "${SCHEDULE}" ]; then
for strat in "${STRATEGIES[@]}"; do
  for i in $(seq 1 "${N}"); do echo "${strat} ${i}" >> "${SCHEDULE}"; done
done
python3 -c "
import random,sys
lines=open('${SCHEDULE}').readlines()
random.shuffle(lines)
sys.stdout.writelines(lines)
" > "${SCHEDULE}.tmp" && mv "${SCHEDULE}.tmp" "${SCHEDULE}"
fi

TOTAL=$(wc -l < "${SCHEDULE}")
echo "============================================================"
echo "  Experiment: ${TOTAL} runs  (${N} × ${#STRATEGIES[@]} strategies)"
echo "  Rate: ${RATE} req/s  Duration: ${DURATION}s  Switch at: ${SWITCH_AT}s"
echo "============================================================"

# ── Scale a namespace and wait ────────────────────────────────────────────────
scale_and_wait() {
  local ns="$1" replicas="$2"
  for svc in "${SERVICES[@]}"; do
    kubectl scale deployment/${svc} --replicas=${replicas} -n "${ns}" 2>/dev/null || true
  done
  if [ "${replicas}" -gt 0 ]; then
    for svc in "${SERVICES[@]}"; do
      kubectl rollout status deployment/${svc} -n "${ns}" --timeout=90s 2>/dev/null || true
    done
  else
    sleep 10
  fi
}

# ── Set image tag for a namespace ─────────────────────────────────────────────
set_image_tag() {
  local ns="$1" tag="$2"
  for svc in "${SERVICES[@]}"; do
    kubectl set image deployment/${svc} \
      ${svc}=shrutimrao/${svc}:${tag} -n "${ns}" 2>/dev/null || true
  done
}

# ── Point nginx at a namespace ────────────────────────────────────────────────
point_nginx_at() {
  local ns="$1"
  cd "${ANSIBLE_DIR}"
  ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
    ansible-playbook -i inventory.ini playbooks/04-switch-traffic.yml \
    -e "switch_env=${ns}" 2>/dev/null || \
  ansible-playbook -i inventory.ini playbooks/05-rollback.yml 2>/dev/null || true
  cd "${PROJECT_ROOT}"
}

# ── Verify router healthy ─────────────────────────────────────────────────────
wait_for_router() {
  local ip; ip=$(minikube ip)
  local target="http://${ip}:30080/health"
  for i in $(seq 1 20); do
    HTTP=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "${target}" 2>/dev/null || echo "000")
    [ "${HTTP}" = "200" ] && { echo ">> Router OK"; return 0; }
    echo ">>   router attempt ${i}/20 — HTTP ${HTTP}"
    sleep 3
  done
  echo "ERROR: Router not healthy"; return 1
}

# ── Reset cluster per strategy ────────────────────────────────────────────────
reset_for_strategy() {
  local strategy="$1"
  echo ">> [reset] Preparing for: ${strategy}"

  case "${strategy}" in
    blue-green)
      # blue=v1, green=v1 (will be upgraded to v2 during switch)
      set_image_tag blue v1
      set_image_tag green v1
      scale_and_wait blue 1
      scale_and_wait green 1
      point_nginx_at blue
      ;;
    rolling)
      (cd "${ANSIBLE_DIR}" && ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
        ansible-playbook -i inventory.ini playbooks/06-setup-rolling.yml)
      point_nginx_at rolling
      ;;
    canary)
      # blue=v1 (stable), green=v2 (canary)
      set_image_tag blue v1
      set_image_tag green v2
      scale_and_wait blue 1
      scale_and_wait green 1
      point_nginx_at blue
      ;;
    recreate)
      (cd "${ANSIBLE_DIR}" && ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
        ansible-playbook -i inventory.ini playbooks/08-setup-recreate.yml)
      point_nginx_at recreate
      ;;
  esac

  echo ">> [reset] Done. Sleeping 10s..."
  sleep 10
}

# ── Main loop ─────────────────────────────────────────────────────────────────
RUN_NUM=0
while IFS=" " read -r STRATEGY RUN_ID; do
  RUN_NUM=$(( RUN_NUM + 1 ))
  
  if ls "${RAW_DIR}/${STRATEGY}_run${RUN_ID}_"*.csv 1> /dev/null 2>&1; then
    echo ">> Run ${RUN_NUM}/${TOTAL} already completed (${STRATEGY} ${RUN_ID}). Skipping."
    continue
  fi

  RUN_LOG="${LOG_DIR}/${STRATEGY}_run${RUN_ID}.log"
  echo ""
  echo "============================================================"
  echo "  RUN ${RUN_NUM}/${TOTAL}: strategy=${STRATEGY}  run_id=${RUN_ID}"
  echo "============================================================"

  reset_for_strategy "${STRATEGY}" 2>&1 | tee -a "${RUN_LOG}"
  wait_for_router 2>&1 | tee -a "${RUN_LOG}"

  "${PROJECT_ROOT}/wrk2/run-strategy-test.sh" \
    "${STRATEGY}" "${DURATION}" "${RATE}" "${SWITCH_AT}" "${RUN_ID}" \
    2>&1 | tee -a "${RUN_LOG}"

  # After blue-green switch, nginx points at green — rollback for next run
  if [ "${STRATEGY}" = "blue-green" ]; then
    echo ">> Post-run: rolling back nginx to blue..."
    (cd "${ANSIBLE_DIR}" && ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
      ansible-playbook -i inventory.ini playbooks/05-rollback.yml 2>/dev/null || true)
  fi

  echo ">> Run ${RUN_NUM}/${TOTAL} done. Sleeping 20s..."
  sleep 20
done < "${SCHEDULE}"

echo ""
echo "============================================================"
echo "  All ${TOTAL} runs complete!"
echo "  Run: python3 analysis/analyze.py"
echo "============================================================"
