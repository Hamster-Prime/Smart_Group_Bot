from __future__ import annotations

import sqlite3
import unittest

from bot.db.engine import (
    _SQLITE_VOTE_BAN_DEDUPE_SQL,
    _SQLITE_VOTE_BAN_INDEX_SQL,
)


class VoteBanMigrationTests(unittest.TestCase):
    def test_duplicate_open_sessions_are_closed_before_unique_index(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute(
            "CREATE TABLE vote_ban_sessions ("
            "id INTEGER PRIMARY KEY, group_id BIGINT NOT NULL, "
            "target_user_id BIGINT NOT NULL, status TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO vote_ban_sessions "
            "(id, group_id, target_user_id, status) VALUES (?, ?, ?, ?)",
            [
                (1, -100, 7, "active"),
                (2, -100, 7, "enforcing"),
                (3, -100, 7, "active"),
                (4, -100, 8, "active"),
            ],
        )

        connection.execute(_SQLITE_VOTE_BAN_DEDUPE_SQL)
        connection.execute(_SQLITE_VOTE_BAN_INDEX_SQL)

        open_rows = connection.execute(
            "SELECT id, target_user_id, status FROM vote_ban_sessions "
            "WHERE status IN ('active', 'enforcing') ORDER BY id"
        ).fetchall()
        self.assertEqual(open_rows, [(2, 7, "enforcing"), (4, 8, "active")])
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO vote_ban_sessions "
                "(id, group_id, target_user_id, status) "
                "VALUES (5, -100, 7, 'active')"
            )


if __name__ == "__main__":
    unittest.main()
