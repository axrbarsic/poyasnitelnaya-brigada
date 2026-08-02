from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts import codex_thread_state


class CodexThreadStateTests(unittest.TestCase):
    def test_reads_newest_database_without_write_access(self) -> None:
        with TemporaryDirectory() as temporary:
            codex_home = Path(temporary)
            database = codex_home / "state_1.sqlite"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """
                    CREATE TABLE threads (
                        id TEXT PRIMARY KEY,
                        archived INTEGER,
                        is_pinned INTEGER,
                        title TEXT,
                        model TEXT,
                        reasoning_effort TEXT,
                        cwd TEXT
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("thread", 0, 0, "Owner", "sol", "max", "/tmp/project"),
                )
                connection.commit()

            row = codex_thread_state.thread_row(codex_home, "thread")

            self.assertIsNotNone(row)
            self.assertEqual(row["title"], "Owner")
            self.assertEqual(row["reasoning_effort"], "max")

    def test_missing_database_fails_closed(self) -> None:
        with TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "database is missing"):
                codex_thread_state.thread_row(Path(temporary), "thread")


if __name__ == "__main__":
    unittest.main()
