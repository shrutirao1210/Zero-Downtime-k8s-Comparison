#!/usr/bin/env bash
# Captures Blue environment baseline performance (no switch, stable traffic).
# Run this BEFORE running the downtime test to establish your baseline.
set -euo pipefail

DURATION="${1:-60}"
RATE="${2:-100}"

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

MINIKUBE_IP=$(minikube ip)
TARGET="http://${MINIKUBE_IP}:30080/health"

echo ">> Cleaning up old result files"
rm -f /tmp/wrk2-results-*.csv /tmp/wrk2-summary.txt /tmp/wrk2-stdout.txt

echo ">> Target: ${TARGET}"
echo ">> Starting BASELINE test: ${DURATION}s @ ${RATE} req/s (no switch)"

wrk -t2 -c20 -d"${DURATION}s" -R"${RATE}" \
    --latency \
    -s "${PROJECT_ROOT}/wrk2/downtime-test.lua" \
    "${TARGET}" \
    > /tmp/wrk2-stdout.txt 2>&1

echo ">> Baseline raw output:"
cat /tmp/wrk2-stdout.txt

echo ">> Parsing baseline results..."
python3 "${PROJECT_ROOT}/wrk2/parse_results.py" --rate "${RATE}"

echo ""
echo "Baseline complete — no switch was triggered. All requests should PASS."
