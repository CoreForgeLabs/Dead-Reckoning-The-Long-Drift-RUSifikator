# test_migrate_state.py
import json
import sqlite3
import tempfile
import os
import unittest

import zen_store
import migrate_state


class TestMigrate(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        zen_store.init_schema(self.con)
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump({
            "reviews": {
                "extra::earth literature": {
                    "status": "reviewed",
                    "updated_at": "2026-08-29T23:53:43.620953",
                    "ip": "203.0.113.5",
                }
            },
            "votes": {
                "narrative::x": {
                    "ip_votes": {"203.0.113.5": 1, "198.51.100.9": -1},
                    "up": 1, "down": 1, "score": 0,
                }
            },
            "suggestions": {
                "narrative::x": [
                    {
                        "id": "sug_1_123",
                        "text": "предложенный текст",
                        "comment": "пояснение",
                        "created_at": "29.08 23:00",
                        "ip": "203.0.113.5",
                        "votes": {"203.0.113.5": 1},
                    }
                ]
            },
            "history": {},
        }, self.tmp, ensure_ascii=False)
        self.tmp.close()

    def tearDown(self):
        os.unlink(self.tmp.name)

    def test_migrate_creates_identities_for_every_ip(self):
        migrate_state.migrate(self.tmp.name, self.con)
        ips = {r["created_ip"] for r in
               self.con.execute("SELECT created_ip FROM identities").fetchall()}
        self.assertEqual(ips, {"203.0.113.5", "198.51.100.9"})

    def test_migrate_creates_votes(self):
        migrate_state.migrate(self.tmp.name, self.con)
        rows = self.con.execute(
            "SELECT value FROM votes WHERE uid='narrative::x'").fetchall()
        self.assertEqual(sorted(r["value"] for r in rows), [-1, 1])

    def test_migrate_creates_suggestions(self):
        migrate_state.migrate(self.tmp.name, self.con)
        row = self.con.execute(
            "SELECT * FROM suggestions WHERE uid='narrative::x'").fetchone()
        self.assertEqual(row["text"], "предложенный текст")
        self.assertEqual(row["status"], "open")

    def test_migrate_returns_counts(self):
        counts = migrate_state.migrate(self.tmp.name, self.con)
        self.assertEqual(counts["identities"], 2)
        self.assertEqual(counts["votes"], 2)
        self.assertEqual(counts["suggestions"], 1)

    def test_migrate_missing_file_returns_zeros(self):
        counts = migrate_state.migrate("/no/such/file.json", self.con)
        self.assertEqual(counts, {"identities": 0, "votes": 0, "suggestions": 0})


if __name__ == "__main__":
    unittest.main()
