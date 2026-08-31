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

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        client_ip = self.get_client_ip()

        if parsed.path.startswith("/api/") and not self.resolve_identity():
            return

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

        super().do_GET()

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

if __name__ == "__main__":
    # По умолчанию только localhost -- наружу сайт смотрит через reverse proxy
    # (Caddy/nginx) с TLS. Слушать 0.0.0.0 напрямую -- значит отдавать HTTP
    # без шифрования (включая /api/admin_login) любому в интернете.
    host = os.environ.get("ZEN_HOST", "127.0.0.1")
    print(f"Starting Zen Standalone Dashboard Server on http://{host}:{PORT}...")
    with socketserver.ThreadingTCPServer((host, PORT), ZenHandler) as httpd:
        httpd.serve_forever()
