"""Daily backup of the data that can't be re-downloaded: the SQLite database and
data/filings/ (hand-downloaded results files and IR PDFs).

A backup is one gzip tarball in BACKUP_DIR (.env), named
stock-intel-YYYYMMDD-HHMMSS.tar.gz (IST), containing:
- stock_intel.db: a consistent copy made with SQLite's online backup API (safe while the
  database is in use), checked with PRAGMA integrity_check before it's archived;
- filings/: a copy of data/filings/;
- manifest.json: when it was made, the database's SHA-256, and file counts.

The archive is written as "<name>.partial" and renamed only when complete, so a crash
never leaves a half-written backup that looks valid. Backups older than KEEP_DAYS are
deleted after a successful backup; the newest one is always kept, and files that don't
match the backup name pattern are never touched.

Restore (never touches the live project): `--restore ARCHIVE --to DIR` extracts into an
empty DIR, then checks the database's SHA-256 against the manifest and runs an
integrity check. Moving the restored files into place is a manual step (see README).

Run with:  uv run python -m storage.backup [--list | --restore ARCHIVE|latest --to DIR]
"""

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from storage.db import PROJECT_ROOT, resolve_db_url
from utils import setup_logging

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
KEEP_DAYS = 14
BACKUP_DIR_ENV = "BACKUP_DIR"
NAME_RE = re.compile(r"^stock-intel-(\d{8})-(\d{6})\.tar\.gz$")
DB_NAME, FILINGS_NAME, MANIFEST_NAME = "stock_intel.db", "filings", "manifest.json"


class BackupError(RuntimeError):
    """A backup or restore failed or couldn't be verified."""


@dataclass(frozen=True)
class BackupResult:
    """A finished backup."""

    path: Path
    size_bytes: int
    filings: int
    deleted: tuple[Path, ...]


def backup_dir() -> Path:
    """BACKUP_DIR from .env; must be absolute and outside the project.

    Raises:
        BackupError: if it isn't set, isn't absolute, or is inside the project.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    raw = os.environ.get(BACKUP_DIR_ENV, "").strip()
    if not raw:
        raise BackupError(f"{BACKUP_DIR_ENV} isn't set in .env")
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise BackupError(f"{BACKUP_DIR_ENV} must be an absolute path")
    if path.resolve().is_relative_to(PROJECT_ROOT.resolve()):
        raise BackupError(f"{BACKUP_DIR_ENV} must be outside the project folder")
    return path


def live_db_path() -> Path:
    """The configured database file (DB_PATH)."""
    return Path(resolve_db_url().removeprefix("sqlite:///"))


def sha256(path: Path) -> str:
    """Hex SHA-256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def copy_database(source: Path, target: Path) -> None:
    """Consistent copy of a (possibly in-use) SQLite database via the backup API, verified
    with PRAGMA integrity_check."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
        result = dst.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        dst.close()
        src.close()
    if result != "ok":
        raise BackupError(f"integrity check of the database copy failed: {result}")


def backup_name(now: dt.datetime) -> str:
    """stock-intel-YYYYMMDD-HHMMSS.tar.gz in IST."""
    return f"stock-intel-{now.astimezone(IST):%Y%m%d-%H%M%S}.tar.gz"


def created_at(name: str) -> dt.datetime | None:
    """When a backup was made, from its file name (None if it isn't a backup name)."""
    m = NAME_RE.match(name)
    if not m:
        return None
    return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=IST)


def prune(directory: Path, now: dt.datetime, keep_days: int = KEEP_DAYS) -> tuple[Path, ...]:
    """Delete backups older than `keep_days`, always keeping the newest. Other files are
    left alone. Returns the deleted paths."""
    backups = sorted(
        (when, p) for p in directory.iterdir() if (when := created_at(p.name)) is not None
    )
    cutoff = now - dt.timedelta(days=keep_days)
    old = [p for when, p in backups[:-1] if when < cutoff]
    for path in old:
        path.unlink()
    return tuple(old)


def make_backup(
    directory: Path,
    db_path: Path,
    filings_dir: Path,
    now: dt.datetime | None = None,
    keep_days: int = KEEP_DAYS,
) -> BackupResult:
    """Write one backup into `directory`, then prune old ones (see module docstring).

    Raises:
        BackupError: if the database is missing, its copy fails the integrity check, or
            writing the archive fails.
    """
    now = now or dt.datetime.now(dt.UTC)
    if not db_path.exists():
        raise BackupError(f"database not found: {db_path}")
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / backup_name(now)
    partial = final.with_name(final.name + ".partial")
    with tempfile.TemporaryDirectory(prefix="stock-intel-backup-") as tmp:
        db_copy = Path(tmp) / DB_NAME
        copy_database(db_path, db_copy)
        files = [p for p in filings_dir.rglob("*") if p.is_file()] if filings_dir.exists() else []
        manifest = {
            "created_at": now.isoformat(),
            "db_sha256": sha256(db_copy),
            "db_bytes": db_copy.stat().st_size,
            "filings_files": len(files),
        }
        (Path(tmp) / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        try:
            with tarfile.open(partial, "w:gz", compresslevel=6) as tar:
                tar.add(Path(tmp) / MANIFEST_NAME, arcname=MANIFEST_NAME)
                tar.add(db_copy, arcname=DB_NAME)
                if filings_dir.exists():
                    tar.add(filings_dir, arcname=FILINGS_NAME)
            os.replace(partial, final)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
    deleted = prune(directory, now, keep_days)
    return BackupResult(final, final.stat().st_size, len(files), deleted)


def restore(archive: Path, target: Path) -> dict:
    """Extract `archive` into the empty (or new) directory `target` and verify it: the
    database's SHA-256 must match the manifest and pass an integrity check. Returns the
    manifest. The live project is never touched.

    Raises:
        BackupError: if `target` isn't empty or verification fails.
    """
    if target.exists() and any(target.iterdir()):
        raise BackupError(f"restore target isn't empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as tar:
        tar.extractall(target, filter="data")  # refuses absolute paths and ../ escapes
    manifest = json.loads((target / MANIFEST_NAME).read_text(encoding="utf-8"))
    db = target / DB_NAME
    if sha256(db) != manifest["db_sha256"]:
        raise BackupError("restored database doesn't match the manifest's SHA-256")
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise BackupError(f"restored database failed the integrity check: {result}")
    files = [p for p in (target / FILINGS_NAME).rglob("*") if p.is_file()]
    if len(files) != manifest["filings_files"]:
        raise BackupError(f"expected {manifest['filings_files']} filings files, got {len(files)}")
    return manifest


def latest_backup(directory: Path) -> Path:
    """The newest backup in `directory`.

    Raises:
        BackupError: if there are none.
    """
    backups = sorted((when, p) for p in directory.iterdir() if (when := created_at(p.name)))
    if not backups:
        raise BackupError(f"no backups in {directory}")
    return backups[-1][1]


def run_backup() -> BackupResult:
    """Back up the configured database and data/filings/ into BACKUP_DIR and log the size."""
    result = make_backup(backup_dir(), live_db_path(), PROJECT_ROOT / "data" / "filings")
    logger.info(
        "Backup written: %s (%.1f MB, database + %d filings files); %d old backup(s) deleted",
        result.path.name,
        result.size_bytes / 1e6,
        result.filings,
        len(result.deleted),
    )
    return result


def main(argv: list[str] | None = None) -> int:
    """Entry point: make a backup, --list backups, or --restore one into a folder."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="list backups in BACKUP_DIR")
    parser.add_argument(
        "--restore", help="archive to restore, or 'latest' for the newest in BACKUP_DIR"
    )
    parser.add_argument("--to", type=Path, help="empty folder to restore into")
    args = parser.parse_args(argv)
    setup_logging()
    if args.restore:
        if not args.to:
            parser.error("--restore needs --to")
        archive = latest_backup(backup_dir()) if args.restore == "latest" else Path(args.restore)
        manifest = restore(archive, args.to)
        logger.info(
            "Restored %s into %s and verified (backup made %s, %d filings files)",
            archive.name,
            args.to,
            manifest["created_at"],
            manifest["filings_files"],
        )
        return 0
    directory = backup_dir()
    if args.list:
        for path in sorted(directory.glob("stock-intel-*.tar.gz")):
            print(f"{path.name}  {path.stat().st_size / 1e6:.1f} MB")
        return 0
    run_backup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
