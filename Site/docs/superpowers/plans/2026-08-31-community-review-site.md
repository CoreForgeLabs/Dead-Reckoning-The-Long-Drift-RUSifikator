# Сайт вычитки перевода: SQLite, токен-идентичность, синхронизация с GitHub

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Заменить в `zen_server.py` уязвимую модель прав (пароль в клиентском JS,
`is_admin` из тела запроса) и IP-идентичность на SQLite-хранилище с токеном в
cookie, отделить официальный текст (read-only снимок `main`) от предложений
сообщества, и наладить синхронизацию: чтение из `main` веткой pull, запись —
отдельным коммитом в `community-suggestions`.

**Architecture:** Один Python-процесс на стандартной библиотеке (`http.server`,
`sqlite3`, `json`, без внешних зависимостей — как сейчас). Новый модуль
`zen_store.py` инкапсулирует всю работу с SQLite (идентичность, голоса,
предложения, история) и не знает про HTTP. `zen_server.py` остаётся HTTP-слоем
поверх него. Два отдельных CLI-скрипта (`sync_baseline.py`,
`export_community_suggestions.py`) делают git pull / commit+push — они не часть
HTTP-процесса, запускаются вручную из админ-панели через `subprocess`.

**Tech Stack:** Python 3 stdlib (`http.server`, `sqlite3`, `subprocess`, `json`,
`unittest`). Никаких новых зависимостей — проект сейчас без единой сторонней
библиотеки, это сохраняется.

**Spec:** `docs/superpowers/specs/2026-08-31-community-review-site-design.md`

## Global Constraints

- Никаких внешних зависимостей (`pip install`) — только стандартная библиотека.
- `is_admin` никогда не принимается от клиента ни в одном обработчике —
  единственный источник: cookie-токен из `Cookie`-заголовка, сверенный с
  `ZEN_ADMIN_TOKENS` на сервере.
- Файлы в `data/source_baseline/` не пишутся никем, кроме `sync_baseline.py`.
- Регистрация нового токена — не больше одной с одного `created_ip`
  (см. Задачу 1). После создания токен не привязан к IP.
- Порог автопринятия предложения: чистый счёт голосов `+3` →
  `community_approved` (см. спеку).
- Кодировка везде `utf-8`, JSON во всех файлах — `ensure_ascii=False` (уже
  принятая практика в проекте, сохранить).

---

## Task 1: Хранилище — схема, идентичность, права администратора

**Files:**
- Create: `zen_store.py`
- Test: `test_zen_store.py`

**Interfaces:**
- Produces: `zen_store.connect(db_path) -> sqlite3.Connection`,
  `zen_store.init_schema(con)`,
  `zen_store.get_or_create_identity(con, token, ip) -> dict | None`
  (`None` = отказ по лимиту IP; `dict` = `{"token", "nickname", "created_ip"}`),
  `zen_store.set_nickname(con, token, nickname)`,
  `zen_store.load_admin_tokens() -> set[str]` (читает `ZEN_ADMIN_TOKENS` из
  окружения),
  `zen_store.is_admin(token, admin_tokens) -> bool`.

- [ ] **Step 1: Написать падающий тест на схему и создание идентичности**

```python
# test_zen_store.py
import os
import sqlite3
import unittest

import zen_store


class TestSchema(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        zen_store.init_schema(self.con)

    def test_tables_exist(self):
        names = {r[0] for r in self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(
            names,
            {"identities", "votes", "suggestions", "suggestion_votes", "history"},
        )


class TestIdentity(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
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

    def test_second_registration_from_same_ip_is_refused(self):
        first = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        self.assertIsNotNone(first)
        second = zen_store.get_or_create_identity(self.con, None, "1.2.3.4")
        self.assertIsNone(second)

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
        self.assertEqual(zen_store.load_admin_tokens(), set())

    def test_is_admin(self):
        tokens = {"secret1", "secret2"}
        self.assertTrue(zen_store.is_admin("secret1", tokens))
        self.assertFalse(zen_store.is_admin("not-in-set", tokens))
        self.assertFalse(zen_store.is_admin(None, tokens))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить тесты, убедиться что падают**

Run: `python -m unittest test_zen_store -v`
Expected: `ModuleNotFoundError: No module named 'zen_store'` (или ошибки импорта
внутри — файла ещё нет).

- [ ] **Step 3: Написать `zen_store.py` (часть 1 — схема и идентичность)**

```python
# zen_store.py
"""SQLite-хранилище сайта вычитки: идентичность, голоса, предложения, история.

Ничего не знает про HTTP -- zen_server.py вызывает эти функции и сам решает,
что ответить клиенту.
"""
import os
import secrets
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS identities (
    token       TEXT PRIMARY KEY,
    nickname    TEXT,
    created_ip  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_identities_ip ON identities(created_ip);

CREATE TABLE IF NOT EXISTS votes (
    uid     TEXT NOT NULL,
    token   TEXT NOT NULL,
    value   INTEGER NOT NULL,
    ts      TEXT NOT NULL,
    PRIMARY KEY (uid, token)
);

CREATE TABLE IF NOT EXISTS suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT NOT NULL,
    text        TEXT NOT NULL,
    comment     TEXT,
    author      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
);
CREATE INDEX IF NOT EXISTS idx_suggestions_uid ON suggestions(uid);
CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);

CREATE TABLE IF NOT EXISTS suggestion_votes (
    suggestion_id  INTEGER NOT NULL,
    token          TEXT NOT NULL,
    value          INTEGER NOT NULL,
    PRIMARY KEY (suggestion_id, token)
);

CREATE TABLE IF NOT EXISTS history (
    uid       TEXT NOT NULL,
    old_text  TEXT,
    new_text  TEXT NOT NULL,
    actor     TEXT NOT NULL,
    action    TEXT NOT NULL,
    ts        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_uid ON history(uid);
"""

PROMOTION_THRESHOLD = 3


def now():
    return datetime.now(timezone.utc).isoformat()


def connect(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    init_schema(con)
    return con


def init_schema(con):
    con.executescript(SCHEMA)
    con.commit()


def _row_to_identity(row):
    return {"token": row["token"], "nickname": row["nickname"],
            "created_ip": row["created_ip"]}


def get_or_create_identity(con, token, ip):
    """Вернуть личность по токену, создать при отсутствии.

    token=None -- новый посетитель без cookie: создаёт новый токен, если с
    этого IP ещё не было регистраций, иначе возвращает None (отказ).
    token задан, но не найден в базе -- клиент принёс cookie от сброшенной
    базы; создаём личность заново С ЭТИМ ЖЕ токеном без проверки лимита по
    IP (cookie сам по себе уже доказательство существовавшей регистрации)."""
    if token:
        row = con.execute(
            "SELECT * FROM identities WHERE token=?", (token,)).fetchone()
        if row:
            return _row_to_identity(row)
        con.execute(
            "INSERT INTO identities (token, nickname, created_ip, created_at) "
            "VALUES (?,?,?,?)", (token, None, ip, now()))
        con.commit()
        return get_or_create_identity(con, token, ip)

    existing = con.execute(
        "SELECT COUNT(*) FROM identities WHERE created_ip=?", (ip,)).fetchone()[0]
    if existing > 0:
        return None

    new_token = secrets.token_hex(16)
    con.execute(
        "INSERT INTO identities (token, nickname, created_ip, created_at) "
        "VALUES (?,?,?,?)", (new_token, None, ip, now()))
    con.commit()
    return get_or_create_identity(con, new_token, ip)


def set_nickname(con, token, nickname):
    con.execute("UPDATE identities SET nickname=? WHERE token=?",
                (nickname.strip()[:60] or None, token))
    con.commit()


def load_admin_tokens():
    raw = os.environ.get("ZEN_ADMIN_TOKENS", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def is_admin(token, admin_tokens):
    return bool(token) and token in admin_tokens
```

- [ ] **Step 4: Запустить тесты, убедиться что часть 1 проходит**

Run: `python -m unittest test_zen_store.TestSchema test_zen_store.TestIdentity test_zen_store.TestAdmin -v`
Expected: все PASS.

- [ ] **Step 5: Закоммитить**

```bash
git add zen_store.py test_zen_store.py
git commit -m "feat: SQLite-хранилище, идентичность по токену вместо IP"
```

---

## Task 2: Хранилище — голоса, предложения, история, автопринятие

**Files:**
- Modify: `zen_store.py`
- Modify: `test_zen_store.py`

**Interfaces:**
- Consumes: `connect`, `init_schema`, `get_or_create_identity`, `now`,
  `PROMOTION_THRESHOLD` из Задачи 1.
- Produces: `zen_store.cast_vote(con, uid, token, value) -> dict` (текущие
  `up/down/score/user_vote`), `zen_store.add_suggestion(con, uid, token, text,
  comment) -> int` (id предложения), `zen_store.cast_suggestion_vote(con,
  suggestion_id, token, value) -> dict`, `zen_store.set_suggestion_status(con,
  suggestion_id, status, actor, action) -> None`,
  `zen_store.get_line_data(con, uid, viewer_token) -> dict` (агрегат: голоса
  по строке, список предложений с голосами и статусами, история).

- [ ] **Step 1: Написать падающий тест на голоса и предложения**

```python
# добавить в test_zen_store.py

class TestVotes(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
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
```

- [ ] **Step 2: Запустить, убедиться что падают**

Run: `python -m unittest test_zen_store.TestVotes test_zen_store.TestSuggestions test_zen_store.TestLineData -v`
Expected: `AttributeError: module 'zen_store' has no attribute 'cast_vote'`.

- [ ] **Step 3: Дописать `zen_store.py` (часть 2)**

```python
# добавить в конец zen_store.py

def cast_vote(con, uid, token, value):
    """value: 1 или -1. Повторный такой же голос -- снимает голос (toggle)."""
    cur = con.execute(
        "SELECT value FROM votes WHERE uid=? AND token=?", (uid, token)).fetchone()
    if cur and cur["value"] == value:
        con.execute("DELETE FROM votes WHERE uid=? AND token=?", (uid, token))
    else:
        con.execute(
            "INSERT INTO votes (uid, token, value, ts) VALUES (?,?,?,?) "
            "ON CONFLICT(uid, token) DO UPDATE SET value=excluded.value, ts=excluded.ts",
            (uid, token, value, now()))
    con.commit()
    return _vote_summary(con, uid, token)


def _vote_summary(con, uid, viewer_token):
    rows = con.execute("SELECT token, value FROM votes WHERE uid=?", (uid,)).fetchall()
    up = sum(1 for r in rows if r["value"] == 1)
    down = sum(1 for r in rows if r["value"] == -1)
    user_vote = next((r["value"] for r in rows if r["token"] == viewer_token), 0)
    return {"up": up, "down": down, "score": up - down, "user_vote": user_vote}


def add_suggestion(con, uid, token, text, comment):
    cur = con.execute(
        "INSERT INTO suggestions (uid, text, comment, author, created_at, status) "
        "VALUES (?,?,?,?,?, 'open')",
        (uid, text, comment, token, now()))
    con.commit()
    return cur.lastrowid


def _suggestion_vote_summary(con, suggestion_id, viewer_token):
    rows = con.execute(
        "SELECT token, value FROM suggestion_votes WHERE suggestion_id=?",
        (suggestion_id,)).fetchall()
    up = sum(1 for r in rows if r["value"] == 1)
    down = sum(1 for r in rows if r["value"] == -1)
    user_vote = next((r["value"] for r in rows if r["token"] == viewer_token), 0)
    return {"up": up, "down": down, "score": up - down, "user_vote": user_vote}


def cast_suggestion_vote(con, suggestion_id, token, value):
    cur = con.execute(
        "SELECT value FROM suggestion_votes WHERE suggestion_id=? AND token=?",
        (suggestion_id, token)).fetchone()
    if cur and cur["value"] == value:
        con.execute(
            "DELETE FROM suggestion_votes WHERE suggestion_id=? AND token=?",
            (suggestion_id, token))
    else:
        con.execute(
            "INSERT INTO suggestion_votes (suggestion_id, token, value) VALUES (?,?,?) "
            "ON CONFLICT(suggestion_id, token) DO UPDATE SET value=excluded.value",
            (suggestion_id, token, value))
    con.commit()

    summary = _suggestion_vote_summary(con, suggestion_id, token)
    row = con.execute(
        "SELECT uid, status FROM suggestions WHERE id=?", (suggestion_id,)).fetchone()
    # Автопринятие -- только из 'open'; ручной статус админа votes не трогают.
    if row["status"] == "open" and summary["score"] >= PROMOTION_THRESHOLD:
        con.execute(
            "UPDATE suggestions SET status='community_approved' WHERE id=?",
            (suggestion_id,))
        con.commit()
        summary["status"] = "community_approved"
    else:
        summary["status"] = row["status"]
    return summary


def set_suggestion_status(con, suggestion_id, status, actor, action):
    row = con.execute(
        "SELECT uid, text FROM suggestions WHERE id=?", (suggestion_id,)).fetchone()
    con.execute("UPDATE suggestions SET status=? WHERE id=?", (status, suggestion_id))
    con.execute(
        "INSERT INTO history (uid, old_text, new_text, actor, action, ts) "
        "VALUES (?,?,?,?,?,?)",
        (row["uid"], None, row["text"], actor, action, now()))
    con.commit()


def get_line_data(con, uid, viewer_token):
    vote_summary = _vote_summary(con, uid, viewer_token)
    sugg_rows = con.execute(
        "SELECT * FROM suggestions WHERE uid=? ORDER BY id", (uid,)).fetchall()
    suggestions = []
    for s in sugg_rows:
        summary = _suggestion_vote_summary(con, s["id"], viewer_token)
        suggestions.append({
            "id": s["id"], "text": s["text"], "comment": s["comment"],
            "author": s["author"], "created_at": s["created_at"],
            "status": s["status"],
            "up": summary["up"], "down": summary["down"],
            "score": summary["score"], "user_vote": summary["user_vote"],
        })
    history_rows = con.execute(
        "SELECT * FROM history WHERE uid=? ORDER BY ts", (uid,)).fetchall()
    history = []
    for r in history_rows:
        history.append({
            "old": r["old_text"], "new": r["new_text"],
            "date": r["ts"][:16].replace("T", " "),
            "actor": _display_name(con, r["actor"]),
        })
    return {**vote_summary, "suggestions": suggestions, "history": history}


def _display_name(con, token):
    """zen_index.html показывает автора правки в истории (было -- IP,
    теперь -- ник или подпись 'Аноним'; личность больше не IP-строка)."""
    row = con.execute(
        "SELECT nickname FROM identities WHERE token=?", (token,)).fetchone()
    if row and row["nickname"]:
        return row["nickname"]
    return "Аноним"
```

- [ ] **Step 4: Запустить тесты, убедиться что проходят**

Run: `python -m unittest test_zen_store -v`
Expected: все PASS (Задачи 1 и 2 вместе).

- [ ] **Step 5: Закоммитить**

```bash
git add zen_store.py test_zen_store.py
git commit -m "feat: голоса, предложения, автопринятие по порогу +3"
```

---

## Task 3: Миграция старого `zen_state.json`

**Files:**
- Create: `migrate_state.py`
- Test: `test_migrate_state.py`

**Interfaces:**
- Consumes: `zen_store.connect`, `zen_store.init_schema` из Задачи 1/2.
- Produces: `migrate_state.migrate(state_json_path, con) -> dict` (счётчики
  перенесённого: `{"identities": n, "votes": n, "suggestions": n}`).

- [ ] **Step 1: Написать падающий тест**

```python
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
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `python -m unittest test_migrate_state -v`
Expected: `ModuleNotFoundError: No module named 'migrate_state'`

- [ ] **Step 3: Написать `migrate_state.py`**

```python
# migrate_state.py
"""Разовый перенос data/zen_state.json в SQLite. Идентичность старых записей
восстанавливается синтетически: один токен на каждый встреченный IP."""
import json
import os
import sys

import zen_store


def _identity_for_ip(con, ip, cache):
    if ip in cache:
        return cache[ip]
    ident = zen_store.get_or_create_identity(con, None, ip)
    if ident is None:
        # IP уже видели в этом же прогоне миграции под другим синтетическим
        # токеном (лимит "1 регистрация на IP" сработал бы и тут) -- ищем
        # существующую запись напрямую, лимит на перенос данных не действует.
        row = con.execute(
            "SELECT token FROM identities WHERE created_ip=? LIMIT 1",
            (ip,)).fetchone()
        ident = {"token": row["token"]}
    cache[ip] = ident["token"]
    return ident["token"]


def migrate(state_json_path, con):
    counts = {"identities": 0, "votes": 0, "suggestions": 0}
    if not os.path.exists(state_json_path):
        return counts

    with open(state_json_path, encoding="utf-8") as f:
        state = json.load(f)

    ip_to_token = {}

    for uid, v in state.get("votes", {}).items():
        for ip, value in v.get("ip_votes", {}).items():
            token = _identity_for_ip(con, ip, ip_to_token)
            zen_store.cast_vote(con, uid, token, value)
            counts["votes"] += 1

    for uid, suggs in state.get("suggestions", {}).items():
        for s in suggs:
            author_ip = s.get("ip", "0.0.0.0")
            author_token = _identity_for_ip(con, author_ip, ip_to_token)
            sid = zen_store.add_suggestion(
                con, uid, author_token, s.get("text", ""), s.get("comment", ""))
            counts["suggestions"] += 1
            for voter_ip, value in s.get("votes", {}).items():
                voter_token = _identity_for_ip(con, voter_ip, ip_to_token)
                zen_store.cast_suggestion_vote(con, sid, voter_token, value)

    for uid, rev in state.get("reviews", {}).items():
        ip = rev.get("ip", "0.0.0.0")
        if ip.startswith("Admin ("):
            ip = ip[len("Admin ("):-1]
        _identity_for_ip(con, ip, ip_to_token)

    counts["identities"] = len(ip_to_token)
    return counts


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    state_path = os.path.join(base_dir, "data", "zen_state.json")
    db_path = os.path.join(base_dir, "zen_review.db")
    con = zen_store.connect(db_path)
    counts = migrate(state_path, con)
    print("Перенесено: %s" % counts)
    if counts["identities"] or counts["votes"] or counts["suggestions"]:
        os.rename(state_path, state_path + ".migrated")
        print("data/zen_state.json -> data/zen_state.json.migrated")


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Запустить тесты**

Run: `python -m unittest test_migrate_state -v`
Expected: все PASS.

- [ ] **Step 5: Закоммитить**

```bash
git add migrate_state.py test_migrate_state.py
git commit -m "feat: миграция zen_state.json в SQLite"
```

---

## Task 4: Синхронизация с `main` — `sync_baseline.py`

**Files:**
- Create: `sync_baseline.py`
- Test: `test_sync_baseline.py`

**Interfaces:**
- Produces: `sync_baseline.ensure_remote(repo_dir, remote_url) -> None`,
  `sync_baseline.pull(repo_dir) -> str` (короткий SHA после pull),
  `sync_baseline.build_baseline(repo_data_dir, src_en_dir, out_dir) -> dict`
  (счётчики по компонентам).

**Замечание по данным:** только `narrative.json`, `labels.json`,
`epilogue.json` нуждаются в отдельном английском тексте — берётся из
`src_en/` соседнего проекта-переводчика (`F:\DEV2\research_game\Dead
Reckoning Russifier\src_en`), потому что в публичной поставке английского нет,
только русский. `extra.json` и `remap_by_en.json` уже ключуются английским
текстом самим по себе (см. спеку прошлой сессии — «адресация по английскому»)
— для них EN = ключ. `gdc_literal_fixes.json` — список `{file, en, ru}`.

- [ ] **Step 1: Написать падающий тест на `build_baseline`**

```python
# test_sync_baseline.py
import json
import os
import shutil
import tempfile
import unittest

import sync_baseline


class TestBuildBaseline(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.repo_data = os.path.join(self.tmp, "repo_data")
        self.src_en = os.path.join(self.tmp, "src_en")
        self.out = os.path.join(self.tmp, "out")
        os.makedirs(self.repo_data)
        os.makedirs(self.src_en)

        def write(d, name, obj):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False)

        write(self.repo_data, "narrative.json", {"k1": "русский текст"})
        write(self.src_en, "narrative.json", {"k1": "english text"})
        write(self.repo_data, "labels.json", {"k2": "метка"})
        write(self.src_en, "labels.json", {"k2": "label"})
        write(self.repo_data, "epilogue.json", {"k3": "эпилог"})
        write(self.src_en, "epilogue.json", {"k3": "epilogue"})
        write(self.repo_data, "extra.json", {"literal one": "буквально один"})
        write(self.repo_data, "remap_by_en.json", {"UI STRING": "СТРОКА UI"})
        write(self.repo_data, "gdc_literal_fixes.json", [
            {"file": "a.gdc", "en": "Hello", "ru": "Привет"},
        ])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_narrative_gets_real_english(self):
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "narrative.json"), encoding="utf-8") as f:
            en = json.load(f)
        self.assertEqual(en["k1"], "english text")
        with open(os.path.join(self.out, "ru", "narrative.json"), encoding="utf-8") as f:
            ru = json.load(f)
        self.assertEqual(ru["k1"], "русский текст")

    def test_extra_english_is_key(self):
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "extra.json"), encoding="utf-8") as f:
            en = json.load(f)
        self.assertEqual(en["literal one"], "literal one")

    def test_remap_english_is_key(self):
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "remap_by_en.json"), encoding="utf-8") as f:
            en = json.load(f)
        self.assertEqual(en["UI STRING"], "UI STRING")

    def test_gdc_literal_fixes_split_into_en_ru(self):
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "gdc_literal_fixes.json"), encoding="utf-8") as f:
            en = json.load(f)
        with open(os.path.join(self.out, "ru", "gdc_literal_fixes.json"), encoding="utf-8") as f:
            ru = json.load(f)
        self.assertEqual(en["Hello"], "Hello")
        self.assertEqual(ru["Hello"], "Привет")

    def test_missing_src_en_key_falls_back_to_key(self):
        # к narrative.json добавлен ключ, которого нет в src_en
        with open(os.path.join(self.repo_data, "narrative.json"), "w", encoding="utf-8") as f:
            json.dump({"k1": "текст", "k_new": "новый текст"}, f, ensure_ascii=False)
        sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        with open(os.path.join(self.out, "en", "narrative.json"), encoding="utf-8") as f:
            en = json.load(f)
        self.assertEqual(en["k_new"], "k_new")

    def test_counts_returned(self):
        counts = sync_baseline.build_baseline(self.repo_data, self.src_en, self.out)
        self.assertEqual(counts["narrative"], 1)
        self.assertEqual(counts["gdc_literal_fixes"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `python -m unittest test_sync_baseline -v`
Expected: `ModuleNotFoundError: No module named 'sync_baseline'`

- [ ] **Step 3: Написать `sync_baseline.py`**

```python
# sync_baseline.py
"""Снимок main из GitHub -> data/source_baseline/{en,ru}/*.json.

repos/dead-reckoning -- локальный клон CoreForgeLabs/Dead-Reckoning-The-Long-
Drift-RUSifikator. Английский текст для narrative/labels/epilogue берётся
отдельно из соседнего проекта-переводчика (публичная поставка содержит
только русский)."""
import json
import os
import subprocess
import sys

REMOTE_URL = "https://github.com/CoreForgeLabs/Dead-Reckoning-The-Long-Drift-RUSifikator.git"
SRC_EN_ENRICHED = ("narrative", "labels", "epilogue")   # берут EN из src_en/
SRC_EN_SELF_KEYED = ("extra", "remap_by_en")             # EN == ключ


def ensure_remote(repo_dir, remote_url=REMOTE_URL):
    result = subprocess.run(
        ["git", "-C", repo_dir, "remote", "get-url", "origin"],
        capture_output=True, text=True)
    if result.returncode != 0:
        subprocess.run(
            ["git", "-C", repo_dir, "remote", "add", "origin", remote_url],
            check=True)


def pull(repo_dir):
    subprocess.run(["git", "-C", repo_dir, "fetch", "origin", "main"], check=True)
    subprocess.run(
        ["git", "-C", repo_dir, "checkout", "main"], check=True,
        capture_output=True)
    subprocess.run(
        ["git", "-C", repo_dir, "reset", "--hard", "origin/main"], check=True)
    sha = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True).stdout.strip()
    return sha


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def build_baseline(repo_data_dir, src_en_dir, out_dir):
    counts = {}

    for name in SRC_EN_ENRICHED:
        ru = _load(os.path.join(repo_data_dir, "%s.json" % name))
        src_en_path = os.path.join(src_en_dir, "%s.json" % name)
        en_source = _load(src_en_path) if os.path.exists(src_en_path) else {}
        en = {k: en_source.get(k, k) for k in ru}
        _dump(os.path.join(out_dir, "en", "%s.json" % name), en)
        _dump(os.path.join(out_dir, "ru", "%s.json" % name), ru)
        counts[name] = len(ru)

    for name in SRC_EN_SELF_KEYED:
        ru = _load(os.path.join(repo_data_dir, "%s.json" % name))
        en = {k: k for k in ru}
        _dump(os.path.join(out_dir, "en", "%s.json" % name), en)
        _dump(os.path.join(out_dir, "ru", "%s.json" % name), ru)
        counts[name] = len(ru)

    gdc_path = os.path.join(repo_data_dir, "gdc_literal_fixes.json")
    if os.path.exists(gdc_path):
        items = _load(gdc_path)
        gdc_en = {it["en"]: it["en"] for it in items}
        gdc_ru = {it["en"]: it["ru"] for it in items}
        _dump(os.path.join(out_dir, "en", "gdc_literal_fixes.json"), gdc_en)
        _dump(os.path.join(out_dir, "ru", "gdc_literal_fixes.json"), gdc_ru)
        counts["gdc_literal_fixes"] = len(items)

    return counts


def main():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = os.path.join(base_dir, "repos", "dead-reckoning")
    repo_data_dir = os.path.join(repo_dir, "rusifikator", "data")
    src_en_dir = r"F:\DEV2\research_game\Dead Reckoning Russifier\src_en"
    out_dir = os.path.join(base_dir, "data", "source_baseline")

    ensure_remote(repo_dir)
    sha = pull(repo_dir)
    counts = build_baseline(repo_data_dir, src_en_dir, out_dir)
    print("main @ %s -> %s" % (sha, counts))


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Запустить тесты**

Run: `python -m unittest test_sync_baseline -v`
Expected: все PASS.

- [ ] **Step 5: Прогнать `ensure_remote`/`pull` руками против реального репозитория**

```bash
python -c "import sync_baseline; sync_baseline.ensure_remote('repos/dead-reckoning'); print(sync_baseline.pull('repos/dead-reckoning'))"
```
Expected: печатает короткий SHA, `git -C repos/dead-reckoning remote -v`
показывает `origin` на `CoreForgeLabs/Dead-Reckoning-The-Long-Drift-RUSifikator`.

- [ ] **Step 6: Закоммитить**

```bash
git add sync_baseline.py test_sync_baseline.py
git commit -m "feat: синхронизация официального текста из main GitHub"
```

---

## Task 5: Выгрузка предложений — `export_community_suggestions.py`

**Files:**
- Create: `export_community_suggestions.py`
- Test: `test_export_community_suggestions.py`

**Interfaces:**
- Consumes: `zen_store.connect` из Задачи 1.
- Produces:
  `export_community_suggestions.collect_accepted(con) -> dict[str, dict[str,
  str]]` (компонент -> `{key: text}`, только `community_approved` +
  `admin_approved`),
  `export_community_suggestions.commit_branch(repo_dir, patch, branch_name)
  -> bool` (`False`, если менять нечего).

- [ ] **Step 1: Написать падающий тест**

```python
# test_export_community_suggestions.py
import sqlite3
import subprocess
import tempfile
import shutil
import os
import json
import unittest

import zen_store
import export_community_suggestions as ecs


class TestCollectAccepted(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        zen_store.init_schema(self.con)

    def test_only_accepted_statuses_included(self):
        sid1 = zen_store.add_suggestion(self.con, "narrative::x", "t1", "принято", "")
        zen_store.set_suggestion_status(self.con, sid1, "admin_approved", "admin", "admin_approved")
        sid2 = zen_store.add_suggestion(self.con, "narrative::y", "t1", "открыто", "")
        patch = ecs.collect_accepted(self.con)
        self.assertEqual(patch["narrative"], {"x": "принято"})

    def test_component_split_by_uid_prefix(self):
        sid = zen_store.add_suggestion(self.con, "labels::z", "t1", "метка", "")
        zen_store.set_suggestion_status(self.con, sid, "community_approved", "t2", "community_approved")
        patch = ecs.collect_accepted(self.con)
        self.assertEqual(patch, {"labels": {"z": "метка"}})

    def test_empty_when_nothing_accepted(self):
        zen_store.add_suggestion(self.con, "narrative::x", "t1", "открыто", "")
        patch = ecs.collect_accepted(self.con)
        self.assertEqual(patch, {})


class TestCommitBranch(unittest.TestCase):
    def setUp(self):
        self.repo_dir = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.repo_dir], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "config", "user.name", "t"], check=True)
        os.makedirs(os.path.join(self.repo_dir, "rusifikator", "data"))
        with open(os.path.join(self.repo_dir, "rusifikator", "data", "narrative.json"), "w", encoding="utf-8") as f:
            json.dump({"x": "старый текст"}, f, ensure_ascii=False)
        subprocess.run(["git", "-C", self.repo_dir, "add", "."], check=True)
        subprocess.run(["git", "-C", self.repo_dir, "commit", "-q", "-m", "init"], check=True)

    def tearDown(self):
        shutil.rmtree(self.repo_dir)

    def test_commit_creates_branch_with_patched_file(self):
        patch = {"narrative": {"x": "новый текст от сообщества"}}
        changed = ecs.commit_branch(self.repo_dir, patch, "community-suggestions")
        self.assertTrue(changed)

        show = subprocess.run(
            ["git", "-C", self.repo_dir, "show",
             "community-suggestions:rusifikator/data/narrative.json"],
            capture_output=True, text=True, check=True).stdout
        self.assertEqual(json.loads(show), {"x": "новый текст от сообщества"})

        branch = subprocess.run(
            ["git", "-C", self.repo_dir, "branch", "--show-current"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(branch, "main")  # commit_branch не должен оставлять нас на ветке

    def test_commit_returns_false_when_nothing_changed(self):
        patch = {"narrative": {"x": "старый текст"}}
        changed = ecs.commit_branch(self.repo_dir, patch, "community-suggestions")
        self.assertFalse(changed)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `python -m unittest test_export_community_suggestions -v`
Expected: `ModuleNotFoundError`

- [ ] **Step 3: Написать `export_community_suggestions.py`**

```python
# export_community_suggestions.py
"""Собрать принятые сообществом правки и закоммитить их отдельной веткой.

main не трогается никогда -- слияние community-suggestions в main смотрит и
делает человек, через пайплайн проекта-переводчика."""
import json
import os
import subprocess
import sys

BRANCH = "community-suggestions"
ACCEPTED_STATUSES = ("community_approved", "admin_approved")


def collect_accepted(con):
    rows = con.execute(
        "SELECT uid, text FROM suggestions WHERE status IN (?,?) "
        "ORDER BY id", ACCEPTED_STATUSES).fetchall()
    patch = {}
    for r in rows:
        comp, key = r["uid"].split("::", 1)
        patch.setdefault(comp, {})[key] = r["text"]
    return patch


def commit_branch(repo_dir, patch, branch_name=BRANCH):
    if not patch:
        return False

    starting_branch = subprocess.run(
        ["git", "-C", repo_dir, "branch", "--show-current"],
        capture_output=True, text=True, check=True).stdout.strip()

    exists = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--verify", branch_name],
        capture_output=True).returncode == 0
    if exists:
        subprocess.run(["git", "-C", repo_dir, "checkout", branch_name], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", repo_dir, "reset", "--hard", "main"], check=True,
                       capture_output=True)
    else:
        subprocess.run(["git", "-C", repo_dir, "checkout", "-b", branch_name], check=True,
                       capture_output=True)

    changed_files = []
    for comp, kv in patch.items():
        path = os.path.join(repo_dir, "rusifikator", "data", "%s.json" % comp)
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        data.update(kv)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        changed_files.append(path)

    subprocess.run(["git", "-C", repo_dir, "add"] + changed_files, check=True)
    diff = subprocess.run(
        ["git", "-C", repo_dir, "diff", "--cached", "--quiet"])
    has_changes = diff.returncode != 0
    if has_changes:
        n = sum(len(kv) for kv in patch.values())
        subprocess.run(
            ["git", "-C", repo_dir, "commit", "-q", "-m",
             "Правки сообщества с сайта вычитки (%d строк)" % n], check=True)

    subprocess.run(
        ["git", "-C", repo_dir, "checkout", starting_branch or "main"], check=True,
        capture_output=True)
    return has_changes


def main():
    import zen_store
    base_dir = os.path.dirname(os.path.abspath(__file__))
    con = zen_store.connect(os.path.join(base_dir, "zen_review.db"))
    repo_dir = os.path.join(base_dir, "repos", "dead-reckoning")

    patch = collect_accepted(con)
    total = sum(len(kv) for kv in patch.values())
    print("к выгрузке: %d строк в %d файлах" % (total, len(patch)))
    changed = commit_branch(repo_dir, patch)
    if not changed:
        print("нечего коммитить (нет новых принятых правок с прошлой выгрузки)")
        return
    if "--push" in sys.argv:
        subprocess.run(
            ["git", "-C", repo_dir, "push", "-u", "origin", BRANCH], check=True)
        print("запушено в origin/%s" % BRANCH)
    else:
        print("закоммичено локально в %s. Для отправки на GitHub: --push" % BRANCH)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Запустить тесты**

Run: `python -m unittest test_export_community_suggestions -v`
Expected: все PASS.

- [ ] **Step 5: Закоммитить**

```bash
git add export_community_suggestions.py test_export_community_suggestions.py
git commit -m "feat: выгрузка принятых правок в ветку community-suggestions"
```

---

## Task 6: `zen_server.py` — идентичность, cookie, вход администратора

**Files:**
- Modify: `zen_server.py`

**Interfaces:**
- Consumes: `zen_store.connect`, `get_or_create_identity`, `set_nickname`,
  `load_admin_tokens`, `is_admin` из Задачи 1.
- Produces: обработчик выставляет `self.identity` (dict из
  `get_or_create_identity`) и `self.is_admin_request` (bool) на каждый запрос
  до вызова конкретного маршрута; новые маршруты `POST /api/set_nickname`,
  `POST /api/admin_login`, `POST /api/admin_logout`.

- [ ] **Step 1: Заменить импорты и константы в начале файла**

Modify: `zen_server.py:1-18` — после существующих импортов и констант
добавить:

```python
import zen_store

DB_PATH = os.path.join(BASE_DIR, "zen_review.db")
BASELINE_DIR = os.path.join(BASE_DIR, "data", "source_baseline")
ADMIN_TOKENS = zen_store.load_admin_tokens()
```

- [ ] **Step 2: Добавить работу с cookie и общую точку входа идентичности**

Добавить в класс `ZenHandler` (после существующего `get_client_ip`, перед
`do_GET`):

```python
    def get_cookie_token(self):
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("zen_token="):
                return part[len("zen_token="):]
        return None

    def resolve_identity(self):
        """Вызывается в начале do_GET/do_POST. Устанавливает self.identity и
        self.is_admin_request. Возвращает False (и уже отправляет 429), если
        новому посетителю отказано в регистрации по лимиту IP."""
        con = zen_store.connect(DB_PATH)
        token = self.get_cookie_token()
        ip = self.get_client_ip()
        identity = zen_store.get_or_create_identity(con, token, ip)
        con.close()
        if identity is None:
            body = json.dumps({
                "status": "error",
                "message": "С этого адреса уже зарегистрирован участник. "
                           "Если это ваш браузер без cookie -- проверьте, не "
                           "заблокированы ли cookies.",
            }).encode("utf-8")
            self.send_response(429)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False

        self.identity = identity
        self.is_admin_request = zen_store.is_admin(identity["token"], ADMIN_TOKENS)
        if token != identity["token"]:
            self._new_cookie = identity["token"]
        else:
            self._new_cookie = None
        return True

    def send_identity_cookie_if_new(self):
        if getattr(self, "_new_cookie", None):
            self.send_header(
                "Set-Cookie",
                "zen_token=%s; Path=/; Max-Age=31536000; SameSite=Lax" % self._new_cookie)
```

- [ ] **Step 3: Вызывать `resolve_identity()` в начале `do_GET` и `do_POST`**

Modify: `zen_server.py` — начало `do_GET` (после `client_ip =
self.get_client_ip()`, эта строка остаётся для существующих маршрутов вроде
`/api/backup`, которые IP не используют для идентичности):

```python
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        client_ip = self.get_client_ip()

        if parsed.path.startswith("/api/") and not self.resolve_identity():
            return
```

Начало `do_POST` (сразу после чтения `body`/`req`, до диспетчеризации по
`parsed.path`):

```python
    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        client_ip = self.get_client_ip()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        req = json.loads(body.decode("utf-8")) if body else {}

        if not self.resolve_identity():
            return
```

- [ ] **Step 4: Отправлять cookie на каждом успешном JSON-ответе**

Существующие ответы вида:
```python
self.send_response(HTTPStatus.OK)
self.send_header("Content-Type", "application/json; charset=utf-8")
self.send_header("Content-Length", str(len(resp)))
self.end_headers()
```
— перед каждым `self.end_headers()` в JSON-ответах `/api/*` добавить строку
`self.send_identity_cookie_if_new()`. Это механическая правка во всех местах,
где такой блок встречается в файле (используется regex-замена, чтобы не
пропустить ни одного места):

```bash
python -c "
import re
p = 'zen_server.py'
s = open(p, encoding='utf-8').read()
pattern = re.compile(
    r'(self\.send_header\(\"Content-Type\", \"application/json; charset=utf-8\"\)\n'
    r'\s+self\.send_header\(\"Content-Length\", str\(len\(resp\)\)\)\n)(\s+)(self\.end_headers\(\))'
)
s2, n = pattern.subn(r'\1\2self.send_identity_cookie_if_new()\n\2\3', s)
print('заменено мест:', n)
open(p, 'w', encoding='utf-8').write(s2)
"
```

- [ ] **Step 5: Добавить `/api/set_nickname`, `/api/admin_login`,
  `/api/admin_logout` в `do_POST`**

Modify: `zen_server.py` — добавить перед строкой `if parsed.path ==
"/api/save":`:

```python
        if parsed.path == "/api/set_nickname":
            nickname = (req.get("nickname") or "").strip()[:60]
            con = zen_store.connect(DB_PATH)
            zen_store.set_nickname(con, self.identity["token"], nickname)
            con.close()
            resp = json.dumps({"status": "ok", "nickname": nickname or None}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.send_identity_cookie_if_new()
            self.end_headers()
            self.wfile.write(resp)
            return

        if parsed.path == "/api/admin_login":
            secret = (req.get("secret") or "").strip()
            if secret in ADMIN_TOKENS:
                self._new_cookie = secret
                con = zen_store.connect(DB_PATH)
                zen_store.get_or_create_identity(con, secret, client_ip)
                con.close()
                resp = json.dumps({"status": "ok", "is_admin": True}).encode("utf-8")
                code = HTTPStatus.OK
            else:
                resp = json.dumps({"status": "error", "message": "Неверный код"}).encode("utf-8")
                code = HTTPStatus.FORBIDDEN
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.send_identity_cookie_if_new()
            self.end_headers()
            self.wfile.write(resp)
            return

        if parsed.path == "/api/admin_logout":
            resp = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.send_header("Set-Cookie", "zen_token=; Path=/; Max-Age=0")
            self.end_headers()
            self.wfile.write(resp)
            return
```

`/api/admin_login` сознательно **меняет cookie-токен запрашивающего на сам
секрет** (`secret in ADMIN_TOKENS`) — секрет и есть токен администратора,
отдельного шага "создать личность, потом как-то её пометить админом" не
нужно: принадлежность к `ADMIN_TOKENS` проверяется на каждом запросе заново
через `zen_store.is_admin`.

- [ ] **Step 6: Проверить вручную**

```bash
python zen_server.py &
sleep 1
curl -i -c /tmp/c.txt http://localhost:8090/api/data | head -5
# ожидается: Set-Cookie: zen_token=... в заголовках, HTTP 200

curl -s -b /tmp/c.txt http://localhost:8090/api/data > /dev/null
curl -s -b /tmp/c.txt -X POST http://localhost:8090/api/data
# второй запрос НЕ должен снова получить Set-Cookie (тот же токен из cookie)

kill %1
```
Expected: первый запрос ставит cookie, второй нет; регистрация с одного IP
без cookie второй раз подряд (`curl` без `-b`/`-c`, то есть без сохранения
cookie) отвечает `429`.

- [ ] **Step 7: Закоммитить**

```bash
git add zen_server.py
git commit -m "feat: идентичность по cookie-токену, серверная проверка админ-прав"
```

---

## Task 7: `zen_server.py` — переписать `/api/data` и голосование/предложения на `zen_store`, убрать уязвимые маршруты

**Files:**
- Modify: `zen_server.py`

**Interfaces:**
- Consumes: `zen_store.get_line_data`, `cast_vote`, `add_suggestion`,
  `cast_suggestion_vote`, `set_suggestion_status` из Задач 1–2;
  `BASELINE_DIR` из Задачи 6.

- [ ] **Step 1: Переписать `load_all_data` на чтение из `BASELINE_DIR` и
  `zen_store`**

Modify: `zen_server.py` — заменить функцию `load_all_data` (текущие строки
~99–174, читающие `LOCALES_RU_DIR`/`state.json`) на:

```python
def load_all_data(viewer_token):
    con = zen_store.connect(DB_PATH)
    all_items = []
    item_id = 1
    for comp in COMPONENTS_META:
        ru_path = os.path.join(BASELINE_DIR, "ru", comp["file"])
        en_path = os.path.join(BASELINE_DIR, "en", comp["file"])
        if not os.path.exists(ru_path):
            continue
        with open(ru_path, encoding="utf-8") as f:
            ru_data = json.load(f)
        en_data = {}
        if os.path.exists(en_path):
            with open(en_path, encoding="utf-8") as f:
                en_data = json.load(f)

        for k, ru_val in ru_data.items():
            en_val = en_data.get(k, k)
            uid = f"{comp['slug']}::{k}"
            line = zen_store.get_line_data(con, uid, viewer_token)

            truncated = is_likely_truncated(k, en_val, ru_val)
            clause = is_generator_clause(k, en_val)
            color_tagged = has_color_tags(ru_val) or has_color_tags(en_val)
            event_id, event_role = parse_event_info(k)
            qa_warnings = validate_translation_variables(en_val, ru_val)

            statuses = {s["status"] for s in line["suggestions"]}
            if "admin_approved" in statuses:
                roll_up_status = "admin_approved"
            elif "community_approved" in statuses:
                roll_up_status = "community_approved"
            elif line["suggestions"] or line["up"] or line["down"]:
                roll_up_status = "in_review"
            else:
                roll_up_status = "pending"

            all_items.append({
                "id": item_id, "uid": uid, "comp": comp["slug"],
                "comp_name": comp["name"], "key": k,
                "ru": ru_val, "en": en_val,
                "status": roll_up_status,
                "score": line["score"], "up": line["up"], "down": line["down"],
                "user_vote": line["user_vote"],
                "suggestions": line["suggestions"], "history": line["history"],
                "qa_warnings": qa_warnings, "truncated": truncated,
                "clause": clause, "color_tagged": color_tagged,
                "event_id": event_id, "event_role": event_role,
            })
            item_id += 1
    con.close()
    return all_items
```

Удалить старые функции, которые эта версия больше не использует:
`load_state`, `save_state` — **не удалять**, `migrate_state.py` и
`export_community_suggestions.py` их не используют (они берут `zen_store`
напрямую), но `/api/backup` (Задача 7, Step 3 ниже) их пока трогает — держать
до Step 3.

- [ ] **Step 2: Обновить вызов `load_all_data` в `do_GET`**

Modify: `zen_server.py` — в `do_GET`, маршрут `/api/data`:

```python
        if parsed.path == "/api/data":
            data = load_all_data(self.identity["token"])
            data_with_identity = {
                "items": data,
                "identity": {
                    "nickname": self.identity["nickname"],
                    "is_admin": self.is_admin_request,
                },
            }
            payload = json.dumps(data_with_identity, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_identity_cookie_if_new()
            self.end_headers()
            self.wfile.write(payload)
            return
```

(Ответ меняет форму с плоского списка на `{"items": [...], "identity":
{...}}` — фронтенд обновляется в Задаче 8, Step 1, синхронно с этим шагом.)

- [ ] **Step 3: Обновить `/api/backup` — читать из `BASELINE_DIR` и
  `zen_review.db` вместо `LOCALES_RU_DIR`/`STATE_FILE`**

Modify: `zen_server.py` — маршрут `/api/backup`:

```python
        if parsed.path == "/api/backup":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for comp in COMPONENTS_META:
                    ru_p = os.path.join(BASELINE_DIR, "ru", comp["file"])
                    if os.path.exists(ru_p):
                        zip_file.write(ru_p, f"baseline_ru/{comp['file']}")
                if os.path.exists(DB_PATH):
                    zip_file.write(DB_PATH, "zen_review.db")
            buf.seek(0)
            zip_data = buf.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f"attachment; filename=DeadReckoning_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
            self.send_header("Content-Length", str(len(zip_data)))
            self.end_headers()
            self.wfile.write(zip_data)
            return
```

- [ ] **Step 4: Удалить `/api/save` и `/api/review` целиком**

Modify: `zen_server.py` — удалить оба блока `if parsed.path == "/api/save":`
и `if parsed.path == "/api/review":` вместе с их телами (это была прямая
запись в файл на диске и приём `is_admin` от клиента — заменяются задачами
ниже). Также удалить теперь неиспользуемые `load_state`/`save_state` и
константу `STATE_FILE`, `LOCALES_RU_DIR`, `LOCALES_EN_DIR` (заменены на
`BASELINE_DIR`).

- [ ] **Step 5: Переписать `/api/vote` на `zen_store.cast_vote`**

```python
        if parsed.path == "/api/vote":
            uid = req.get("uid")
            vote_val = int(req.get("vote", 0))
            con = zen_store.connect(DB_PATH)
            summary = zen_store.cast_vote(con, uid, self.identity["token"], vote_val)
            con.close()
            resp = json.dumps({"status": "ok", **summary}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.send_identity_cookie_if_new()
            self.end_headers()
            self.wfile.write(resp)
            return
```

- [ ] **Step 6: Переписать `/api/suggest` на `zen_store.add_suggestion`**

```python
        if parsed.path == "/api/suggest":
            uid = req.get("uid")
            text = (req.get("text") or "").strip()
            comment = (req.get("comment") or "").strip()
            if text:
                con = zen_store.connect(DB_PATH)
                zen_store.add_suggestion(con, uid, self.identity["token"], text, comment)
                con.close()
            resp = json.dumps({"status": "ok", "message": "Suggestion added"}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.send_identity_cookie_if_new()
            self.end_headers()
            self.wfile.write(resp)
            return
```

- [ ] **Step 7: Переписать `/api/vote_suggestion` на
  `zen_store.cast_suggestion_vote`**

```python
        if parsed.path == "/api/vote_suggestion":
            sug_id = int(req.get("sug_id"))
            vote_val = int(req.get("vote", 1))
            con = zen_store.connect(DB_PATH)
            summary = zen_store.cast_suggestion_vote(
                con, sug_id, self.identity["token"], vote_val)
            con.close()
            resp = json.dumps({"status": "ok", **summary}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.send_identity_cookie_if_new()
            self.end_headers()
            self.wfile.write(resp)
            return
```

- [ ] **Step 8: Переписать `/api/accept_suggestion` — теперь только для
  администратора**

```python
        if parsed.path == "/api/accept_suggestion":
            if not self.is_admin_request:
                resp = json.dumps({"status": "error", "message": "Только для администратора"}).encode("utf-8")
                self.send_response(HTTPStatus.FORBIDDEN)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return
            sug_id = int(req.get("sug_id"))
            con = zen_store.connect(DB_PATH)
            zen_store.set_suggestion_status(
                con, sug_id, "admin_approved", self.identity["token"], "admin_approved")
            con.close()
            resp = json.dumps({"status": "ok", "new_status": "admin_approved"}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
```

- [ ] **Step 9: Добавить `/api/admin_approve_line` — одобрить строку как есть,
  без отдельного предложения**

Кнопка «Строка и так хороша» в интерфейсе администратора (Задача 8) вызывает
этот маршрут, когда правок не требуется, но строку стоит явно пометить
проверенной:

```python
        if parsed.path == "/api/admin_approve_line":
            if not self.is_admin_request:
                resp = json.dumps({"status": "error", "message": "Только для администратора"}).encode("utf-8")
                self.send_response(HTTPStatus.FORBIDDEN)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
                return
            uid = req.get("uid")
            comp_slug, key = uid.split("::", 1)
            meta = next((c for c in COMPONENTS_META if c["slug"] == comp_slug), None)
            with open(os.path.join(BASELINE_DIR, "ru", meta["file"]), encoding="utf-8") as f:
                current_text = json.load(f).get(key, "")
            con = zen_store.connect(DB_PATH)
            sid = zen_store.add_suggestion(
                con, uid, self.identity["token"], current_text,
                "одобрено администратором как есть")
            zen_store.set_suggestion_status(
                con, sid, "admin_approved", self.identity["token"], "admin_approved")
            con.close()
            resp = json.dumps({"status": "ok"}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(resp)))
            self.end_headers()
            self.wfile.write(resp)
            return
```

- [ ] **Step 10: Проверить вручную полный цикл**

```bash
python migrate_state.py       # если ещё не выполнялось
python sync_baseline.py       # наполняет data/source_baseline
python zen_server.py &
sleep 1

curl -s -c /tmp/c.txt http://localhost:8090/api/data | python -m json.tool | head -20

UID=$(curl -s -b /tmp/c.txt http://localhost:8090/api/data | python -c "import json,sys; print(json.load(sys.stdin)['items'][0]['uid'])")

curl -s -b /tmp/c.txt -X POST http://localhost:8090/api/suggest \
  -d "{\"uid\": \"$UID\", \"text\": \"тестовое предложение\", \"comment\": \"\"}"

curl -s -b /tmp/c.txt -X POST http://localhost:8090/api/vote \
  -d "{\"uid\": \"$UID\", \"vote\": 1}"

# без входа как админ -- запрет
curl -s -b /tmp/c.txt -X POST http://localhost:8090/api/accept_suggestion \
  -d '{"sug_id": 1}'
# ожидается: {"status": "error", "message": "Только для администратора"}, HTTP 403

kill %1
```
Expected: предложение появляется в `/api/data`, голос учитывается,
`accept_suggestion` без прав администратора отвечает `403`.

- [ ] **Step 11: Закоммитить**

```bash
git add zen_server.py
git commit -m "feat: голоса/предложения через SQLite, accept_suggestion требует прав администратора"
```

---

## Task 8: Фронтенд — убрать клиентский пароль, подключить cookie-идентичность

**Files:**
- Modify: `zen_index.html`

**Interfaces:**
- Consumes: новую форму ответа `GET /api/data` → `{"items": [...],
  "identity": {"nickname", "is_admin"}}` из Задачи 7.

- [ ] **Step 1: Обновить разбор ответа `/api/data`**

Modify: `zen_index.html:627-637` (функция `init`), заменить:

```js
        const res = await fetch('/api/data');
        allData = await res.json();
```
на:
```js
        const res = await fetch('/api/data');
        const payload = await res.json();
        allData = payload.items;
        isAdmin = payload.identity.is_admin;
        currentNickname = payload.identity.nickname;
```

- [ ] **Step 2: Убрать клиентский пароль и localStorage-флаг администратора**

Modify: `zen_index.html:552` — заменить
```js
    let isAdmin = localStorage.getItem('dr_is_admin') === '1'; // Admin role persistence
```
на:
```js
    let isAdmin = false;          // приходит с сервера при каждой загрузке /api/data
    let currentNickname = null;   // приходит с сервера
```

Modify: `zen_index.html:639-656` (функция `toggleAdminRole`), заменить целиком
на:

```js
    async function toggleAdminRole() {
      if (isAdmin) {
        await fetch('/api/admin_logout', { method: 'POST' });
        isAdmin = false;
        showToast('👥 Переключено в режим участника сообщества');
        updateAdminUI();
        applyFilter(true);
        return;
      }
      const secret = prompt('Код администратора:');
      if (!secret) return;
      const res = await fetch('/api/admin_login', {
        method: 'POST',
        body: JSON.stringify({ secret }),
      });
      const data = await res.json();
      if (data.status === 'ok') {
        isAdmin = true;
        showToast('👑 Режим администратора активирован');
      } else {
        alert(data.message || 'Неверный код');
        return;
      }
      updateAdminUI();
      applyFilter(true);
    }

    async function changeNickname() {
      const nickname = prompt('Ваш ник (виден в истории правок):', currentNickname || '');
      if (nickname === null) return;
      const res = await fetch('/api/set_nickname', {
        method: 'POST',
        body: JSON.stringify({ nickname }),
      });
      const data = await res.json();
      currentNickname = data.nickname;
      showToast(currentNickname ? `Ник установлен: ${currentNickname}` : 'Ник сброшен');
    }
```

- [ ] **Step 3: Убрать `is_admin` из всех тел запросов на клиенте**

Modify: `zen_index.html:1099-1104` — блок с `/api/review`. Поскольку
`/api/review` больше не существует (Задача 7, Step 4), а этот блок отмечал
строку одобренной без создания предложения — заменить вызов на
`/api/admin_approve_line`:

```js
        await fetch('/api/admin_approve_line', {
          method: 'POST',
          body: JSON.stringify({ uid: item.uid }),
        });
        item.status = 'admin_approved';
```

Modify: `zen_index.html:1559-1580` — второй блок `/api/review` (кнопка
переключения статуса строки), тем же способом:

```js
        const isCurrentlyApproved = item.status === 'admin_approved';
        if (isCurrentlyApproved) {
          return; // снятие одобрения не поддерживается на уровне строки --
                   // статус живёт на предложениях, снимайте одобрение с
                   // конкретного предложения через его карточку
        }
        const res = await fetch('/api/admin_approve_line', {
          method: 'POST',
          body: JSON.stringify({ uid: item.uid }),
        });
        const data = await res.json();
        if (data.status === 'ok') {
          item.status = 'admin_approved';
        }
```

- [ ] **Step 4: Перенаправить кнопку «Сохранить» из `/api/save` в
  `/api/suggest`**

Modify: `zen_index.html:1847-1859` — заменить:

```js
        const res = await fetch('/api/save', {
          method: 'POST',
          body: JSON.stringify({ comp: item.comp, key: item.key, ru: focusRuText.value }),
        });
```
на:
```js
        const res = await fetch('/api/suggest', {
          method: 'POST',
          body: JSON.stringify({ uid: item.uid, text: focusRuText.value, comment: '' }),
        });
```

И следующую строку `item.status = 'admin_approved';` — удалить (предложение
не становится одобренным автоматически, оно уходит в `open` и появляется в
`item.suggestions` при следующей загрузке `/api/data`).

- [ ] **Step 5: Обновить модалку истории — `ip` переименован в `actor`**

`get_line_data` (Задача 2) теперь отдаёт в каждой записи `history` поле
`actor` (ник или «Аноним») вместо `ip`. Modify: `zen_index.html:1441` —
заменить:

```js
            <span class="font-mono text-gray-400">Версия #${hIdx + 1} · ${h.date} (${h.ip})</span>
```
на:
```js
            <span class="font-mono text-gray-400">Версия #${hIdx + 1} · ${h.date} (${h.actor})</span>
```

- [ ] **Step 6: Добавить кнопку смены ника рядом с переключателем
  администратора**

Найти в HTML разметке место, где рендерится кнопка `toggleAdminRole` (элемент
с `onclick="toggleAdminRole()"`), и добавить рядом:

```html
<button onclick="changeNickname()" class="text-xs px-2 py-1 rounded hover:bg-white/10">
  ✏️ Ник
</button>
```

- [ ] **Step 7: Проверить в браузере**

```bash
python migrate_state.py
python sync_baseline.py
python zen_server.py
```
Открыть `http://localhost:8090`. Проверить: страница грузится без ошибок в
консоли; кнопка «Ник» запрашивает и сохраняет ник (проверить `document.cookie`
в devtools — должен появиться `zen_token`); кнопка администратора запрашивает
код, при вводе значения из `ZEN_ADMIN_TOKENS` переключает режим; сохранение
правки через основную форму создаёт предложение, а не переписывает
официальный текст сразу.

- [ ] **Step 8: Закоммитить**

```bash
git add zen_index.html
git commit -m "feat: cookie-идентичность и серверный вход администратора на фронтенде"
```

---

## Task 9: Сквозная проверка и документация запуска

**Files:**
- Modify: `README.md`

**Interfaces:**
- Нет новых — финальная проверка того, что все предыдущие задачи работают
  вместе.

- [ ] **Step 1: Полный прогон с нуля**

```bash
rm -f zen_review.db
python migrate_state.py
python sync_baseline.py
ZEN_ADMIN_TOKENS=test-admin-secret python zen_server.py &
sleep 1

# аноним смотрит данные
curl -s -c /tmp/c1.txt http://localhost:8090/api/data | python -c "import json,sys; d=json.load(sys.stdin); print('строк:', len(d['items']), 'админ:', d['identity']['is_admin'])"

# аноним предлагает правку и голосует
UID=$(curl -s -b /tmp/c1.txt http://localhost:8090/api/data | python -c "import json,sys; print(json.load(sys.stdin)['items'][0]['uid'])")
curl -s -b /tmp/c1.txt -X POST http://localhost:8090/api/suggest -d "{\"uid\":\"$UID\",\"text\":\"вариант A\",\"comment\":\"\"}"

# второй посетитель (свой cookie-файл = своя личность) голосует за предложение
SUG_ID=$(curl -s -b /tmp/c1.txt http://localhost:8090/api/data | python -c "import json,sys; d=json.load(sys.stdin); i=[x for x in d['items'] if x['uid']=='$UID'][0]; print(i['suggestions'][0]['id'])")
curl -s -c /tmp/c2.txt -b /tmp/c2.txt -X POST http://localhost:8090/api/vote_suggestion -d "{\"sug_id\":$SUG_ID,\"vote\":1}"

# вход администратором
curl -s -c /tmp/ca.txt -X POST http://localhost:8090/api/admin_login -d '{"secret":"test-admin-secret"}'
curl -s -b /tmp/ca.txt -X POST http://localhost:8090/api/accept_suggestion -d "{\"sug_id\":$SUG_ID}"

# выгрузка принятого в ветку
python export_community_suggestions.py

kill %1
```

Expected: каждая команда возвращает `{"status": "ok", ...}` (кроме
финального `export_community_suggestions.py`, который печатает `к выгрузке: 1
строк...` и `закоммичено локально в community-suggestions`); в
`repos/dead-reckoning` появляется ветка `community-suggestions` с
закоммиченной правкой; повторный запуск `git -C repos/dead-reckoning branch`
показывает, что текущая ветка снова `main`.

- [ ] **Step 2: Обновить `README.md`**

Modify: `README.md` — заменить содержимое (Weblate-инструкции) на:

```markdown
# Сайт вычитки перевода Dead Reckoning

Своя лёгкая витрина для голосования и предложений по русскому переводу —
`zen_server.py` (Python stdlib, без зависимостей). Docker/Weblate из более
ранней версии проекта не используются (файлы оставлены как справочные).

## Запуск

    ZEN_ADMIN_TOKENS=<секрет1>,<секрет2> python zen_server.py

Сайт на `http://localhost:8090`. `ZEN_ADMIN_TOKENS` — список кодов
администратора через запятую; человек, знающий один из них, вводит его в
диалоге входа на сайте.

## Обновление официального текста (из main GitHub)

    python sync_baseline.py

## Выгрузка принятых сообществом правок

    python export_community_suggestions.py [--push]

Без `--push` коммитит локально в ветку `community-suggestions`, с ним же —
отправляет на GitHub. `main` не трогается никогда: слияние делает человек
через пайплайн проекта-переводчика.

## Перенос старых данных (однократно)

    python migrate_state.py

Переносит `data/zen_state.json` в `zen_review.db`, если файл ещё не
перенесён.
```

- [ ] **Step 3: Прогнать все автоматические тесты разом**

```bash
python -m unittest discover -p "test_*.py" -v
```
Expected: все PASS.

- [ ] **Step 4: Закоммитить**

```bash
git add README.md
git commit -m "docs: обновить README под собственный сервер вместо Weblate"
```
