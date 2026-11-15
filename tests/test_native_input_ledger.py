"""Exercise the real native DDL/F0n insert in an isolated in-memory SQLite DB."""
import json
import sqlite3
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA, SAVE_INPUT = (ROOT / "tests/fixtures/zcode_3_18_2_session_input.sql").read_text(encoding="utf-8").split("-- NATIVE_F0N_INSERT\n", 1)


class NativeInputLedgerRegressionTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("pragma foreign_keys=on")
        self.db.executescript(SCHEMA)

    def save_input(self, queue_id="native-input-1", client_id="desktop-native-shim-v1"):
        payload = json.dumps({"text": "fixture input", "intent": {"clientId": client_id}})
        self.db.execute(SAVE_INPUT, (queue_id, "sess_fresh", "sendText", "startNow", payload, "sess_fresh", 1, 1))

    def persist_session(self):
        self.db.execute(
            "insert into session(id,project_id,slug,directory,title,version,time_created,time_updated) values(?,?,?,?,?,?,?,?)",
            ("sess_fresh", "fixture-project", "fixture-slug", "/fixture-workspace", "Fixture", "fixture", 1, 1),
        )

    def test_native_v4_ledger_cannot_admit_input_before_core_persists_the_session(self):
        with self.assertRaisesRegex(sqlite3.IntegrityError, "FOREIGN KEY constraint failed"):
            self.save_input()
        self.assertEqual(self.db.execute("select count(*) from session_input").fetchone()[0], 0)
        self.assertEqual(self.db.execute("select count(*) from session").fetchone()[0], 0)

    def test_core_session_persistence_before_v4_ledger_removes_the_exact_fk_failure(self):
        self.persist_session()
        self.save_input()
        self.assertEqual(self.db.execute("select session_id,delivery,status from session_input").fetchone(),
                         ("sess_fresh", "startNow", "admitted"))

    def test_client_identity_is_json_payload_not_a_registered_foreign_key(self):
        foreign_keys = self.db.execute("pragma foreign_key_list(session_input)").fetchall()
        self.assertEqual([(row[2], row[3], row[4]) for row in foreign_keys], [("session", "session_id", "id")])
        self.persist_session()
        self.save_input(client_id="desktop-native-shim-v1")
        self.save_input(queue_id="native-input-2", client_id="another-unregistered-fixture-client")
        rows = self.db.execute("select payload from session_input order by admitted_sequence").fetchall()
        self.assertEqual([json.loads(row[0])["intent"]["clientId"] for row in rows],
                         ["desktop-native-shim-v1", "another-unregistered-fixture-client"])


if __name__ == "__main__":
    unittest.main()
