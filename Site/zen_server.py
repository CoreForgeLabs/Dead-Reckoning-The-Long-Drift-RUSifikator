import collections
import http.server
import socketserver
import json
import os
import re
import zipfile
import io
import urllib.parse
from http import HTTPStatus
from datetime import datetime

import zen_store

PORT = 8090
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCE_DIR = os.path.join(BASE_DIR, "data", "source")
EXPORT_DIR = os.path.join(BASE_DIR, "data", "exported_dist")

DB_PATH = os.path.join(BASE_DIR, "zen_review.db")
BASELINE_DIR = os.path.join(BASE_DIR, "data", "source_baseline")
ADMIN_TOKENS = zen_store.load_admin_tokens()

COMPONENTS_META = [
    {"slug": "narrative", "name": "Сюжет и события", "file": "narrative.json"},
    {"slug": "labels", "name": "Интерфейс и метки", "file": "labels.json"},
    {"slug": "extra", "name": "Дополнительные строки", "file": "extra.json"},
    {"slug": "epilogue", "name": "Эпилоги и концовки", "file": "epilogue.json"},
    {"slug": "remap", "name": "Замены и нормализация", "file": "remap_by_en.json"},
    {"slug": "gdc-literal-fixes", "name": "Код и скрипты", "file": "gdc_literal_fixes.json"},
]

os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)

def extract_variables(text):
    if not text:
        return []
    p_vars = re.findall(r'%[-+0-9.]*[a-zA-Z%]', text)
    p_vars = [v for v in p_vars if v != '%%']
    b_vars = re.findall(r'\{[0-9a-zA-Z_]+\}', text)
    return p_vars + b_vars

def validate_translation_variables(src_text, tgt_text):
    src_vars = extract_variables(src_text)
    tgt_vars = extract_variables(tgt_text)
    
    missing = []
    for v in src_vars:
        if v not in tgt_vars:
            missing.append(v)
            
    # Strict word boundary for BBCode tags
    unclosed_tags = []
    for tag in ['color', 'bgcolor', 'b', 'i', 'u', 'center', 'font_size', 'img']:
        open_count = len(re.findall(rf'\[{tag}(?:\]|\s|=[^\]]*\])', tgt_text, re.IGNORECASE))
        close_count = len(re.findall(rf'\[/{tag}\]', tgt_text, re.IGNORECASE))
        # Only report if original was closed or if counts mismatch
        src_open = len(re.findall(rf'\[{tag}(?:\]|\s|=[^\]]*\])', src_text, re.IGNORECASE))
        src_close = len(re.findall(rf'\[/{tag}\]', src_text, re.IGNORECASE))
        if open_count != close_count and src_open == src_close:
            unclosed_tags.append(f'[{tag}] (открыто: {open_count}, закрыто: {close_count})')
            
    warnings = []
    if missing:
        warnings.append(f"Пропущены переменные: {', '.join(missing)}")
    if unclosed_tags:
        warnings.append(f"Нарушен баланс тегов: {', '.join(unclosed_tags)}")
        
    return warnings

def is_likely_truncated(src_key, src_text, ru_text):
    text = src_text or src_key or ""
    if text.startswith(("ecisions that", "ectories are clipped", "ed it.")):
        return True
    return False

def is_generator_clause(src_key, src_text):
    text = (src_text or src_key or "").strip()
    if text.startswith(("because the ", "because of ", "being on ", "a barren ", "a gas giant ", "a jungle ", "a temperate ", "a toxic ")):
        return True
    return False

def has_color_tags(text):
    return bool(re.search(r'\[/?(?:color|bgcolor)[^\]]*\]', text or "", re.IGNORECASE))

def parse_event_info(key):
    m = re.match(r'^(.*?)(?:_(title|message|choice_\d+_label|choice_\d+_log|choice_\d+_fail|fail|success|label|log))$', key)
    if m:
        return m.group(1), m.group(2)
    return "", ""

ParsedQuery = collections.namedtuple(
    "ParsedQuery", ["target_field", "has_var", "has_color", "has_sugg", "has_diff", "clean_q"])


def parse_query(raw_q):
    """Разобрать поисковую строку: !ru/!en/!key ограничивает поле поиска,
    has:var/has:color/has:sugg/has:diff -- модификаторы-фильтры. Портировано
    1-в-1 из прежней клиентской applyFilter() в zen_index.html."""
    q = (raw_q or "").lower().strip()
    target_field = "all"
    for prefix, field, cut in (("!ru ", "ru", 4), ("!en ", "en", 4), ("!key ", "key", 5)):
        if q.startswith(prefix):
            target_field = field
            q = q[cut:].strip()
            break

    has_var = "has:var" in q
    q = q.replace("has:var", "").strip()
    has_color = "has:color" in q
    q = q.replace("has:color", "").strip()
    has_sugg = "has:sugg" in q
    q = q.replace("has:sugg", "").strip()
    has_diff = "has:diff" in q
    q = q.replace("has:diff", "").strip()

    return ParsedQuery(target_field, has_var, has_color, has_sugg, has_diff, q)


def item_matches(item, status, comp, parsed):
    if status == "admin_queue":
        has_activity = bool(item["suggestions"]) or item["score"] > 0 or item["up"] > 0
        if item["status"] == "admin_approved" or not has_activity:
            return False
    elif status == "admin_approved":
        if item["status"] != "admin_approved":
            return False
    elif status == "has_suggestions":
        if not item["suggestions"]:
            return False
    elif status == "unvoted":
        if (item["status"] != "pending" or item["suggestions"]
                or item["up"] > 0 or item["down"] > 0):
            return False

    if comp != "all" and item["comp"] != comp:
        return False

    if parsed.has_var and not ("%" in item["ru"] or "{" in item["ru"]):
        return False
    if parsed.has_color and not item["color_tagged"]:
        return False
    if parsed.has_sugg and not item["suggestions"]:
        return False
    if parsed.has_diff and not item["history"]:
        return False

    if not parsed.clean_q:
        return True

    ru_l, en_l, key_l = item["ru"].lower(), item["en"].lower(), item["key"].lower()
    if parsed.target_field == "ru":
        return parsed.clean_q in ru_l
    if parsed.target_field == "en":
        return parsed.clean_q in en_l
    if parsed.target_field == "key":
        return parsed.clean_q in key_l
    return parsed.clean_q in ru_l or parsed.clean_q in en_l or parsed.clean_q in key_l


def admin_priority_score(item):
    is_approved = item["status"] == "admin_approved"
    sugg_count = len(item["suggestions"])
    vote_score = item["score"]
    sugg_votes = sum(s.get("score", 0) for s in item["suggestions"])
    activity = sugg_count * 25 + vote_score * 5 + sugg_votes * 3 + item["up"] * 4
    category = 0 if (not is_approved and activity > 0) else (1 if not is_approved else 2)
    return (category, -activity)


def query_items(items, comp, status, q, offset, limit):
    """Отфильтровать/отсортировать/постранично нарезать items -- то же, что
    раньше делал applyFilter()+renderItems() над ПОЛНЫМ allData в браузере.
    Возвращает (страница, всего_подходит, глобальная_статистика).

    Глобальная статистика (счётчики вкладок статуса, прогресс-бар) всегда
    считается по ВСЕМ items, независимо от текущего comp/status/q -- так же,
    как раньше updateStats() в JS игнорировала активные фильтры раздела."""
    parsed = parse_query(q)
    matched = [it for it in items if item_matches(it, status, comp, parsed)]
    if status == "admin_queue":
        matched.sort(key=admin_priority_score)

    total = len(matched)
    page = matched[offset:offset + limit]

    global_stats = {
        "all": len(items),
        "admin_approved": sum(1 for it in items if it["status"] == "admin_approved"),
        "has_suggestions": sum(1 for it in items if it["suggestions"]),
        "unvoted": sum(1 for it in items if it["status"] == "pending"
                       and not it["suggestions"] and it["up"] == 0 and it["down"] == 0),
        "admin_queue": sum(1 for it in items if it["status"] != "admin_approved"
                            and (it["suggestions"] or it["up"] > 0 or it["score"] > 0)),
    }

    by_component = {}
    for it in items:
        c = by_component.setdefault(it["comp"], {"total": 0, "admin_approved": 0})
        c["total"] += 1
        if it["status"] == "admin_approved":
            c["admin_approved"] += 1
    global_stats["by_component"] = by_component

    return page, total, global_stats


def load_all_data(viewer_token):
    con = zen_store.connect(DB_PATH)
    # Один проход по votes/suggestions/history вместо запроса на каждую из
    # 13k+ строк -- иначе load_all_data занимала больше секунды на каждый
    # /api/data.
    bulk = zen_store.get_bulk_line_data(con, viewer_token)
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
            line = bulk.get(uid, zen_store.EMPTY_LINE_DATA)

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

class ZenHandler(http.server.SimpleHTTPRequestHandler):
    def get_client_ip(self):
        x_forwarded = self.headers.get("X-Forwarded-For")
        if x_forwarded:
            return x_forwarded.split(",")[0].strip()
        return self.client_address[0]

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.OK)
        self.end_headers()

    def get_cookie_token(self):
        cookie_header = self.headers.get("Cookie", "")
        for part in cookie_header.split(";"):
            part = part.strip()
            if part.startswith("zen_token="):
                return part[len("zen_token="):]
        return None

    def resolve_identity(self):
        """Вызывается в начале do_GET/do_POST. Устанавливает self.identity и
        self.is_admin_request. Всегда возвращает True -- IP без cookie
        получает СВОЮ существующую личность обратно (см.
        zen_store.get_or_create_identity), а не отказ."""
        con = zen_store.connect(DB_PATH)
        token = self.get_cookie_token()
        ip = self.get_client_ip()
        identity = zen_store.get_or_create_identity(con, token, ip)
        con.close()

        self.identity = identity
        self.is_admin_request = zen_store.is_admin(identity["token"], ADMIN_TOKENS)
        self._new_cookie = identity["token"] if token != identity["token"] else None
        return True

    def send_identity_cookie_if_new(self):
        if getattr(self, "_new_cookie", None):
            self.send_header(
                "Set-Cookie",
                "zen_token=%s; Path=/; Max-Age=31536000; SameSite=Lax" % self._new_cookie)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        client_ip = self.get_client_ip()

        if parsed.path.startswith("/api/") and not self.resolve_identity():
            return

        if parsed.path == "/api/data":
            # Пагинация + фильтрация на сервере -- раньше клиент получал и
            # держал в памяти ВСЕ 13k+ строк на каждой загрузке, что роняло
            # вкладку браузера по памяти при переключении разделов.
            qs = urllib.parse.parse_qs(parsed.query)
            comp = qs.get("comp", ["all"])[0]
            status = qs.get("status", ["all"])[0]
            q = qs.get("q", [""])[0]
            try:
                offset = max(0, int(qs.get("offset", ["0"])[0]))
            except ValueError:
                offset = 0
            try:
                limit = min(200, max(1, int(qs.get("limit", ["50"])[0])))
            except ValueError:
                limit = 50

            all_items = load_all_data(self.identity["token"])
            page, total, stats = query_items(all_items, comp, status, q, offset, limit)
            data_with_identity = {
                "items": page,
                "total": total,
                "stats": stats,
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

        if parsed.path == "/api/event_siblings":
            # Модалка "Событие" показывает весь квест целиком (заголовок +
            # все выборы) -- этим строкам-соседям нужен полный список
            # событий, который клиент больше не хранит целиком.
            qs = urllib.parse.parse_qs(parsed.query)
            event_id = qs.get("event_id", [""])[0]
            all_items = load_all_data(self.identity["token"])
            siblings = [it for it in all_items
                        if it["event_id"] == event_id or it["key"].startswith(event_id)]
            payload = json.dumps({"items": siblings}, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/api/export_csv":
            # CSV раньше строился в браузере из filteredData -- теперь клиент
            # не держит весь отфильтрованный список, поэтому CSV собирает и
            # отдаёт сервер напрямую, без прохода через память вкладки.
            qs = urllib.parse.parse_qs(parsed.query)
            comp = qs.get("comp", ["all"])[0]
            status = qs.get("status", ["all"])[0]
            q = qs.get("q", [""])[0]
            all_items = load_all_data(self.identity["token"])
            matched, _, _ = query_items(all_items, comp, status, q, 0, len(all_items))

            import csv
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(["Component", "Status", "Key", "English", "Russian", "Score", "Warnings"])
            for it in matched:
                writer.writerow([
                    it["comp_name"], it["status"], it["key"], it["en"], it["ru"],
                    it["score"], "; ".join(it["qa_warnings"]),
                ])
            csv_bytes = ("﻿" + buf.getvalue()).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f"attachment; filename=DeadReckoning_Translation_{comp}_{status}_"
                f"{datetime.now().strftime('%Y%m%d')}.csv")
            self.send_header("Content-Length", str(len(csv_bytes)))
            self.end_headers()
            self.wfile.write(csv_bytes)
            return

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
            self.send_header("Content-Disposition", f"attachment; filename=DeadReckoning_Translation_Backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip")
            self.send_header("Content-Length", str(len(zip_data)))
            self.end_headers()
            self.wfile.write(zip_data)
            return
            
        if parsed.path in ["/", "/index.html"]:
            html_path = os.path.join(BASE_DIR, "zen_index.html")
            with open(html_path, "rb") as f:
                content = f.read()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
            return

        # Никакой раздачи произвольных файлов из рабочей директории --
        # SimpleHTTPRequestHandler.do_GET() отдал бы .env, .git и что угодно
        # ещё, лежащее рядом с zen_server.py. Все нужные маршруты уже
        # обработаны выше; всё остальное -- честный 404.
        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        client_ip = self.get_client_ip()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        req = json.loads(body.decode("utf-8")) if body else {}

        if not self.resolve_identity():
            return

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

        self.send_error(404, "Not found")

class ZenServer(socketserver.ThreadingTCPServer):
    # Без этого сокет после падения/systemctl restart остаётся в TIME_WAIT
    # ~60 секунд, и каждая попытка автоперезапуска валится в EADDRINUSE --
    # снаружи это выглядит как сайт лёг и не поднимается несколько минут.
    allow_reuse_address = True


if __name__ == "__main__":
    # По умолчанию только localhost -- наружу сайт смотрит через reverse proxy
    # (Caddy/nginx) с TLS. Слушать 0.0.0.0 напрямую -- значит отдавать HTTP
    # без шифрования (включая /api/admin_login) любому в интернете.
    host = os.environ.get("ZEN_HOST", "127.0.0.1")
    print(f"Starting Zen Standalone Dashboard Server on http://{host}:{PORT}...")
    with ZenServer((host, PORT), ZenHandler) as httpd:
        httpd.serve_forever()
