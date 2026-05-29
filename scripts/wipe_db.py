#!/usr/bin/env python3
"""Wipe the database clean and verify."""
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bot.db")
DB_PATH = os.path.abspath(DB_PATH)

# Show current state
if os.path.exists(DB_PATH):
    conn = sqlite3.connect(DB_PATH)
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    print("=== BEFORE WIPE ===")
    for (name,) in tables:
        count = conn.execute(f"SELECT COUNT(*) FROM [{name}]").fetchone()[0]
        print(f"  {name}: {count} rows")
    conn.close()
else:
    print("No database found.")

# Delete everything
for suffix in ("", "-shm", "-wal", "-journal"):
    p = DB_PATH + suffix
    if os.path.exists(p):
        os.remove(p)
        print(f"Deleted: {p}")

print("\n✅ Database wiped. Fresh start ready.")
