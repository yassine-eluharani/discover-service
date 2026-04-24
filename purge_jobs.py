#!/usr/bin/env python3
"""One-shot script: purge jobs + discovery_runs, keep users intact."""

from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from db import get_connection, init_db

conn = get_connection()

jobs_count = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
runs_count = conn.execute("SELECT COUNT(*) FROM discovery_runs").fetchone()[0]
users_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

print(f"Before: {jobs_count} jobs, {runs_count} discovery_runs, {users_count} users (kept)")

conn.execute("DELETE FROM jobs")
conn.execute("DELETE FROM discovery_runs")
conn.commit()

print("Done. All jobs and discovery_runs deleted. Users untouched.")
