#!/usr/bin/env bash
# Single blue-green downtime test. For multi-run experiment, use:
#   experiment/run-experiment.sh
#
# Usage: ./run-downtime-test.sh [duration_s] [rate] [switch_at_s]

set -euo pipefail
DURATION="${1:-120}"
RATE="${2:-50}"
SWITCH_AT="${3:-40}"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

exec "${PROJECT_ROOT}/wrk2/run-strategy-test.sh" \
  blue-green "${DURATION}" "${RATE}" "${SWITCH_AT}" 1
