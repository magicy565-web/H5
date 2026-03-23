FROM python:3.11-slim

# System deps for cron
RUN apt-get update && \
    apt-get install -y --no-install-recommends cron && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace/H5

# Copy requirements first for layer caching
COPY monitor/requirements.txt /workspace/H5/monitor/requirements.txt
COPY server/requirements.txt  /workspace/H5/server/requirements.txt
RUN pip install --no-cache-dir \
    -r monitor/requirements.txt \
    -r server/requirements.txt

# Copy the full project
COPY . /workspace/H5/

# Create necessary directories
RUN mkdir -p /workspace/H5/db \
             /workspace/H5/monitor/output/logs

# Make scripts executable
RUN chmod +x /workspace/H5/deploy.sh /workspace/H5/run_monitor.sh

# Environment variables (override via docker-compose or docker run)
ENV DASHSCOPE_API_KEY="" \
    LLM_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1" \
    LLM_MODEL="qwen-plus" \
    APIFY_API_TOKEN="" \
    WECOM_CORP_ID="" \
    WECOM_AGENT_ID="1000002" \
    WECOM_SECRET="" \
    WECOM_WEBHOOK_URL="" \
    SERVERCHAN_KEY="" \
    PYTHONUNBUFFERED=1

# Set up cron schedule: run all industries at 08:00 and 20:00 (UTC)
RUN echo '0 8 * * * cd /workspace/H5 && /usr/local/bin/python -m monitor.intent_monitor --industry all >> /workspace/H5/monitor/output/logs/cron.log 2>&1' > /etc/cron.d/monitor-cron && \
    echo '0 20 * * * cd /workspace/H5 && /usr/local/bin/python -m monitor.intent_monitor --industry all >> /workspace/H5/monitor/output/logs/cron.log 2>&1' >> /etc/cron.d/monitor-cron && \
    echo '' >> /etc/cron.d/monitor-cron && \
    chmod 0644 /etc/cron.d/monitor-cron && \
    crontab /etc/cron.d/monitor-cron

# Entrypoint: pass env vars to cron, then start cron daemon in foreground
COPY <<'ENTRY' /entrypoint.sh
#!/usr/bin/env bash
set -e

# Export all env vars so cron jobs can see them
printenv | grep -v "no_proxy" >> /etc/environment

echo "[entrypoint] Starting cron daemon ..."
exec cron -f
ENTRY
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
