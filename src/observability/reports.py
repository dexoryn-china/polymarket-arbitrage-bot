"""Run reports — generate JSON summaries of bot runs."""

from __future__ import annotations

import json
import datetime as dt
from pathlib import Path
from typing import Any

from src.observability.logger import get_logger
from src.observability.metrics import metrics

log = get_logger(__name__)


def generate_run_report(
    run_id: str,
    forecasts: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    output_dir: str = "reports/",
) -> Path:
    """Generate a JSON run report and write to disk."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    report = {
        "run_id": run_id,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "summary": {
            "markets_scanned": len(forecasts),
            "trades_executed": len(trades),
            "trades_skipped": len(forecasts) - len(trades),
        },
        "metrics": metrics.snapshot(),
        "forecasts": forecasts,
        "trades": trades,
    }

    filepath = out / f"run_{run_id}.json"
    with open(filepath, "w") as f:
        json.dump(report, f, indent=2, default=str)

    log.info("report.generated", path=str(filepath), markets=len(forecasts))
    return filepath
