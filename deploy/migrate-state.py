#!/usr/bin/env python3
"""Back up and migrate a stopped instance's SQLite state without replacing it."""
import argparse
from contextlib import closing
from pathlib import Path
import runpy
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from tracker import state, storage


def migrate(db, backup, timeout=30):
    path = Path(db)
    with state.writer_lock(path, timeout=10):
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
        before = state.read(path)
        if version == storage.FORMAT:
            return False
        if version not in (0, 2, 3):
            raise ValueError('unsupported database storage version; no migration performed')
        create_backup = runpy.run_path(str(ROOT / 'deploy/backup-snapshot.py'))['backup']
        create_backup(path, backup, timeout)
        if state.read(backup) != before:
            raise ValueError('backup does not match the stopped database')
        storage.migrate(path)
        if state.read(path) != before:
            raise ValueError('migration changed observations; stop and inspect the saved backup')
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--backup', required=True)
    parser.add_argument('--timeout-seconds', type=float, default=30)
    args = parser.parse_args(argv)
    try:
        changed = migrate(args.db, args.backup, args.timeout_seconds)
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f'migration failed: {error}', file=sys.stderr)
        return 2
    print('migrated' if changed else 'unchanged')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
