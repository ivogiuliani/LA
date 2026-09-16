#!/usr/bin/env python3
"""
lead_api.py — endpoint pubblico di intake lead (stdlib, nessuna dipendenza web).

    POST /api/lead      JSON o application/x-www-form-urlencoded
    GET  /api/healthz   {"ok": true, "service": "lead_api", ...}

Bind 127.0.0.1:8788 — esposto da Caddy su https://content.myvilla.la/api/lead
(vedi _system/deploy/Caddyfile.snippet). Protezioni:
  - CORS solo per https://myvilla.la (+ preflight OPTIONS)
  - honeypot `_gotcha` (pieno → 200 "ok" finto, nessun record, nessun ack)
  - rate-limit per IP 5/min (X-Forwarded-For dietro Caddy)
  - dedup email + 10 min (registro): niente doppio ack
  - validazione dominio email via socket.getaddrinfo + blocklist usa-e-getta
  - spam → nessun ack (record marcato, o scartato se honeypot)
Poi: lead_ledger.add → lead_score → lead_ack (ack + alert) in un thread
di background, così il browser riceve la risposta subito.

Risposta: JSON {"ok": true, "lead_id": "…", "redirect": thank_you_url}
oppure, per un form HTML classico (Accept: text/html), 303 → thank_you_url.

Campi accettati (stessi nomi del form del sito): first_name, last_name,
email, phone, project_type, timeline, site_location, message, how_found,
referred_by, consent_nurture, source_page, referrer, landing_url, utm_*.

CLI:
    python3 lead_api.py                       # serve su 127.0.0.1:8788
    python3 lead_api.py --port 8788 --dry-run # nessun invio (ack/alert loggati in dry-run)
    python3 lead_api.py --self-test           # avvia su porta libera, POST di prova, esce 0
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from lead_settings import get as cfg_get, private_dir

ALLOWED_ORIGINS = {"https://myvilla.la", "https://www.myvilla.la"}
RATE_PER_MIN = 5
MAX_BODY = 64 * 1024
DISPOSABLE_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com", "temp-mail.org",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com", "dispostable.com",
    "maildrop.cc", "fakeinbox.com", "throwawaymail.com", "mohmal.com", "emailondeck.com",
    "mintemail.com", "spamgourmet.com", "mytemp.email", "tempr.email", "discard.email",
}
_ACCEPTED = ("first_name", "last_name", "email", "phone", "project_type", "timeline",
             "site_location", "message", "how_found", "referred_by", "consent_nurture",
             "source_page", "referrer", "landing_url", "utm_source", "utm_medium",
             "utm_campaign", "utm_term", "utm_content", "form_id", "_gotcha")

DRY_RUN = False
USE_LLM = True
_rate: dict = defaultdict(deque)
_rate_lock = threading.Lock()
_started = time.time()


def _log(msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat(timespec='seconds')}] {msg}"
    print("  [lead_api] " + msg, flush=True)
    try:
        d = private_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "lead_api.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _hash(s: str) -> str:
    return hashlib.sha1((s or "").lower().encode("utf-8")).hexdigest()[:10]


def rate_ok(ip: str) -> bool:
    now = time.time()
    with _rate_lock:
        q = _rate[ip]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_PER_MIN:
            return False
        q.append(now)
        return True


def email_domain_ok(email: str) -> tuple:
    """(ok, reason). Sintassi, blocklist usa-e-getta, risoluzione DNS del dominio."""
    e = (email or "").strip().lower()
    if "@" not in e or e.count("@") != 1 or " " in e:
        return False, "invalid_syntax"
    local, domain = e.split("@")
    if not local or "." not in domain or len(domain) < 4:
        return False, "invalid_syntax"
    if domain in DISPOSABLE_DOMAINS:
        return False, "disposable_domain"
    try:
        socket.setdefaulttimeout(4)
        socket.getaddrinfo(domain, 25)
    except (socket.gaierror, socket.timeout, OSError):
        # nessun A/AAAA: prova un MX-like via 'mail.' non è affidabile; consideriamo non risolvibile
        return False, "domain_unresolvable"
    return True, "ok"


def spam_signals(fields: dict) -> list:
    sig = []
    msg = (fields.get("message") or "")
    if fields.get("_gotcha"):
        sig.append("honeypot")
    if msg.count("http") >= 3:
        sig.append("many_links")
    if len(msg) > 6000:
        sig.append("too_long")
    low = msg.lower()
    for w in ("casino", "crypto signal", "viagra", "loan approval", "seo services", "backlinks"):
        if w in low:
            sig.append(f"kw:{w}")
    return sig


def _process_background(lead_data: dict) -> None:
    try:
        from lead_intake import process_lead
        summary = process_lead(lead_data, dry_run=DRY_RUN, use_llm=USE_LLM)
        _log("processed " + json.dumps(summary, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        _log(f"background error: {type(exc).__name__}: {exc}")


class Handler(BaseHTTPRequestHandler):
    server_version = "MyVillaLeadAPI/1.0"

    def log_message(self, fmt, *args):  # quiet default logging (no IPs in stdout)
        return

    # ---- helpers ------------------------------------------------------
    def _origin_ok(self) -> Optional[str]:
        origin = self.headers.get("Origin", "")
        return origin if origin in ALLOWED_ORIGINS else None

    def _cors(self) -> None:
        origin = self._origin_ok()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, url: str) -> None:
        self.send_response(303)
        self.send_header("Location", url)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _client_ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return self.client_address[0]

    def _wants_html(self) -> bool:
        acc = self.headers.get("Accept", "")
        ctype = self.headers.get("Content-Type", "")
        return "text/html" in acc and "application/json" not in ctype

    # ---- routes -------------------------------------------------------
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/api/healthz", "/healthz"):
            self._json({"ok": True, "service": "lead_api", "dry_run": DRY_RUN,
                        "uptime_s": int(time.time() - _started),
                        "ledger_dir": str(private_dir(create=False))})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/lead", "/lead"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        ip = self._client_ip()
        if not rate_ok(ip):
            self._json({"ok": False, "error": "too many requests"}, 429)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY:
            self._json({"ok": False, "error": "payload too large"}, 413)
            return
        raw = self.rfile.read(length) if length else b""
        ctype = (self.headers.get("Content-Type") or "").lower()
        fields: dict = {}
        try:
            if "application/json" in ctype:
                data = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("json must be an object")
                fields = {k: ("" if v is None else str(v)) for k, v in data.items()}
            else:
                parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
                fields = {k: (v[0] if v else "") for k, v in parsed.items()}
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"ok": False, "error": f"bad payload: {exc}"}, 400)
            return
        fields = {k: v.strip()[:6000] for k, v in fields.items() if k in _ACCEPTED}
        thank_you = cfg_get("brand.thank_you_url", "https://myvilla.la/briefing-received.html")

        # Honeypot: risposta "ok" finta, nessun record.
        if fields.get("_gotcha"):
            _log(f"honeypot hit from ip#{_hash(ip)}")
            if self._wants_html():
                self._redirect(thank_you)
            else:
                self._json({"ok": True, "redirect": thank_you})
            return

        email = (fields.get("email") or "").lower()
        if not email or not (fields.get("first_name") or fields.get("message")):
            self._json({"ok": False, "error": "email and first_name (or message) are required"}, 400)
            return
        ok, why = email_domain_ok(email)
        if not ok:
            _log(f"rejected email#{_hash(email)}: {why}")
            self._json({"ok": False, "error": f"email rejected ({why})"}, 422)
            return

        signals = spam_signals(fields)
        from lead_intake import fields_to_lead
        lead_data = fields_to_lead(fields, source="lead_api")
        lead_data["attribution"]["referrer"] = lead_data["attribution"].get("referrer") or self.headers.get("Referer", "")
        lead_data["ip_hash"] = _hash(ip)

        import lead_ledger
        try:
            lead = lead_ledger.add(lead_data)
        except Exception as exc:  # noqa: BLE001
            _log(f"ledger error: {exc}")
            self._json({"ok": False, "error": "storage error"}, 500)
            return

        if lead.get("_duplicate_of"):
            _log(f"duplicate email#{_hash(email)} → {lead['_duplicate_of']}")
            lead_id = lead["_duplicate_of"]
        else:
            lead_id = lead["lead_id"]
            if signals:
                _log(f"spam signals for {lead_id}: {signals} → no ack")
                lead_ledger.update(lead_id, tier="C", score=0, next_action="review (spam signals)",
                                   needs_human=True, spam_signals=signals, note="spam signals, no ack")
            else:
                # score + ack + alert in background (il record esiste già: ledger_add=False)
                lead_full = dict(lead)
                t = threading.Thread(target=_process_after_add, args=(lead_full,), daemon=True)
                t.start()
        if self._wants_html():
            self._redirect(thank_you)
        else:
            self._json({"ok": True, "lead_id": lead_id, "redirect": thank_you})


def _process_after_add(lead: dict) -> None:
    """Score + ack + alert su un lead già nel registro."""
    try:
        import lead_ledger
        from lead_score import score_lead
        from lead_ack import ack_and_alert
        score = score_lead(lead, use_llm=USE_LLM)
        lead["tier"], lead["score"] = score["tier"], score["score"]
        next_action = ("review (vendor/spam)" if score.get("vendor") else
                       "human check tier" if score.get("needs_human") else "founder reply")
        lead_ledger.update(lead["lead_id"], tier=score["tier"], score=score["score"],
                           next_action=next_action, score_reasons=score.get("reasons", []),
                           needs_human=bool(score.get("needs_human")), note=f"scored ({score.get('method')})")
        if score.get("vendor"):
            _log(f"{lead['lead_id']} vendor → no ack")
            return
        res = ack_and_alert(lead, score, dry_run=DRY_RUN)
        _log(f"{lead['lead_id']} tier={score['tier']} ack={((res.get('ack') or {}).get('reason'))} "
             f"alert={((res.get('alert') or {}).get('reason'))}")
    except Exception as exc:  # noqa: BLE001
        _log(f"post-add error: {type(exc).__name__}: {exc}")


def serve(host: str, port: int) -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    _log(f"listening on http://{host}:{port}  dry_run={DRY_RUN} private_dir={private_dir(create=False)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def self_test() -> int:
    global DRY_RUN, USE_LLM
    DRY_RUN, USE_LLM = True, False
    tmp = tempfile.mkdtemp(prefix="myvilla-leadapi-")
    os.environ[str(cfg_get("private_dir_env", "MYVILLA_PRIVATE_DIR"))] = tmp
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.request
    import urllib.error
    base = f"http://127.0.0.1:{port}"

    def post(path, data, ctype, headers=None):
        req = urllib.request.Request(base + path, data=data, method="POST",
                                     headers={"Content-Type": ctype, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    with urllib.request.urlopen(base + "/api/healthz") as r:
        print("  healthz:", r.status, r.read().decode()[:80])
    body = json.dumps({"first_name": "Test", "last_name": "Api", "email": "test.api@gmail.com",
                       "project_type": "New custom build", "timeline": "Ready now",
                       "site_location": "Malibu", "message": "Self-test lead."}).encode()
    print("  json lead:", post("/api/lead", body, "application/json", {"Origin": "https://myvilla.la"}))
    print("  dup (10 min):", post("/api/lead", body, "application/json"))
    print("  honeypot:", post("/api/lead", b"email=x@gmail.com&first_name=a&_gotcha=bot", "application/x-www-form-urlencoded"))
    print("  bad domain:", post("/api/lead", b"email=a@nonexistent-domain-xyz123.invalid&first_name=a", "application/x-www-form-urlencoded"))
    print("  disposable:", post("/api/lead", b"email=a@mailinator.com&first_name=a", "application/x-www-form-urlencoded"))
    for i in range(6):
        st, _ = post("/api/lead", b"email=b@gmail.com&first_name=b", "application/x-www-form-urlencoded",
                     {"X-Forwarded-For": "203.0.113.9"})
    print("  rate limit 6th from same IP:", st)
    time.sleep(1.5)
    import lead_ledger
    leads = lead_ledger.list_leads()
    print(f"  ledger: {len(leads)} lead(s) in {tmp}; first: state={leads[-1].get('state')} tier={leads[-1].get('tier')}")
    log = os.path.join(tmp, "leads", "send_log.jsonl")
    if os.path.exists(log):
        kinds = [json.loads(l)["kind"] + ":" + str(json.loads(l).get("reason")) for l in open(log)]
        print("  sends (dry-run):", kinds)
    srv.shutdown()
    return 0


def _main(argv=None) -> int:
    global DRY_RUN, USE_LLM
    p = argparse.ArgumentParser(description="My Villa lead API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8788)
    p.add_argument("--dry-run", action="store_true", help="ack/alert in dry-run (log, no send)")
    p.add_argument("--no-llm", action="store_true")
    p.add_argument("--self-test", action="store_true")
    a = p.parse_args(argv)
    DRY_RUN = a.dry_run
    USE_LLM = not a.no_llm
    if a.self_test:
        return self_test()
    serve(a.host, a.port)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
