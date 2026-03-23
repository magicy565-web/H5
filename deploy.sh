#!/usr/bin/env bash
# deploy.sh — Deploy the Intent Monitor system
# Usage: bash deploy.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
MONITOR_DIR="${SCRIPT_DIR}/monitor"
ENV_FILE="${SCRIPT_DIR}/.env"
ENV_EXAMPLE="${SCRIPT_DIR}/.env.example"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── 1. Check Python 3.8+ ─────────────────────────────────────────────────
info "Checking Python version ..."
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$("$candidate" -c 'import sys; print(sys.version_info.major)')
        minor=$("$candidate" -c 'import sys; print(sys.version_info.minor)')
        if [ "$major" -ge 3 ] && [ "$minor" -ge 8 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    error "Python 3.8+ is required but not found. Please install Python 3.8 or later."
fi
info "Found $PYTHON ($version)"

# ── 2. Create virtualenv ─────────────────────────────────────────────────
if [ -d "$VENV_DIR" ]; then
    warn "Virtual environment already exists at ${VENV_DIR}. Skipping creation."
else
    info "Creating virtual environment at ${VENV_DIR} ..."
    "$PYTHON" -m venv "$VENV_DIR"
fi

# Activate
source "${VENV_DIR}/bin/activate"
info "Activated virtualenv: $(which python)"

# ── 3. Install requirements ──────────────────────────────────────────────
info "Installing monitor requirements ..."
pip install --upgrade pip -q
pip install -r "${MONITOR_DIR}/requirements.txt" -q

if [ -f "${SCRIPT_DIR}/server/requirements.txt" ]; then
    info "Installing server requirements ..."
    pip install -r "${SCRIPT_DIR}/server/requirements.txt" -q
fi

info "All dependencies installed."

# ── 4. Create .env from example ──────────────────────────────────────────
if [ ! -f "$ENV_EXAMPLE" ]; then
    info "Creating .env.example ..."
    cat > "$ENV_EXAMPLE" <<'ENVEOF'
# Intent Monitor Environment Configuration
# Copy this file to .env and fill in your values.

# DashScope / LLM API
DASHSCOPE_API_KEY=your_dashscope_api_key_here
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_MODEL=qwen-plus

# Apify (for premium collectors)
APIFY_API_TOKEN=your_apify_token_here

# WeCom push notifications
WECOM_CORP_ID=
WECOM_AGENT_ID=1000002
WECOM_SECRET=

# WeCom group bot webhook (optional)
WECOM_WEBHOOK_URL=

# ServerChan push key (optional)
SERVERCHAN_KEY=
ENVEOF
fi

if [ -f "$ENV_FILE" ]; then
    warn ".env file already exists. Skipping copy."
else
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    info "Created .env from .env.example"
fi

# ── 5. Create output directories ─────────────────────────────────────────
mkdir -p "${MONITOR_DIR}/output/logs"
mkdir -p "${SCRIPT_DIR}/db"
info "Output and log directories ready."

# ── 6. Set up cron jobs ──────────────────────────────────────────────────
echo ""
info "Cron job setup"
echo "  The monitor can be scheduled to run automatically."
echo "  Default schedule: twice daily at 08:00 and 20:00."
echo ""
read -rp "  Set up cron jobs? [y/N]: " setup_cron

if [[ "${setup_cron,,}" == "y" ]]; then
    RUNNER="${SCRIPT_DIR}/run_monitor.sh"

    # Build cron lines
    CRON_08="0 8 * * * ${RUNNER} all >> ${MONITOR_DIR}/output/logs/cron_08.log 2>&1"
    CRON_20="0 20 * * * ${RUNNER} all >> ${MONITOR_DIR}/output/logs/cron_20.log 2>&1"

    # Merge with existing crontab (avoid duplicates)
    EXISTING_CRON=$(crontab -l 2>/dev/null || true)
    NEW_CRON="$EXISTING_CRON"

    if echo "$EXISTING_CRON" | grep -qF "$RUNNER"; then
        warn "Cron entries for run_monitor.sh already exist. Skipping."
    else
        NEW_CRON="${EXISTING_CRON}
# Intent Monitor — twice daily
${CRON_08}
${CRON_20}"
        echo "$NEW_CRON" | crontab -
        info "Cron jobs installed:"
        echo "    ${CRON_08}"
        echo "    ${CRON_20}"
    fi
else
    info "Skipping cron setup. You can run manually with: ./run_monitor.sh [industry|all]"
fi

# ── 7. Done ───────────────────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "  Deployment complete!"
echo "=========================================="
echo ""
echo "  Next steps:"
echo "    1. Edit .env with your API keys:"
echo "       vi ${ENV_FILE}"
echo ""
echo "    2. Test a run:"
echo "       ./run_monitor.sh 注塑机"
echo ""
echo "    3. Run all industries:"
echo "       ./run_monitor.sh all"
echo ""
echo "    4. Or use the Python scheduler:"
echo "       source .venv/bin/activate"
echo "       python -m monitor.scheduler"
echo ""
echo "=========================================="
