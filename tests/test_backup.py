"""Tests for storage/backup.py (tmp folders only; the real database and BACKUP_DIR are
never touched)."""

import datetime as dt
import io
import sqlite3
import tarfile
from pathlib import Path

import pytest

import storage.backup as bk

NOW = dt.datetime(2026, 9, 25, 10, 45, tzinfo=dt.UTC)  # 16:15 IST


def make_db(path: Path, rows: int = 100) -> sqlite3.Connection:
    """A WAL-mode database with committed rows; returns an open connection."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE prices (symbol TEXT, day TEXT, close REAL)")
    conn.executemany(
        "INSERT INTO prices VALUES ('INFY', ?, ?)",
        [(f"2026-01-{i % 28 + 1:02d}", 1000.0 + i) for i in range(rows)],
    )
    conn.commit()
    return conn


@pytest.fixture
def data(tmp_path: Path) -> tuple[Path, Path]:
    db = tmp_path / "project" / "stock_intel.db"
    db.parent.mkdir()
    make_db(db).close()
    filings = tmp_path / "project" / "filings"
    (filings / "INFY").mkdir(parents=True)
    (filings / "INFY" / "a.xml").write_text("<xbrl>results</xbrl>")
    (filings / "inbox" / "rejected").mkdir(parents=True)
    (filings / "inbox" / "rejected" / "b.pdf").write_bytes(b"%PDF-1.4 fake")
    return db, filings


def test_backup_and_restore_round_trip(tmp_path: Path, data) -> None:
    db, filings = data
    result = bk.make_backup(tmp_path / "backups", db, filings, now=NOW)
    assert result.path.name == "stock-intel-20260925-161500.tar.gz"
    assert result.filings == 2 and result.size_bytes == result.path.stat().st_size
    with tarfile.open(result.path) as tar:
        names = set(tar.getnames())
    assert {
        "manifest.json",
        "stock_intel.db",
        "filings/INFY/a.xml",
        "filings/inbox/rejected/b.pdf",
    } <= names

    target = tmp_path / "restored"
    manifest = bk.restore(result.path, target)
    assert manifest["filings_files"] == 2
    restored = sqlite3.connect(target / "stock_intel.db")
    assert restored.execute("SELECT count(*), sum(close) FROM prices").fetchone() == (100, 104950.0)
    assert (target / "filings/INFY/a.xml").read_text() == "<xbrl>results</xbrl>"
    assert (target / "filings/inbox/rejected/b.pdf").read_bytes() == b"%PDF-1.4 fake"


def test_the_copy_is_consistent_while_the_database_is_in_use(tmp_path: Path, data) -> None:
    db, filings = data
    writer = sqlite3.connect(db)
    writer.execute("BEGIN")
    writer.execute("INSERT INTO prices VALUES ('TCS', '2026-09-25', 1.0)")  # not committed
    result = bk.make_backup(tmp_path / "backups", db, filings, now=NOW)
    writer.rollback()
    writer.close()
    bk.restore(result.path, tmp_path / "restored")
    restored = sqlite3.connect(tmp_path / "restored" / "stock_intel.db")
    assert restored.execute("SELECT count(*) FROM prices").fetchone() == (100,)  # committed only


def test_restore_refuses_a_non_empty_folder_and_a_tampered_database(tmp_path: Path, data) -> None:
    db, filings = data
    archive = bk.make_backup(tmp_path / "backups", db, filings, now=NOW).path
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("mine")
    with pytest.raises(bk.BackupError, match="isn't empty"):
        bk.restore(archive, busy)

    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(archive) as src, tarfile.open(tampered, "w:gz") as dst:
        for member in src.getmembers():
            payload = src.extractfile(member) if member.isfile() else None
            if member.name == "stock_intel.db":
                changed = payload.read() + b"x"
                member.size = len(changed)
                payload = io.BytesIO(changed)
            dst.addfile(member, payload)
    with pytest.raises(bk.BackupError, match="SHA-256"):
        bk.restore(tampered, tmp_path / "t2")


def test_restore_never_writes_outside_the_target(tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    with pytest.raises(tarfile.TarError):
        bk.restore(evil, tmp_path / "target")
    assert not (tmp_path / "escaped.txt").exists()


def test_old_backups_are_pruned_and_other_files_left_alone(tmp_path: Path, data) -> None:
    db, filings = data
    backups = tmp_path / "backups"
    backups.mkdir()
    for days in (20, 15, 13, 1):
        name = bk.backup_name(NOW - dt.timedelta(days=days))
        (backups / name).write_bytes(b"old")
    (backups / "notes.txt").write_text("not a backup")
    result = bk.make_backup(backups, db, filings, now=NOW)
    left = sorted(p.name for p in backups.iterdir())
    assert [p.name for p in result.deleted] == [
        bk.backup_name(NOW - dt.timedelta(days=d)) for d in (20, 15)
    ]
    assert "notes.txt" in left and len([n for n in left if n.endswith(".tar.gz")]) == 3


def test_the_newest_backup_is_kept_even_if_old(tmp_path: Path) -> None:
    only = tmp_path / bk.backup_name(NOW - dt.timedelta(days=40))
    only.write_bytes(b"x")
    assert bk.prune(tmp_path, NOW) == () and only.exists()


def test_a_failed_archive_leaves_no_partial_file(tmp_path: Path, data, monkeypatch) -> None:
    db, filings = data

    def broken_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(bk.tarfile, "open", broken_open)
    with pytest.raises(OSError):
        bk.make_backup(tmp_path / "backups", db, filings, now=NOW)
    assert list((tmp_path / "backups").iterdir()) == []


@pytest.mark.parametrize(
    ("value", "message"),
    [("", "isn't set"), ("relative/dir", "absolute"), (str(bk.PROJECT_ROOT / "data"), "outside")],
)
def test_backup_dir_must_be_set_absolute_and_outside_the_project(
    monkeypatch, value: str, message: str
) -> None:
    monkeypatch.setattr(bk, "load_dotenv", lambda path: None)
    monkeypatch.setenv("BACKUP_DIR", value)
    with pytest.raises(bk.BackupError, match=message):
        bk.backup_dir()


def test_missing_database_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(bk.BackupError, match="not found"):
        bk.make_backup(tmp_path / "b", tmp_path / "none.db", tmp_path / "filings", now=NOW)


def test_latest_backup_is_the_newest_by_name(tmp_path: Path) -> None:
    for days in (3, 1, 2):
        (tmp_path / bk.backup_name(NOW - dt.timedelta(days=days))).write_bytes(b"x")
    (tmp_path / "zzz.tar.gz").write_bytes(b"not a backup")
    assert bk.latest_backup(tmp_path).name == bk.backup_name(NOW - dt.timedelta(days=1))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(bk.BackupError, match="no backups"):
        bk.latest_backup(empty)
