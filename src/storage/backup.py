"""Database backup utility for SQLite.

Provides both CLI-callable and programmatic backup of the bot database.
Uses SQLite's built-in backup API for consistency.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path

from src.observability.logger import get_logger

log = get_logger(__name__)


def backup_database(
    source_path: str = "data/bot.db",
    backup_dir: str = "data/backups",
    max_backups: int = 10,
) -> str:
    """Create a timestamped backup of the SQLite database.

    Uses SQLite's online backup API for a safe, consistent copy even
    while the engine is running (WAL mode).

    Args:
        source_path: Path to the live database.
        backup_dir: Directory to store backups.
        max_backups: Maximum number of backup files to keep.

    Returns:
        Path to the new backup file.
    """
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"Source database not found: {source_path}")

    dest_dir = Path(backup_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    dest_path = dest_dir / f"bot_{timestamp}.db"

    # Use SQLite backup API (safe for WAL mode)
    src_conn = sqlite3.connect(str(src))
    dst_conn = sqlite3.connect(str(dest_path))
    try:
        src_conn.backup(dst_conn)
        log.info("backup.created", path=str(dest_path), size_mb=round(dest_path.stat().st_size / 1024 / 1024, 2))
    finally:
        dst_conn.close()
        src_conn.close()

    # Prune old backups
    backups = sorted(dest_dir.glob("bot_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[max_backups:]:
        old.unlink()
        log.info("backup.pruned", path=str(old))

    return str(dest_path)
