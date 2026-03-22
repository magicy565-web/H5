"""
Base collector and signal data classes.
"""
from dataclasses import dataclass, field
from typing import Optional
import hashlib


@dataclass
class RawSignal:
    """A raw signal collected from any source."""
    source: str              # e.g. "google_search", "go4worldbusiness"
    url: str                 # source URL
    title: str               # signal title/headline
    text: str                # full text content
    buyer_name: str = ""     # if extractable
    buyer_country: str = ""  # if extractable
    contact_info: str = ""   # any contact found
    collected_at: str = ""   # ISO timestamp
    extra: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        """Hash of URL + title for dedup."""
        raw = f"{self.url}|{self.title}".lower().strip()
        return hashlib.md5(raw.encode()).hexdigest()[:12]


class BaseCollector:
    """Abstract base for all collectors."""
    name: str = "base"

    async def collect(self) -> list[RawSignal]:
        raise NotImplementedError
