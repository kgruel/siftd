"""Ingestion layer for strata."""

from .discovery import discover_all
from .orchestration import IngestStats, ingest_all

__all__ = ["discover_all", "ingest_all", "IngestStats"]
