"""Database backup / restore.

    python -m trinity.scripts.backup                 # create a backup now
    python -m trinity.scripts.backup --list
    python -m trinity.scripts.backup --restore FILE  # engine must be stopped
"""

from __future__ import annotations

import argparse
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from trinity.database.repository import Repository  # noqa: E402


def create_backup(repo: Repository, backup_dir: Path, keep: int = 14) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    dest = repo.backup_to(backup_dir / f"trinity-{stamp}.db")
    backups = sorted(backup_dir.glob("trinity-*.db"))
    for old in backups[:-keep]:
        old.unlink(missing_ok=True)
    return dest


def restore_backup(src: Path, db_path: Path) -> Path:
    check = Repository(src, read_only=True)
    try:
        if not check.integrity_ok():
            raise SystemExit(f"backup {src} failed integrity check")
    finally:
        check.close()
    if db_path.exists():
        aside = db_path.with_name(db_path.stem + f".before-restore-{datetime.now(UTC):%Y%m%d-%H%M%S}.db")
        shutil.move(str(db_path), aside)
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
    shutil.copy2(src, db_path)
    repo = Repository(db_path)
    repo.migrate()
    repo.set_kv("recovery_required", {"reason": f"manual restore from {src.name}", "ts": datetime.now(UTC).isoformat()})
    repo.close()
    return db_path


def main() -> None:
    from trinity.config.settings import load_settings

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--restore", type=Path)
    ap.add_argument("--keep", type=int, default=14)
    args = ap.parse_args()
    s = load_settings()
    if args.list:
        for p in sorted(s.backup_dir.glob("trinity-*.db")):
            print(p.name, p.stat().st_size)
        return
    if args.restore:
        print("restored ->", restore_backup(args.restore, s.database_path))
        print("Trading will stay locked until startup reconciliation succeeds.")
        return
    repo = Repository(s.database_path)
    repo.migrate()
    print("backup ->", create_backup(repo, s.backup_dir, args.keep))
    repo.close()


if __name__ == "__main__":
    main()
