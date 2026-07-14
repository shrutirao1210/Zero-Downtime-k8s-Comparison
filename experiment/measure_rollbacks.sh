#!/usr/bin/env bash
# ============================================================
# measure_rollbacks.sh — Rapidly measures empirical rollback times
# without running the 120s load generator.
# ============================================================
set -euo pipefail

N="${1:-10}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ANSIBLE_DIR="${PROJECT_ROOT}/ansible"
RAW_DIR="${PROJECT_ROOT}/experiment/raw"
mkdir -p "${RAW_DIR}"

STRATEGIES=(blue-green canary rolling recreate)
SERVICES=(api-gateway catalog-service price-service inventory-service shipping-service)

echo "============================================================"
echo "  Rapid Rollback Measurement: ${N} runs per strategy"
echo "============================================================"

# ── Helpers ────────────────────────────────────────────────────────
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

set_image_tag() {
  local ns="$1" tag="$2"
  for svc in "${SERVICES[@]}"; do
    kubectl set image deployment/${svc} \
      ${svc}=shrutimrao/${svc}:${tag} -n "${ns}" 2>/dev/null || true
  done
}

point_nginx_at() {
  local ns="$1"
  (cd "${ANSIBLE_DIR}" && ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
    ansible-playbook -i inventory.ini playbooks/04-switch-traffic.yml -e "switch_env=${ns}"  || true)
}

# ── Main Loop ──────────────────────────────────────────────────────
for strategy in "${STRATEGIES[@]}"; do
  echo ""
  echo ">> Preparing baseline state for: ${strategy}"

  case "${strategy}" in
    blue-green|canary)
      set_image_tag blue v1
      set_image_tag green v2
      scale_and_wait blue 1
      scale_and_wait green 1
      point_nginx_at green
      ;;
    rolling)
      (cd "${ANSIBLE_DIR}" && ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
        ansible-playbook -i inventory.ini playbooks/06-setup-rolling.yml )
      # Trigger an upgrade to v2 so we can undo it
      set_image_tag rolling v2
      for svc in "${SERVICES[@]}"; do
        kubectl rollout status deployment/${svc} -n rolling --timeout=90s 2>/dev/null || true
      done
      point_nginx_at rolling
      ;;
    recreate)
      (cd "${ANSIBLE_DIR}" && ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
        ansible-playbook -i inventory.ini playbooks/08-setup-recreate.yml )
      # Trigger an upgrade to v2 so we can undo it
      set_image_tag recreate v2
      for svc in "${SERVICES[@]}"; do
        kubectl rollout status deployment/${svc} -n recreate --timeout=90s 2>/dev/null || true
      done
      point_nginx_at recreate
      ;;
  esac

  echo ">> Measuring ${N} rollbacks for ${strategy}..."
  for RUN_ID in $(seq 1 "${N}"); do
    OUT_FILE="${RAW_DIR}/${strategy}_run${RUN_ID}_rollback_duration_ms.txt"
    echo -n "   Run ${RUN_ID}/${N}: "

    if [ "${strategy}" = "blue-green" ] || [ "${strategy}" = "canary" ]; then
      T1=$(date +%s%3N)
      (cd "${ANSIBLE_DIR}" && ANSIBLE_CONFIG="${ANSIBLE_DIR}/ansible.cfg" \
        ansible-playbook -i inventory.ini playbooks/05-rollback.yml )
      T2=$(date +%s%3N)
      echo "$(( T2 - T1 ))" > "${OUT_FILE}"
      echo "$(( T2 - T1 )) ms"
      # Swap back to green for the next iteration
      point_nginx_at green

    elif [ "${strategy}" = "rolling" ] || [ "${strategy}" = "recreate" ]; then
      T1=$(date +%s%3N)
      for svc in "${SERVICES[@]}"; do
        kubectl rollout undo deployment/${svc} -n "${strategy}" 
      done
      for svc in "${SERVICES[@]}"; do
        kubectl rollout status deployment/${svc} -n "${strategy}" --timeout=90s  || true
      done
      T2=$(date +%s%3N)
      echo "$(( T2 - T1 ))" > "${OUT_FILE}"
      echo "$(( T2 - T1 )) ms"
      
      # Redo the upgrade to v2 for the next iteration
      for svc in "${SERVICES[@]}"; do
        kubectl rollout undo deployment/${svc} -n "${strategy}" 
      done
      for svc in "${SERVICES[@]}"; do
        kubectl rollout status deployment/${svc} -n "${strategy}" --timeout=90s  || true
      done
    fi
  done
done

echo "============================================================"
echo "  Measurement complete!"
echo "============================================================"
