"""
JARVIS Integrations
===================
External system integrations.

Current:
  - Fyers data pipeline integration (data freshness, manifest)

Planned:
  - Dashboard API (Phase 2)
  - VPS SSH (Phase 5)
  - Email processing (Phase 6)
  - Google Drive (Phase 6)
"""

from __future__ import annotations

from typing import Any, Optional

from ..logging_config import get_logger

log = get_logger("integrations")


async def get_data_status() -> dict[str, Any]:
    """
    Check Fyers data pipeline status.

    Returns dict with latest_download, symbols_count, status.
    """
    try:
        from tracker.manifest import get_manifest
        manifest = get_manifest()
        return {
            "status": "ok",
            "latest_download": manifest.get("last_updated", "unknown"),
            "symbols_count": len(manifest.get("symbols", [])),
            "manifest": manifest,
        }
    except ImportError:
        return {
            "status": "not_available",
            "error": "tracker module not accessible",
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
        }
