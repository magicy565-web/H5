#!/usr/bin/env bash
# run_monitor.sh — Run the Intent Monitor for a specific industry or all
# Usage:
#   ./run_monitor.sh              # defaults to "all"
#   ./run_monitor.sh 注塑机       # specific industry
#   ./run_monitor.sh all          # all industries

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
LOG_DIR="${SCRIPT_DIR}/monitor/output/logs"
INDUSTRY="${1:-all}"
DATE_STR="$(date +%Y%m%d)"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
LOG_FILE="${LOG_DIR}/monitor_${INDUSTRY}_${DATE_STR}.log"

# Ensure log directory exists
mkdir -p "$LOG_DIR"

# ── Activate virtualenv ───────────────────────────────────────────────────
if [ ! -d "$VENV_DIR" ]; then
    echo "[ERROR] Virtual environment not found at ${VENV_DIR}." >&2
    echo "        Run deploy.sh first: bash deploy.sh" >&2
    exit 1
fi
source "${VENV_DIR}/bin/activate"

# ── Load .env ─────────────────────────────────────────────────────────────
ENV_FILE="${SCRIPT_DIR}/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
else
    echo "[WARN] No .env file found at ${ENV_FILE}. Using defaults." >&2
fi

# ── Run the monitor ──────────────────────────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting monitor for industry: ${INDUSTRY}" | tee -a "$LOG_FILE"

cd "$SCRIPT_DIR"

if python -m monitor.intent_monitor --industry "$INDUSTRY" 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Monitor completed successfully." | tee -a "$LOG_FILE"
    exit 0
else
    EXIT_CODE=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Monitor exited with code ${EXIT_CODE}." | tee -a "$LOG_FILE"
    exit "$EXIT_CODE"
fi
