"""
scripts/generate_migration.py
──────────────────────────────
Convenience script to auto-generate an Alembic migration from model changes.
Usage:  python scripts/generate_migration.py "describe your changes"
"""
import subprocess
import sys

message = " ".join(sys.argv[1:]) or "auto_migration"
subprocess.run(["alembic", "revision", "--autogenerate", "-m", message], check=True)
print(f"Migration generated: {message}")
print("Review migrations/versions/ then run: alembic upgrade head")
