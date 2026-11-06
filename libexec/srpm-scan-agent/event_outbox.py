"""Small durable outbox; notifications are removed only after acknowledgement."""

import os
from pathlib import Path
import sqlite3
import time


class EventOutbox:
    def __init__(self, workdir, kind):
        directory = Path(workdir) / ".scan-event-outbox"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if directory.is_symlink():
            raise OSError("event outbox directory must not be a symbolic link")
        directory.chmod(0o700)
        path = directory / f"{kind}.sqlite3"
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        self.db = sqlite3.connect(path, timeout=5)
        try:
            # FULL is deliberate: commit is the durability boundary before I/O.
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS pending_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    endpoint TEXT NOT NULL,
                    body BLOB NOT NULL,
                    created_at_ns INTEGER NOT NULL,
                    last_attempt_ns INTEGER,
                    UNIQUE(endpoint, body)
                )
            """)
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.db.close()

    def store(self, endpoint, body):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO pending_events (endpoint, body, created_at_ns) VALUES (?, ?, ?)",
                (endpoint, body, time.time_ns()),
            )
            return self.db.execute(
                "SELECT id FROM pending_events WHERE endpoint = ? AND body = ?",
                (endpoint, body),
            ).fetchone()[0]

    def pending(self, endpoint):
        # A rejected event must not starve older, unattempted work across batches.
        return self.db.execute(
            "SELECT id, body FROM pending_events WHERE endpoint = ? "
            "ORDER BY coalesce(last_attempt_ns, created_at_ns), id LIMIT 20", (endpoint,),
        ).fetchall()

    def attempted(self, event_id):
        with self.db:
            self.db.execute(
                "UPDATE pending_events SET last_attempt_ns = ? WHERE id = ?",
                (time.time_ns(), event_id),
            )

    def count(self, endpoint):
        return self.db.execute(
            "SELECT count(*) FROM pending_events WHERE endpoint = ?", (endpoint,),
        ).fetchone()[0]

    def acknowledge(self, event_id):
        # Concurrent senders can repeat delivery; the Portal is idempotent.
        # Never replace an event in-place, so a late acknowledgement cannot
        # delete a newer event with a reused identifier.
        with self.db:
            self.db.execute("DELETE FROM pending_events WHERE id = ?", (event_id,))
