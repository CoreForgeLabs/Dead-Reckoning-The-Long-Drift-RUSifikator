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
