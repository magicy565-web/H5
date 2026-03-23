"""
scheduler.py — Python-based scheduler for the Intent Monitor.

Runs each industry on a staggered schedule to avoid rate limiting.
Includes a simple health-check HTTP endpoint on port 8081.

Default schedule:
    注塑机  08:00 / 20:00
    家纺    09:00 / 21:00
    家具    10:00 / 22:00

Usage:
    python -m monitor.scheduler
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Dict, List, Tuple

import schedule

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_DIR = os.path.join(os.path.dirname(__file__), "output", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(LOG_DIR, f"scheduler_{datetime.now():%Y%m%d}.log"),
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("monitor.scheduler")

# ---------------------------------------------------------------------------
# Schedule configuration
# ---------------------------------------------------------------------------
# Each entry: (industry, hour, minute)
DEFAULT_SCHEDULE: List[Tuple[str, str]] = [
    ("注塑机", "08:00"),
    ("注塑机", "20:00"),
    ("家纺",  "09:00"),
    ("家纺",  "21:00"),
    ("家具",  "10:00"),
    ("家具",  "22:00"),
]

HEALTH_PORT = int(os.getenv("SCHEDULER_HEALTH_PORT", "8081"))

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------
_shutdown_event = threading.Event()


def _signal_handler(signum, frame):
    signame = signal.Signals(signum).name
    logger.info("Received %s — shutting down gracefully ...", signame)
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Monitor runner (wraps the async entry point)
# ---------------------------------------------------------------------------
def run_industry(industry: str) -> None:
    """Run the monitor for a single industry (blocking call)."""
    logger.info("Scheduled run started: industry=%s", industry)
    try:
        from monitor.intent_monitor import run_monitor
        asyncio.run(run_monitor(industry))
        logger.info("Scheduled run finished: industry=%s", industry)
    except Exception:
        logger.exception("Scheduled run FAILED: industry=%s", industry)


# ---------------------------------------------------------------------------
# Health-check HTTP server
# ---------------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    """Minimal handler: GET /health -> 200 OK with JSON payload."""

    def do_GET(self):
        if self.path in ("/health", "/healthz", "/"):
            payload = (
                '{"status":"ok","scheduler":"running",'
                f'"time":"{datetime.utcnow().isoformat()}Z",'
                f'"jobs":{len(schedule.get_jobs())}}}'
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        # Suppress default stderr logging for health checks
        pass


def _start_health_server() -> HTTPServer:
    server = HTTPServer(("0.0.0.0", HEALTH_PORT), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info("Health-check endpoint listening on http://0.0.0.0:%d/health", HEALTH_PORT)
    return server


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    logger.info("=" * 60)
    logger.info("Intent Monitor Scheduler starting")
    logger.info("=" * 60)

    # Register jobs
    for industry, time_str in DEFAULT_SCHEDULE:
        schedule.every().day.at(time_str).do(run_industry, industry=industry)
        logger.info("  Registered: %s at %s", industry, time_str)

    logger.info("Total scheduled jobs: %d", len(schedule.get_jobs()))
    logger.info("")

    # Start health-check server
    health_srv = _start_health_server()

    # Main loop
    try:
        while not _shutdown_event.is_set():
            schedule.run_pending()
            # Sleep in small increments so we can react to shutdown quickly
            _shutdown_event.wait(timeout=30)
    finally:
        logger.info("Stopping health-check server ...")
        health_srv.shutdown()
        logger.info("Scheduler stopped.")


if __name__ == "__main__":
    main()
