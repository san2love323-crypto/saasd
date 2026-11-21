#!/usr/bin/env python3
"""
나만의 일회용 메일 서버
- 받기: 아무거나@DOMAIN 으로 오는 메일을 전부 받아서 저장 (SMTP 25번 포트)
- 보기/보내기: 웹 화면 (WEB_PORT) — 비밀번호 필요
- 보내기는 메일 발송 서비스(SMTP 릴레이)를 통해서 나감

실행:  pip install aiosmtpd
       sudo python3 mail_server.py
"""
import hmac
import json
import os
import re
import secrets
import smtplib
import sqlite3
import threading
import time
from email import message_from_bytes, policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from aiosmtpd.controller import Controller

# ───────────── 여기만 바꾸면 됨 ─────────────
DOMAIN = "xn--o39a34hbpi05hsifu9b0u7b.xn--h32bi4v.xn--3e0b707e"
PASSWORD = os.environ.get("MAIL_PASS", "change-me")   # 웹 화면 비밀번호 (영어/숫자로, 꼭 바꾸기)
WEB_PORT = int(os.environ.get("WEB_PORT", 8780))      # 웹 화면 포트
SMTP_PORT = int(os.environ.get("xsmtpsib-f0c010d29c9958301ed81158a797101d3e9282ab49c22f748660da86bb4d8be1-GdeV3DGPMFpQAqeU", 25))      # 메일 받는 포트 (25 고정, 테스트 때만 변경)
KEEP_HOURS = 24                                       # 받은 메일 보관 시간
MAX_MAIL_BYTES = 5 * 1024 * 1024                      # 메일 1통 최대 크기
SEND_LIMIT_PER_DAY = 30                               # 하루 발송 제한

# 보내기용 SMTP 릴레이 (메일 발송 서비스에서 받은 값). 비워두면 보내기 꺼짐.
RELAY_HOST = os.environ.get("RELAY_HOST", "")
RELAY_PORT = int(os.environ.get("RELAY_PORT", 587))
RELAY_USER = os.environ.get("RELAY_USER", "")
RELAY_PASS = os.environ.get("RELAY_PASS", "")
# ────────────────────────────────────────────

BASE = os.path.dirname(os.path.abspath(__file__))
LOCAL_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,39}$")
ADDR_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")

db = sqlite3.connect(os.path.join(BASE, "mail.db"), check_same_thread=False)
db_lock = threading.Lock()
with db_lock:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS messages(
            id TEXT PRIMARY KEY, addr TEXT, sender TEXT, subject TEXT,
            body TEXT, html TEXT, ts REAL);
        CREATE INDEX IF NOT EXISTS idx_addr ON messages(addr, ts);
        CREATE TABLE IF NOT EXISTS sent(ts REAL, sender TEXT, rcpt TEXT, subject TEXT);
        """
    )
    db.commit()


def query(sql, args=(), write=False):
    with db_lock:
        cur = db.execute(sql, args)
        if write:
            db.commit()
            return cur.rowcount
        return cur.fetchall()


# ───────────── 받기 (SMTP) ─────────────
class InboxHandler:
    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        local, _, dom = address.lower().rpartition("@")
        if dom != DOMAIN or not LOCAL_RE.match(local):
            return "550 no such user"          # 다른 도메인으로 중계 안 함
        envelope.rcpt_tos.append(local)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        try:
            msg = message_from_bytes(envelope.content, policy=policy.default)
            sender = str(msg["from"] or envelope.mail_from or "")
            subject = str(msg["subject"] or "(제목 없음)")
            body, html = "", ""
            try:
                part = msg.get_body(preferencelist=("plain",))
                if part is not None:
                    body = part.get_content()
                part = msg.get_body(preferencelist=("html",))
                if part is not None:
                    html = part.get_content()
            except Exception:
                body = envelope.content.decode("utf-8", "replace")
            now = time.time()
            for local in set(envelope.rcpt_tos):
                query(
                    "INSERT INTO messages VALUES(?,?,?,?,?,?,?)",
                    (secrets.token_urlsafe(9), local, sender[:300], subject[:300],
                     str(body), str(html), now),
                    write=True,
                )
        except Exception as e:  # 저장 실패해도 서버는 계속
            print("메일 저장 실패:", e)
            return "451 temporary failure"
        return "250 Message accepted"


# ───────────── 보내기 ─────────────
def send_mail(local, to, subject, body):
    if not RELAY_HOST:
        return "보내기 설정(RELAY_HOST)이 아직 안 되어 있어."
    if not LOCAL_RE.match(local):
        return "보내는 주소 이름이 이상해 (영어 소문자/숫자/._- 만)."
    if not ADDR_RE.match(to):
        return "받는 사람 주소가 이상해. 한 번에 한 명만 가능."
    if "\n" in subject or "\r" in subject:
        return "제목에 줄바꿈은 안 돼."
    since = time.time() - 86400
    if query("SELECT COUNT(*) FROM sent WHERE ts>?", (since,))[0][0] >= SEND_LIMIT_PER_DAY:
        return f"하루 발송 제한({SEND_LIMIT_PER_DAY}통)에 걸렸어."
    m = EmailMessage()
    m["From"] = f"{local}@{DOMAIN}"
    m["To"] = to
    m["Subject"] = subject or "(제목 없음)"
    m["Date"] = formatdate(localtime=True)
    m["Message-ID"] = make_msgid(domain=DOMAIN)
    m.set_content(body or "")
    try:
        with smtplib.SMTP(RELAY_HOST, RELAY_PORT, timeout=20) as s:
            s.starttls()
            if RELAY_USER:
                s.login(RELAY_USER, RELAY_PASS)
            s.send_message(m)
    except Exception as e:
        return f"발송 실패: {e}"
    query("INSERT INTO sent VALUES(?,?,?,?)", (time.time(), local, to, subject[:300]), write=True)
    return None


# ───────────── 웹 ─────────────
class Web(BaseHTTPRequestHandler):
    server_version = "mail"

    def log_message(self, *a):
        pass

    def reply(self, code, data, ctype="application/json; charset=utf-8"):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def authed(self):
        given = (self.headers.get("X-Pass") or "").encode()
        if hmac.compare_digest(given, PASSWORD.encode()):
            return True
        time.sleep(1)  # 비밀번호 마구 찍기 방지
        self.reply(401, {"error": "비밀번호가 틀렸어."})
        return False

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/":
            with open(os.path.join(BASE, "mail.html"), "rb") as f:
                return self.reply(200, f.read(), "text/html; charset=utf-8")
        if not u.path.startswith("/api/"):
            return self.reply(404, {"error": "없음"})
        if not self.authed():
            return
        if u.path == "/api/info":
            return self.reply(200, {"domain": DOMAIN, "keep_hours": KEEP_HOURS,
                                    "can_send": bool(RELAY_HOST)})
        if u.path == "/api/inbox":
            local = (q.get("addr", [""])[0]).lower()
            rows = query("SELECT id,sender,subject,ts FROM messages WHERE addr=? "
                         "ORDER BY ts DESC LIMIT 100", (local,))
            return self.reply(200, [{"id": r[0], "from": r[1], "subject": r[2], "ts": r[3]}
                                    for r in rows])
        if u.path == "/api/msg":
            rows = query("SELECT addr,sender,subject,body,html,ts FROM messages WHERE id=?",
                         (q.get("id", [""])[0],))
            if not rows:
                return self.reply(404, {"error": "메일이 없어 (지워졌을 수도)."})
            r = rows[0]
            return self.reply(200, {"to": r[0], "from": r[1], "subject": r[2],
                                    "body": r[3], "html": r[4], "ts": r[5]})
        self.reply(404, {"error": "없음"})

    def do_POST(self):
        if not self.authed():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200_000:
                return self.reply(413, {"error": "너무 길어."})
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.reply(400, {"error": "요청이 이상해."})
        if self.path == "/api/send":
            err = send_mail(str(data.get("from", "")).lower().strip(),
                            str(data.get("to", "")).strip(),
                            str(data.get("subject", "")).strip(),
                            str(data.get("body", "")))
            return self.reply(400, {"error": err}) if err else self.reply(200, {"ok": True})
        if self.path == "/api/delete":
            query("DELETE FROM messages WHERE id=?", (str(data.get("id", "")),), write=True)
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "없음"})


def cleaner():
    while True:
        query("DELETE FROM messages WHERE ts<?", (time.time() - KEEP_HOURS * 3600,), write=True)
        query("DELETE FROM sent WHERE ts<?", (time.time() - 7 * 86400,), write=True)
        time.sleep(600)


if __name__ == "__main__":
    if PASSWORD == "change-me":
        raise SystemExit("PASSWORD를 먼저 바꿔줘 (mail_server.py 위쪽).")
    smtp = Controller(InboxHandler(), hostname="0.0.0.0", port=SMTP_PORT,
                      data_size_limit=MAX_MAIL_BYTES)
    smtp.start()
    threading.Thread(target=cleaner, daemon=True).start()
    print(f"받기: *@{DOMAIN} (포트 {SMTP_PORT}) / 웹: http://0.0.0.0:{WEB_PORT}")
    print("보내기:", "켜짐" if RELAY_HOST else "꺼짐 (RELAY_HOST 설정 필요)")
    try:
        ThreadingHTTPServer(("0.0.0.0", WEB_PORT), Web).serve_forever()
    except KeyboardInterrupt:
        smtp.stop()
