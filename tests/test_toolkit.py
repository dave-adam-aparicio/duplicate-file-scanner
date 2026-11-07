"""All mutation tests use disposable TemporaryDirectory fixtures."""
import contextlib
import io
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import duplicate_scan as scan
import duplicate_cleanup as cleanup


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'input'
        self.root.mkdir()
        self.db = self.base / 'scan.db'
        self.a = self.root / 'a'
        self.b = self.root / 'b'
        self.a.write_bytes(b'content')
        self.b.write_bytes(b'content')
        self.conn = sqlite3.connect(self.db)
        self.addCleanup(self.conn.close)
        self.conn.executescript(scan.SCHEMA)

    def inventory(self):
        sid = scan.create_scan(self.conn, self.root)
        seen, errors = scan.collect_files(self.root, self.conn, sid)
        hashed, more = scan.hash_candidates(self.conn, sid)
        groups, files = scan.assign_duplicate_groups(self.conn, sid)
        scan.finalize_scan(self.conn, sid, seen, hashed, files, groups, errors + more)
        return sid

    def act(self, **kwargs):
        return cleanup.run(self.conn, **kwargs)

    def test_content_detection_and_sql(self):
        (self.root / 'c').write_bytes(b'differ!')
        self.inventory()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files WHERE duplicate_group IS NOT NULL').fetchone()[0], 2)
        self.assertEqual(self.conn.execute('SELECT duplicate_groups FROM scans').fetchone()[0], 1)

    def test_alphabetical_and_repeat_delete(self):
        self.inventory()
        self.assertEqual(self.act(apply=True)[0], 1)
        self.assertTrue(self.a.exists())
        self.assertFalse(self.b.exists())
        self.assertEqual(self.act(apply=True)[0], 0)
        self.assertTrue(self.a.exists())

    def test_newest(self):
        os.utime(self.a, ns=(1_000_000_000, 1_000_000_001))
        os.utime(self.b, ns=(1_000_000_000, 1_000_000_002))
        self.inventory()
        self.act(policy='newest', apply=True)
        self.assertFalse(self.a.exists())
        self.assertTrue(self.b.exists())

    def test_tie(self):
        for p in (self.a, self.b):
            os.utime(p, ns=(2_000_000_000, 2_000_000_000))
        self.inventory()
        self.act(policy='newest', apply=True)
        self.assertTrue(self.a.exists())
        self.assertFalse(self.b.exists())

    def test_dry_run_unchanged_database_and_files(self):
        self.inventory()
        before = self.db.read_bytes()
        self.assertEqual(cleanup.main([str(self.db), '--move', str(self.base / 'out')]), 0)
        self.assertEqual(self.db.read_bytes(), before)
        self.assertTrue(self.a.exists() and self.b.exists())
        self.assertFalse((self.base / 'out').exists())

    def test_all_retention_move_combinations(self):
        for policy in ('alphabetical', 'newest'):
            with self.subTest(policy=policy):
                self.a.write_bytes(b'content')
                self.b.write_bytes(b'content')
                os.utime(self.a, ns=(1_000_000_000, 1_000_000_000))
                os.utime(self.b, ns=(2_000_000_000, 2_000_000_000))
                sid = self.inventory()
                destination = self.base / policy
                self.assertEqual(self.act(scan_id=sid, policy=policy, action='move', destination=destination, apply=True)[0], 1)
                keeper = self.a if policy == 'alphabetical' else self.b
                self.assertTrue(keeper.exists())
                moved = list(destination.rglob('*'))
                self.assertEqual(sum(p.is_file() for p in moved), 1)
                self.assertIn(str(destination), self.conn.execute("SELECT action_note FROM files WHERE status='moved' AND scan_id=?", (sid,)).fetchone()[0])

    def test_duplicate_basenames(self):
        for name in ('x', 'y'):
            folder = self.root / name
            folder.mkdir()
            (folder / 'same').write_bytes(b'content')
        self.inventory()
        out = self.base / 'out'
        self.assertEqual(self.act(action='move', destination=out, apply=True)[0], 3)
        self.assertEqual(len(list(out.rglob('same'))), 2)
        self.assertTrue(self.a.exists())

    def test_changed_missing_keeper_or_candidate(self):
        for path in (self.a, self.b):
            for change in ('missing', 'content'):
                with self.subTest(path=path.name, change=change):
                    self.a.write_bytes(b'content')
                    self.b.write_bytes(b'content')
                    sid = self.inventory()
                    if change == 'missing':
                        path.unlink()
                    else:
                        path.write_bytes(b'changed')
                    self.assertEqual(self.act(scan_id=sid, apply=True)[0], 0)
                    other = self.b if path == self.a else self.a
                    self.assertTrue(other.exists())

    def test_live_timestamp_after_scan(self):
        self.inventory()
        os.utime(self.b, ns=(8_000_000_000, 8_000_000_000))
        os.utime(self.a, ns=(7_000_000_000, 7_000_000_000))
        self.act(policy='newest', apply=True)
        self.assertTrue(self.b.exists())

    def test_links_excluded_and_revalidated(self):
        (self.root / 'symbolic').symlink_to(self.a)
        os.link(self.a, self.root / 'hard')
        self.inventory()
        self.assertEqual(self.conn.execute('SELECT count(*) FROM files').fetchone()[0], 1)
        (self.root / 'hard').unlink()
        sid = self.inventory()
        self.b.unlink()
        self.b.symlink_to(self.a)
        self.assertEqual(self.act(scan_id=sid, apply=True)[0], 0)
        self.assertTrue(self.a.exists())

    def test_original_schema_and_scan_isolation(self):
        old = self.inventory()
        new = self.inventory()
        self.assertEqual(self.act(scan_id=new, apply=True)[0], 1)
        self.assertEqual(self.act(scan_id=old, apply=True)[0], 0)
        self.assertTrue(self.a.exists())

    def test_incompatible_schema_clear_failure(self):
        bad = self.base / 'old.db'
        with sqlite3.connect(bad) as conn:
            conn.execute('CREATE TABLE files(path TEXT)')
        with self.assertLogs(level='ERROR') as logs:
            self.assertEqual(cleanup.main([str(bad)]), 1)
        self.assertIn('preserve older databases', ''.join(logs.output))

    def test_destination_collision_no_overwrite(self):
        sid = self.inventory()
        out = self.base / 'out'
        target = out / f'scan-{sid}' / 'b'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'valuable')
        self.assertEqual(self.act(action='move', destination=out, apply=True)[2], 1)
        self.assertEqual(target.read_bytes(), b'valuable')
        self.assertTrue(self.b.exists())

    def test_invalid_destinations(self):
        self.inventory()
        for dest in (self.root, self.root / 'out', self.base):
            with self.assertRaises(OSError):
                self.act(action='move', destination=dest, apply=True)

    def test_no_apply_requires_explicit_action(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                cleanup.main([str(self.db), '--apply'])

    def test_keeper_disappears_before_action(self):
        self.inventory()
        original = cleanup.verified
        calls = 0
        def change(row):
            nonlocal calls
            calls += 1
            if calls == 3:
                self.a.unlink()
            return original(row)
        with patch.object(cleanup, 'verified', side_effect=change):
            self.assertEqual(self.act(apply=True)[0], 0)
        self.assertTrue(self.b.exists())

    def test_database_inside_root_excluded(self):
        with sqlite3.connect(self.root / 'inventory.db') as conn:
            conn.executescript(scan.SCHEMA)
            sid = scan.create_scan(conn, self.root)
            scan.collect_files(self.root, conn, sid)
            self.assertEqual(conn.execute('SELECT count(*) FROM files').fetchone()[0], 2)

    def test_interrupted_move_keeps_source_and_intent(self):
        self.inventory()
        out = self.base / 'out'
        with patch.object(cleanup.shutil, 'copyfileobj', side_effect=OSError('simulated disk full')):
            result = self.act(action='move', destination=out, apply=True)
        self.assertEqual(result[0], 0)
        self.assertTrue(self.a.exists() and self.b.exists())
        note = self.conn.execute("SELECT action_note FROM files WHERE path=?", (str(self.b),)).fetchone()[0]
        self.assertIn('intent move:', note)
        self.assertEqual(self.act(action='move', destination=out, apply=True)[0], 0)

    def test_symlink_directory_not_traversed(self):
        outside = self.base / 'outside'
        outside.mkdir()
        (outside / 'copy').write_bytes(b'content')
        (self.root / 'linkdir').symlink_to(outside, target_is_directory=True)
        self.inventory()
        self.assertEqual(self.conn.execute('SELECT files_seen FROM scans').fetchone()[0], 2)

    def test_large_file_hash_covers_last_chunk(self):
        data = b'x' * (1024 * 1024 + 17)
        self.a.write_bytes(data + b'a')
        self.b.write_bytes(data + b'b')
        self.inventory()
        self.assertEqual(self.conn.execute('SELECT duplicate_groups FROM scans').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
