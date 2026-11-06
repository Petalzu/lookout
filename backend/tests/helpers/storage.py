"""Reusable old-format SQLite fixtures, independent of test modules."""
import json
import sqlite3
from contextlib import closing


def legacy(db, snapshot, stamp=None):
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute('CREATE TABLE snapshot (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        conn.execute('INSERT INTO snapshot VALUES (1, ?)', (json.dumps(snapshot),))
        conn.execute('CREATE TABLE snapshot_clock (id INTEGER PRIMARY KEY, revision TEXT, build_checked_at TEXT)')
        conn.execute('INSERT INTO snapshot_clock VALUES (1, ?, ?)', ('old', stamp))


def rows(db):
    with closing(sqlite3.connect(db)) as conn:
        return {tuple(row[:3]): tuple(row[3:]) for row in conn.execute('SELECT * FROM snapshot_records')}
