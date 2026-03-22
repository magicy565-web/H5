"""
Deduplication module – filters out previously seen signals.
"""
import json
import logging
from pathlib import Path

from monitor.collectors.base import RawSignal
from monitor.config import LEADS_FILE

logger = logging.getLogger(__name__)


class Deduplicator:
    """Track content hashes to avoid processing duplicate signals."""

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._load_existing()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_existing(self) -> None:
        """Load content_hash values from the persistent leads file."""
        if not LEADS_FILE.exists():
            logger.debug("Leads file not found at %s – starting fresh.", LEADS_FILE)
            return

        try:
            data = json.loads(LEADS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                for entry in data:
                    h = entry.get("contentHash") or entry.get("content_hash")
                    if h:
                        self._seen.add(h)
            logger.info("Loaded %d known hashes from %s.", len(self._seen), LEADS_FILE)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read leads file: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def filter_new(self, signals: list[RawSignal]) -> list[RawSignal]:
        """Return only signals whose content_hash has not been seen."""
        new = [s for s in signals if s.content_hash not in self._seen]
        logger.info(
            "Dedup: %d/%d signals are new.", len(new), len(signals),
        )
        return new

    def mark_seen(self, signals: list[RawSignal]) -> None:
        """Add signal hashes to the known set."""
        for s in signals:
            self._seen.add(s.content_hash)
