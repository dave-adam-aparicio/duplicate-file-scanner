#!/usr/bin/env python3
"""
duplicate_scan.py - inventory files and identify exact-content duplicates.

Usage:
    python3 duplicate_scan.py /path/to/folder
    python3 duplicate_scan.py /path/to/folder --database duplicates.db
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import sqlite3
import os
import stat
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS scans (
    id              INTEGER PRIMARY KEY,
    root_path       TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    files_seen      INTEGER NOT NULL DEFAULT 0,
    files_hashed    INTEGER NOT NULL DEFAULT 0,
    duplicate_files INTEGER NOT NULL DEFAULT 0,
    duplicate_groups INTEGER NOT NULL DEFAULT 0,
    errors          INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS files (
    id                INTEGER PRIMARY KEY,
    scan_id           INTEGER NOT NULL REFERENCES scans(id),
    duplicate_group   INTEGER,
    path              TEXT NOT NULL,
    filename          TEXT NOT NULL,
    size              INTEGER NOT NULL,
    sha256            TEXT,
    modified_time     REAL,
    status            TEXT NOT NULL DEFAULT 'pending',
    actioned_at       TEXT,
    action_note       TEXT,
    scanned_at        TEXT NOT NULL,
    error_message     TEXT,
    UNIQUE(scan_id, path)
);

CREATE INDEX IF NOT EXISTS idx_files_scan_size
    ON files(scan_id, size);

CREATE INDEX IF NOT EXISTS idx_files_scan_hash
    ON files(scan_id, sha256);

CREATE INDEX IF NOT EXISTS idx_files_group
    ON files(scan_id, duplicate_group);

CREATE INDEX IF NOT EXISTS idx_files_status
    ON files(status);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )


def get_file_hash(path: Path, chunk_size: int = 1024 * 1024) -> str:
    return inspect_file(path, chunk_size)[1]


def inspect_file(path: Path, chunk_size: int = 1024 * 1024) -> tuple[os.stat_result, str]:
    """Hash a stable, regular, singly linked file without following symlinks."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise OSError("symlink path component")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise OSError("not a regular singly linked file")
        digest = hashlib.sha256()
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
        after = os.fstat(handle.fileno())
    if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(path.lstat()):
        raise OSError("file changed during hashing")
    return after, digest.hexdigest()


def fingerprint(info: os.stat_result) -> tuple[int, ...]:
    """Identity and mutation indicators used around reads and mutations."""
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_nlink)


def collect_files(root: Path, conn: sqlite3.Connection, scan_id: int) -> tuple[int, int]:
    """Collect regular files without following symlinks."""
    seen = 0
    errors = 0

    # os.walk gives us control over symlink behavior and lets us continue
    # when individual directories/files are inaccessible.
    import os

    def walk_error(exc):
        nonlocal errors
        errors += 1
        logging.warning("Cannot traverse directory: %s", exc)

    database_path = conn.execute("PRAGMA database_list").fetchone()[2]
    excluded = {database_path + suffix for suffix in ("", "-journal", "-wal", "-shm")}
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=walk_error):
        # Do not recurse into symlinked directories.
        dirnames[:] = [
            name for name in dirnames
            if not (Path(dirpath) / name).is_symlink()
        ]

        for name in filenames:
            path = Path(dirpath) / name

            try:
                if str(path) in excluded or path.is_symlink():
                    continue

                info = path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue

                rows.append((
                    scan_id,
                    None,
                    str(path.resolve()),
                    path.name,
                    info.st_size,
                    None,
                    info.st_mtime,
                    "pending",
                    None,
                    None,
                    utc_now(),
                    None,
                ))
                seen += 1

                if len(rows) >= 500:
                    conn.executemany(
                        """INSERT INTO files (
                            scan_id, duplicate_group, path, filename, size,
                            sha256, modified_time, status, actioned_at,
                            action_note, scanned_at, error_message
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        rows,
                    )
                    conn.commit()
                    rows.clear()

            except (OSError, PermissionError) as exc:
                errors += 1
                logging.warning("Cannot inspect %s: %s", path, exc)

    if rows:
        conn.executemany(
            """INSERT INTO files (
                scan_id, duplicate_group, path, filename, size,
                sha256, modified_time, status, actioned_at,
                action_note, scanned_at, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        conn.commit()

    return seen, errors


def hash_candidates(conn: sqlite3.Connection, scan_id: int) -> tuple[int, int]:
    """Hash only files whose size occurs more than once."""
    rows = conn.execute(
        """
        SELECT id, path, size
        FROM files
        WHERE scan_id = ?
          AND size IN (
              SELECT size
              FROM files
              WHERE scan_id = ?
              GROUP BY size
              HAVING COUNT(*) > 1
          )
        ORDER BY path
        """,
        (scan_id, scan_id),
    ).fetchall()

    hashed = 0
    errors = 0

    for file_id, path_string, expected_size in rows:
        path = Path(path_string)
        try:
            info, digest = inspect_file(path)
            if info.st_size != expected_size:
                raise OSError("size changed since discovery; rescan required")
            conn.execute(
                "UPDATE files SET sha256 = ? WHERE id = ?",
                (digest, file_id),
            )
            hashed += 1

            if hashed % 100 == 0:
                conn.commit()
                logging.info("Hashed %d candidate files...", hashed)

        except (OSError, PermissionError) as exc:
            errors += 1
            conn.execute(
                """
                UPDATE files
                SET status = 'error', error_message = ?
                WHERE id = ?
                """,
                (str(exc), file_id),
            )
            logging.warning("Cannot hash %s: %s", path, exc)

    conn.commit()
    return hashed, errors


def assign_duplicate_groups(
    conn: sqlite3.Connection, scan_id: int
) -> tuple[int, int]:
    """Assign deterministic integer groups to identical size+hash combinations."""
    groups = conn.execute(
        """
        SELECT size, sha256
        FROM files
        WHERE scan_id = ?
          AND sha256 IS NOT NULL
          AND status != 'error'
        GROUP BY size, sha256
        HAVING COUNT(*) > 1
        ORDER BY size, sha256
        """,
        (scan_id,),
    ).fetchall()

    duplicate_group = 1
    duplicate_files = 0

    for size, digest in groups:
        ids = conn.execute(
            """
            SELECT id
            FROM files
            WHERE scan_id = ? AND size = ? AND sha256 = ?
            ORDER BY path
            """,
            (scan_id, size, digest),
        ).fetchall()

        conn.executemany(
            "UPDATE files SET duplicate_group = ? WHERE id = ?",
            [(duplicate_group, row[0]) for row in ids],
        )

        duplicate_files += len(ids)
        duplicate_group += 1

    conn.commit()
    return duplicate_group - 1, duplicate_files


def create_scan(conn: sqlite3.Connection, root: Path) -> int:
    cursor = conn.execute(
        """
        INSERT INTO scans (root_path, started_at)
        VALUES (?, ?)
        """,
        (str(root), utc_now()),
    )
    conn.commit()
    return cursor.lastrowid


def finalize_scan(
    conn: sqlite3.Connection,
    scan_id: int,
    files_seen: int,
    files_hashed: int,
    duplicate_files: int,
    duplicate_groups: int,
    errors: int,
) -> None:
    conn.execute(
        """
        UPDATE scans
        SET completed_at = ?,
            files_seen = ?,
            files_hashed = ?,
            duplicate_files = ?,
            duplicate_groups = ?,
            errors = ?
        WHERE id = ?
        """,
        (
            utc_now(),
            files_seen,
            files_hashed,
            duplicate_files,
            duplicate_groups,
            errors,
            scan_id,
        ),
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan a directory and record exact duplicate files in SQLite."
    )
    parser.add_argument("root", type=Path, help="Directory to scan recursively")
    parser.add_argument(
        "--database",
        "-d",
        type=Path,
        default=Path("duplicates.db"),
        help="SQLite database (default: duplicates.db)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable debug logging"
    )
    args = parser.parse_args()

    configure_logging(args.verbose)

    root = args.root.expanduser().resolve()
    if not root.exists():
        logging.error("Directory does not exist: %s", root)
        return 1
    if not root.is_dir():
        logging.error("Not a directory: %s", root)
        return 1

    logging.info("Scanning: %s", root)
    logging.info("Database: %s", args.database.resolve())

    start = time.monotonic()

    try:
        conn = sqlite3.connect(args.database.expanduser())
    except sqlite3.Error as exc:
        logging.error("Cannot open database: %s", exc)
        return 1
    try:
        conn.executescript(SCHEMA)

        scan_id = create_scan(conn, root)
        files_seen, collect_errors = collect_files(root, conn, scan_id)

        logging.info("Files discovered: %d", files_seen)

        files_hashed, hash_errors = hash_candidates(conn, scan_id)
        logging.info("Files hashed: %d", files_hashed)

        duplicate_groups, duplicate_files = assign_duplicate_groups(
            conn, scan_id
        )

        total_errors = collect_errors + hash_errors
        finalize_scan(
            conn,
            scan_id,
            files_seen,
            files_hashed,
            duplicate_files,
            duplicate_groups,
            total_errors,
        )

        elapsed = time.monotonic() - start

        logging.info("Duplicate groups: %d", duplicate_groups)
        logging.info("Files in duplicate groups: %d", duplicate_files)
        logging.info("Errors: %d", total_errors)
        logging.info("Elapsed time: %.1f seconds", elapsed)

        if duplicate_groups:
            logging.info(
                "Inspect with: sqlite3 %s",
                args.database,
            )

    except (OSError, sqlite3.Error) as exc:
        logging.error("Scan failed: %s; preserve the database and inspect or rescan", exc)
        return 1
    finally:
        conn.close()

    return 1 if total_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
