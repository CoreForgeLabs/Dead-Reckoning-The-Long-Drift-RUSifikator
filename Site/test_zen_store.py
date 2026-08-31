# test_zen_store.py
import os
import sqlite3
import tempfile
import unittest

import zen_store


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        zen_store.init_schema(self.con)

    def test_tables_exist(self):
        names = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(
            names,
            {"identities", "votes", "suggestions", "suggestion_votes", "history"},
        )


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        zen_store.init_schema(self.con)

    def test_new_token_is_created(self):
        ident = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        self.assertIsNotNone(ident)
        self.assertEqual(len(ident["token"]), 32)
        self.assertEqual(ident["created_ip"], "1.2.3.4")
        self.assertIsNone(ident["nickname"])

    def test_known_token_is_returned_unchanged(self):
        first = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        again = zen_store.get_or_create_identity(self.con, first["token"], "9.9.9.9")
        self.assertEqual(again["token"], first["token"])
        # повторный визит с другого IP не переписывает created_ip
        self.assertEqual(again["created_ip"], "1.2.3.4")

    def test_second_registration_from_same_ip_recovers_existing_identity(self):
        # Потеря cookie (очистка, смена браузера, приватный режим) не должна
        # означать вечную блокировку с этого IP -- вернуть ту же личность,
        # а не отказ и не вторую отдельную личность.
        first = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        second = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        self.assertIsNotNone(second)
        self.assertEqual(second["token"], first["token"])

    def test_recovered_identity_keeps_nickname(self):
        first = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        zen_store.set_nickname(self.con, first["token"], "Вася")
        second = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        self.assertEqual(second["nickname"], "Вася")

    def test_different_ip_can_register(self):
        first = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        second = zen_store.get_or_create_identity(self.con, None, "5.6.7.8")
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["token"], second["token"])

    def test_set_nickname(self):
        ident = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        zen_store.set_nickname(self.con, ident["token"], "Вася")
        again = zen_store.get_or_create_identity(self.con, ident["token"], "1.2.3.4")
        self.assertEqual(again["nickname"], "Вася")


class TestAdmin(unittest.TestCase):
    def test_load_admin_tokens_splits_and_strips(self):
        os.environ["ZEN_ADMIN_TOKENS"] = " abc123 , def456,,ghi789 "
        self.assertEqual(
            zen_store.load_admin_tokens(), {"abc123", "def456", "ghi789"})
        del os.environ["ZEN_ADMIN_TOKENS"]

    def test_load_admin_tokens_empty_when_unset(self):
        os.environ.pop("ZEN_ADMIN_TOKENS", None)
        self.assertEqual(zen_store.load_admin_tokens("/no/such/.env"), set())

    def test_is_admin(self):
        tokens = {"secret1", "secret2"}
        self.assertTrue(zen_store.is_admin("secret1", tokens))
        self.assertFalse(zen_store.is_admin("not-in-set", tokens))
        self.assertFalse(zen_store.is_admin(None, tokens))


class TestDotenv(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".env", delete=False, encoding="utf-8")
        os.environ.pop("ZEN_ADMIN_TOKENS", None)
        os.environ.pop("ZEN_TEST_ONLY_VAR", None)

    def tearDown(self):
        self.tmp.close()
        os.unlink(self.tmp.name)
        os.environ.pop("ZEN_ADMIN_TOKENS", None)
        os.environ.pop("ZEN_TEST_ONLY_VAR", None)

    def test_load_dotenv_sets_environ(self):
        self.tmp.write("ZEN_ADMIN_TOKENS=secretA,secretB\n")
        self.tmp.close()
        zen_store.load_dotenv(self.tmp.name)
        self.assertEqual(os.environ["ZEN_ADMIN_TOKENS"], "secretA,secretB")

    def test_load_dotenv_skips_comments_and_blank_lines(self):
        self.tmp.write("# a comment\n\nZEN_TEST_ONLY_VAR=hello\n")
        self.tmp.close()
        zen_store.load_dotenv(self.tmp.name)
        self.assertEqual(os.environ["ZEN_TEST_ONLY_VAR"], "hello")

    def test_load_dotenv_does_not_override_real_env(self):
        os.environ["ZEN_ADMIN_TOKENS"] = "already-set"
        self.tmp.write("ZEN_ADMIN_TOKENS=from-file\n")
        self.tmp.close()
        zen_store.load_dotenv(self.tmp.name)
        self.assertEqual(os.environ["ZEN_ADMIN_TOKENS"], "already-set")

    def test_load_dotenv_missing_file_is_noop(self):
        zen_store.load_dotenv("/no/such/.env")  # не должно бросать исключение

    def test_load_admin_tokens_reads_dotenv(self):
        self.tmp.write("ZEN_ADMIN_TOKENS=fromenvfile\n")
        self.tmp.close()
        self.assertEqual(
            zen_store.load_admin_tokens(self.tmp.name), {"fromenvfile"})


class TestVotes(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        zen_store.init_schema(self.con)

    def test_first_vote_counts(self):
        r = zen_store.cast_vote(self.con, "narrative::x", "tok1", 1)
        self.assertEqual(r, {"up": 1, "down": 0, "score": 1, "user_vote": 1})

    def test_toggle_off_same_vote(self):
        zen_store.cast_vote(self.con, "narrative::x", "tok1", 1)
        r = zen_store.cast_vote(self.con, "narrative::x", "tok1", 1)
        self.assertEqual(r, {"up": 0, "down": 0, "score": 0, "user_vote": 0})

    def test_switch_vote_direction(self):
        zen_store.cast_vote(self.con, "narrative::x", "tok1", 1)
        r = zen_store.cast_vote(self.con, "narrative::x", "tok1", -1)
        self.assertEqual(r, {"up": 0, "down": 1, "score": -1, "user_vote": -1})

    def test_two_voters(self):
        zen_store.cast_vote(self.con, "narrative::x", "tok1", 1)
        r = zen_store.cast_vote(self.con, "narrative::x", "tok2", 1)
        self.assertEqual(r["up"], 2)
        self.assertEqual(r["score"], 2)


class TestSuggestions(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        zen_store.init_schema(self.con)

    def test_add_suggestion_creates_open_status(self):
        sid = zen_store.add_suggestion(
            self.con, "narrative::x", "tok1", "новый текст", "комментарий")
        row = self.con.execute(
            "SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["status"], "open")
        self.assertEqual(row["text"], "новый текст")
        self.assertEqual(row["author"], "tok1")

    def test_suggestion_promotes_at_threshold(self):
        sid = zen_store.add_suggestion(self.con, "narrative::x", "tok1", "т", "")
        for i in range(zen_store.PROMOTION_THRESHOLD):
            zen_store.cast_suggestion_vote(self.con, sid, f"voter{i}", 1)
        row = self.con.execute(
            "SELECT status FROM suggestions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["status"], "community_approved")

    def test_suggestion_does_not_promote_below_threshold(self):
        sid = zen_store.add_suggestion(self.con, "narrative::x", "tok1", "т", "")
        for i in range(zen_store.PROMOTION_THRESHOLD - 1):
            zen_store.cast_suggestion_vote(self.con, sid, f"voter{i}", 1)
        row = self.con.execute(
            "SELECT status FROM suggestions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["status"], "open")

    def test_admin_status_is_manual_and_not_overridden_by_votes(self):
        sid = zen_store.add_suggestion(self.con, "narrative::x", "tok1", "т", "")
        zen_store.set_suggestion_status(
            self.con, sid, "admin_approved", "admin_tok", "admin_approved")
        for i in range(zen_store.PROMOTION_THRESHOLD):
            zen_store.cast_suggestion_vote(self.con, sid, f"voter{i}", -1)
        row = self.con.execute(
            "SELECT status FROM suggestions WHERE id=?", (sid,)).fetchone()
        self.assertEqual(row["status"], "admin_approved")

    def test_set_suggestion_status_records_history(self):
        sid = zen_store.add_suggestion(self.con, "narrative::x", "tok1", "новый", "")
        zen_store.set_suggestion_status(
            self.con, sid, "admin_approved", "admin_tok", "admin_approved")
        hist = self.con.execute(
            "SELECT * FROM history WHERE uid=?", ("narrative::x",)).fetchall()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["new_text"], "новый")
        self.assertEqual(hist[0]["action"], "admin_approved")


class TestLineData(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        zen_store.init_schema(self.con)

    def test_get_line_data_shape(self):
        zen_store.cast_vote(self.con, "narrative::x", "tok1", 1)
        sid = zen_store.add_suggestion(self.con, "narrative::x", "tok2", "т", "к")
        zen_store.cast_suggestion_vote(self.con, sid, "tok3", 1)
        data = zen_store.get_line_data(self.con, "narrative::x", "tok1")
        self.assertEqual(data["up"], 1)
        self.assertEqual(data["user_vote"], 1)
        self.assertEqual(len(data["suggestions"]), 1)
        self.assertEqual(data["suggestions"][0]["score"], 1)
        self.assertEqual(data["suggestions"][0]["user_vote"], 0)
        self.assertEqual(data["suggestions"][0]["status"], "open")

    def test_suggestion_author_is_display_name_not_raw_token(self):
        # zen_index.html показывает автора предложения в карточке (было --
        # IP, теперь -- ник/"Аноним", как и в истории правок).
        zen_store.get_or_create_identity(self.con, "tok2", "1.2.3.4")
        zen_store.set_nickname(self.con, "tok2", "Вася")
        zen_store.add_suggestion(self.con, "narrative::x", "tok2", "т", "к")
        data = zen_store.get_line_data(self.con, "narrative::x", "tok1")
        self.assertEqual(data["suggestions"][0]["author"], "Вася")

    def test_history_shape_matches_frontend_diff_modal(self):
        # zen_index.html:1438-1449 читает h.old, h.new, h.date, h.ip (ключ
        # переименован в h.actor -- см. Задачу 8, Step 5) из каждой записи.
        sid = zen_store.add_suggestion(self.con, "narrative::x", "tok1", "новый", "")
        zen_store.set_suggestion_status(self.con, sid, "admin_approved", "tok1", "admin_approved")
        data = zen_store.get_line_data(self.con, "narrative::x", "tok1")
        self.assertEqual(len(data["history"]), 1)
        h = data["history"][0]
        self.assertEqual(set(h), {"old", "new", "date", "actor"})
        self.assertEqual(h["new"], "новый")
        self.assertEqual(h["actor"], "Аноним")  # у tok1 нет ника

    def test_history_actor_uses_nickname_when_set(self):
        zen_store.get_or_create_identity(self.con, "tok1", "1.2.3.4")
        zen_store.set_nickname(self.con, "tok1", "Вася")
        sid = zen_store.add_suggestion(self.con, "narrative::x", "tok1", "новый", "")
        zen_store.set_suggestion_status(self.con, sid, "admin_approved", "tok1", "admin_approved")
        data = zen_store.get_line_data(self.con, "narrative::x", "tok1")
        self.assertEqual(data["history"][0]["actor"], "Вася")


if __name__ == "__main__":
    unittest.main()
