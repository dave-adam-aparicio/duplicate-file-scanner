#!/usr/bin/env python3
"""Preview or explicitly apply verified duplicate cleanup from a completed scan."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import shutil
import sqlite3

from duplicate_scan import fingerprint, inspect_file, utc_now


def verified(row: sqlite3.Row | dict) -> os.stat_result:
    """Return current metadata only if recorded content still matches."""
    info, digest = inspect_file(Path(row['path']))
    if info.st_size != row['size'] or digest != row['sha256']:
        raise OSError(f"content changed: {row['path']}")
    return info


def select_keeper(rows: list[sqlite3.Row], metadata: dict[int, os.stat_result], policy: str) -> sqlite3.Row:
    """Choose a keeper independently of the requested filesystem action."""
    if policy == 'newest':
        return min(rows, key=lambda r: (-metadata[r['id']].st_mtime_ns, r['path']))
    return min(rows, key=lambda r: r['path'])


def check_destination(destination: Path, root: Path) -> Path:
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise OSError('destination contains a symlink')
    destination = destination.resolve()
    if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise OSError('destination must be outside and not contain the scanned root')
    if destination.exists() and not destination.is_dir():
        raise OSError('destination is not a directory')
    return destination


def run(conn: sqlite3.Connection, scan_id: int | None = None,
        policy: str = 'alphabetical', action: str = 'delete',
        destination: Path | None = None, apply: bool = False) -> tuple[int, int, int]:
    """Validate groups; return (selected/actioned files, logical bytes, failures)."""
    conn.row_factory = sqlite3.Row
    # Probe the original schema before any filesystem mutation.
    conn.execute('SELECT scan_id, duplicate_group, path, size, sha256, status, action_note, actioned_at FROM files LIMIT 0')
    scan = conn.execute('SELECT * FROM scans WHERE completed_at IS NOT NULL AND (? IS NULL OR id=?) ORDER BY id DESC LIMIT 1', (scan_id, scan_id)).fetchone()
    if scan is None:
        raise ValueError('no matching completed scan')
    root = Path(scan['root_path'])
    if action == 'move':
        if destination is None:
            raise ValueError('move requires a destination')
        destination = check_destination(destination.absolute(), root)
    groups = conn.execute("SELECT DISTINCT duplicate_group FROM files WHERE scan_id=? AND duplicate_group IS NOT NULL AND status='pending' ORDER BY duplicate_group", (scan['id'],)).fetchall()
    count = total = failures = 0
    for group in groups:
        rows = conn.execute("SELECT * FROM files WHERE scan_id=? AND duplicate_group=? AND status='pending' ORDER BY path", (scan['id'], group[0])).fetchall()
        if len(rows) < 2:
            continue
        try:
            if len({(r['size'], r['sha256']) for r in rows}) != 1 or not rows[0]['sha256']:
                raise OSError('inconsistent duplicate group')
            if any(not Path(r['path']).is_absolute() or not Path(r['path']).is_relative_to(root) for r in rows):
                raise OSError('path outside scan root')
            metadata = {r['id']: verified(r) for r in rows}
            keeper = select_keeper(rows, metadata, policy)
            logging.info('Scan %s group %s | keep=%s | action=%s | apply=%s', scan['id'], group[0], policy, action, apply)
            logging.info('KEEP %s | bytes=%s | mtime_ns=%s', keeper['path'], keeper['size'], metadata[keeper['id']].st_mtime_ns)
        except OSError as exc:
            logging.warning('SKIP group %s: %s', group[0], exc)
            failures += 1
            continue
        for row in rows:
            if row['id'] == keeper['id']:
                continue
            source = Path(row['path'])
            target = destination / f"scan-{scan['id']}" / source.relative_to(root) if destination else None
            logging.info('%s %s | bytes=%s | mtime_ns=%s%s', action.upper(), source, row['size'], metadata[row['id']].st_mtime_ns, f' -> {target}' if target else '')
            if not apply:
                count += 1
                total += row['size']
                continue
            try:
                # Persist recoverable intent before touching the filesystem.
                conn.execute('UPDATE files SET action_note=?, actioned_at=? WHERE id=?', (f'intent {action}: {source} -> {target}', utc_now(), row['id']))
                conn.commit()
                for member in (keeper, row):
                    if fingerprint(verified(member)) != fingerprint(metadata[member['id']]):
                        raise OSError('metadata changed since selection; rescan/repreview')
                if target:
                    check_destination(destination, root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if any(p.is_symlink() for p in (target.parent, *target.parent.parents)):
                        raise OSError('symlink destination parent')
                    # Exclusive creation prevents overwrites; an existing target is skipped.
                    with target.open('xb') as output, source.open('rb') as input_file:
                        shutil.copyfileobj(input_file, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    shutil.copystat(source, target, follow_symlinks=False)
                    directory_fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    verified(dict(row, path=str(target)))
                # Revalidate both copies immediately before unlinking the candidate.
                for member in (keeper, row):
                    if fingerprint(verified(member)) != fingerprint(metadata[member['id']]):
                        raise OSError('file changed before unlink')
                source.unlink()
                conn.execute('UPDATE files SET status=?, actioned_at=?, action_note=? WHERE id=?', ('moved' if target else 'deleted', utc_now(), f'{action}: {source} -> {target}', row['id']))
                conn.commit()
                count += 1
                total += row['size']
            except (OSError, sqlite3.Error) as exc:
                logging.error('FAILED %s: %s; inspect action_note and any destination copy', source, exc)
                failures += 1
                break
    logging.info('Total files=%d | logical bytes=%d | failures=%d', count, total, failures)
    return count, total, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path)
    parser.add_argument('--keep', choices=['alphabetical', 'newest'], default='alphabetical')
    parser.add_argument('--scan-id', type=int, help='Completed scan ID (default: latest completed scan)')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--move', type=Path, metavar='DIR', help='Quarantine outside scanned tree')
    actions.add_argument('--delete', action='store_true', help='Select permanent deletion (preview unless --apply)')
    parser.add_argument('--apply', action='store_true', help='Perform the explicitly selected action')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args(argv)
    if args.apply and not (args.move or args.delete):
        parser.error('--apply requires --move DIR or --delete')
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format='%(levelname)s %(message)s')
    try:
        uri = args.database.expanduser().resolve().as_uri() + ('?mode=rw' if args.apply else '?mode=ro')
        with sqlite3.connect(uri, uri=True) as conn:
            result = run(conn, args.scan_id, args.keep, 'move' if args.move else 'delete', args.move.expanduser() if args.move else None, args.apply)
        return 1 if result[2] else 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        logging.error('%s. Use a database created by duplicate_scan.py; preserve older databases and rescan into a new database if incompatible.', exc)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
