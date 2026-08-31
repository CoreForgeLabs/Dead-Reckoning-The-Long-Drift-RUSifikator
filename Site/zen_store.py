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


DEFAULT_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_dotenv(path=DEFAULT_ENV_PATH):
    """Прочитать KEY=VALUE построчно из .env и положить в os.environ, не
    перезаписывая переменные, уже заданные в реальном окружении."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def load_admin_tokens(env_path=DEFAULT_ENV_PATH):
    load_dotenv(env_path)
    raw = os.environ.get("ZEN_ADMIN_TOKENS", "")
    return {t.strip() for t in raw.split(",") if t.strip()}


def is_admin(token, admin_tokens):
    return bool(token) and token in admin_tokens


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
