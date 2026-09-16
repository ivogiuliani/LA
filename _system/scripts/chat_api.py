#!/usr/bin/env python3
"""
chat_api.py — concierge chat di myvilla.la (stdlib HTTP, Claude tier "cheap"
via llm_client → Claude Code headless = abbonamento, MAI API a consumo).

    POST /api/chat     {"session_id": "…"?, "message": "…", "page": "/"?}
    GET  /api/healthz  {"ok": true, "service": "chat_api", …}

Bind 127.0.0.1:8789 — esposto da Caddy su https://content.myvilla.la/api/chat.
Il widget JS NON è qui (conversion-layer lo aggiunge quando chat.enabled=true);
il contratto request/response è in _system/docs/fase2/lead-lifecycle.md.

Regole:
  - risponde SOLO con i fatti di `_system/knowledge/chat_faq.md`; il system
    prompt vieta di inventare prezzi, polizze, timeline, progetti costruiti
  - disclosure obbligatoria (chat.disclosure) nella PRIMA risposta
  - quando l'utente mostra interesse: chiede nome/email/telefono e propone la
    call (booking_url o due finestre, mattine LA) → campo `capture_lead` nel
    JSON strutturato della risposta (un solo turno: {reply, capture_lead|null})
    → lead_ledger.add(source="chat") + score + ack + alert (thread)
  - sessione con id, storia max 20 turni, tetto 30 messaggi/sessione,
    rate-limit per IP 20/min, CORS solo https://myvilla.la
  - log conversazioni nel private dir: chat/<session_id>.jsonl (PII qui, mai nel repo)

CLI:
    python3 chat_api.py                      # serve su 127.0.0.1:8789
    python3 chat_api.py --dry-run            # ack/alert dei lead catturati in dry-run
    python3 chat_api.py --self-test          # backend finto, nessun modello: verifica il flusso completo
    python3 chat_api.py --self-test --live   # come sopra ma con Claude reale (Claude Code, abbonamento)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import urlparse

from lead_settings import get as cfg_get, private_dir, SYSTEM_DIR

FAQ_PATH = SYSTEM_DIR / "knowledge" / "chat_faq.md"
ALLOWED_ORIGINS = {"https://myvilla.la", "https://www.myvilla.la"}
RATE_PER_MIN = 20
MAX_TURNS = 20              # coppie user/assistant tenute in contesto
MAX_MESSAGES = 30           # messaggi utente per sessione
MAX_MSG_CHARS = 1500
SESSION_TTL_S = 6 * 3600
MAX_BODY = 16 * 1024

DRY_RUN = False
BACKEND = "claude_code"     # "claude_code" (llm_client, abbonamento) | "fake"
LLM_TIMEOUT_S = 120         # la chat è interattiva: niente attese lunghe, fallback subito
_sessions: dict = {}
_sess_lock = threading.Lock()
_rate: dict = defaultdict(deque)
_rate_lock = threading.Lock()
_started = time.time()

# Schema dell'output strutturato di ogni turno (un'unica chiamata al modello):
# `reply` è il testo per il visitatore; `capture_lead` è valorizzato SOLO nel
# turno in cui il visitatore ha dato nome + email (altrimenti null).
CAPTURE_FIELDS = {
    "first_name": {"type": "string"},
    "last_name": {"type": "string"},
    "email": {"type": "string"},
    "phone": {"type": "string"},
    "project_type": {"type": "string", "description": "e.g. New custom build, Rebuild after fire, Future site / land search, General interest"},
    "timeline": {"type": "string", "description": "e.g. Ready now, 6-12 months, 12-24 months, Exploring"},
    "site_location": {"type": "string"},
    "summary": {"type": "string", "description": "2-3 sentences: what the visitor wants, in their words"},
    "preferred_windows": {"type": "string", "description": "call windows the visitor proposed, if any"},
}
TURN_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "Your answer to the visitor, plain text, no markdown headers."},
        "capture_lead": {
            "description": ("Contact details to record so the founder's office can follow up. "
                            "Fill it ONLY in the turn where the visitor has given at least a first "
                            "name and an email address; otherwise null. Never invent or guess any field."),
            "anyOf": [
                {"type": "null"},
                {"type": "object", "properties": CAPTURE_FIELDS, "required": ["first_name", "email"]},
            ],
        },
    },
    "required": ["reply", "capture_lead"],
}


def _log(msg: str) -> None:
    print("  [chat_api] " + msg, flush=True)


def _hash(s: str) -> str:
    return hashlib.sha1((s or "").lower().encode("utf-8")).hexdigest()[:10]


def load_faq() -> str:
    try:
        return FAQ_PATH.read_text(encoding="utf-8")
    except OSError:
        return "(FAQ file missing: answer only that a person will follow up by email.)"


def system_prompt() -> str:
    booking = (cfg_get("brand.booking_url", "") or "").strip()
    sched = (f"offer this booking link for a 30-minute call with the founder: {booking}"
             if booking else
             "ask for two time windows that suit them (Los Angeles mornings work best) for a "
             "30-minute Teams call with the founder")
    return f"""You are the concierge assistant on myvilla.la, the website of My Villa: Italian-designed
luxury villas in reinforced concrete for Los Angeles. You are an AI, not a person, and you say so
when relevant. Tone: warm, precise, unhurried, British-neutral English, short paragraphs, no
exclamation marks, no sales pressure, no fear-based language (never "bunker", "fortress",
"protect your family", "survive the next fire", "dream home").

KNOWLEDGE: answer ONLY with the facts in the FAQ below. If the answer is not there, say plainly
that you do not have that detail and offer a call with the founder. NEVER invent or estimate
prices (no per-square-foot figures, no ranges, no totals), insurance premiums or coverage outcomes, permit
dates, partner names, awards, or completed villas. My Villa has NOT yet delivered a villa: if
asked, say so honestly and point to the partners' track record. Do not quote internal figures.

LEAD CAPTURE: when the visitor shows real interest (a lot, a rebuild, a budget, a timeline, or
asks to talk to someone), ask for their first name, email and phone (phone optional), and
{sched}. The moment the visitor gives a first name and an email address, fill the "capture_lead"
object of your JSON output IN THAT SAME TURN with what they told you (do not wait for the phone or
the time windows: ask for those afterwards, in the confirmation), and in "reply" confirm that the
founder's office will reply {cfg_get('canonical.response_promise', 'within one business day')}.
Fill "capture_lead" at most once per conversation and never with guessed data; in every other turn
set it to null. Never ask for financial details, IDs or passwords.

OUTPUT: always a JSON object {{"reply": "<text for the visitor>", "capture_lead": null | {{...}}}}.
Keep "reply" under 120 words unless the visitor asks for detail. Do not use markdown headers.

=== FAQ (sole source of facts) ===
{load_faq()}
=== END FAQ ==="""


# --------------------------------------------------------------------------- #
# Sessions / rate limit / logging
# --------------------------------------------------------------------------- #

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


def _gc_sessions() -> None:
    now = time.time()
    with _sess_lock:
        for sid in [s for s, v in _sessions.items() if now - v["last"] > SESSION_TTL_S]:
            _sessions.pop(sid, None)


def get_session(sid: Optional[str], ip: str) -> tuple:
    _gc_sessions()
    with _sess_lock:
        if sid and sid in _sessions:
            return sid, _sessions[sid]
        sid = secrets.token_urlsafe(12)
        _sessions[sid] = {"history": [], "count": 0, "created": time.time(), "last": time.time(),
                          "ip_hash": _hash(ip), "lead_captured": False, "lead_id": ""}
        return sid, _sessions[sid]


def _log_turn(sid: str, role: str, text: str, extra: Optional[dict] = None) -> None:
    try:
        d = private_dir() / "chat"
        d.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9_\-]", "", sid)[:40]
        with open(d / f"{safe}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                "role": role, "text": text, **(extra or {})}, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Lead capture
# --------------------------------------------------------------------------- #

def capture_lead(args: dict, session: dict, sid: str) -> dict:
    """Tool handler → registro + score + ack/alert (thread). Ritorna il tool_result."""
    email = (args.get("email") or "").strip().lower()
    first = (args.get("first_name") or "").strip()
    if not first or not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", email):
        return {"ok": False, "error": "need a first name and a valid email before capturing"}
    if session.get("lead_captured"):
        return {"ok": True, "lead_id": session.get("lead_id"), "note": "already captured in this session"}
    try:
        from lead_api import email_domain_ok
        ok, why = email_domain_ok(email)
        if not ok:
            return {"ok": False, "error": f"email rejected ({why}); ask the visitor to check it"}
    except Exception:  # noqa: BLE001
        pass
    lead_data = {
        "source": "chat", "form_id": "chat",
        "first_name": first, "last_name": (args.get("last_name") or "").strip(),
        "email": email, "phone": (args.get("phone") or "").strip(),
        "project_type": (args.get("project_type") or "").strip(),
        "timeline": (args.get("timeline") or "").strip(),
        "site_location": (args.get("site_location") or "").strip(),
        "message": ((args.get("summary") or "").strip() +
                    (f"\nPreferred call windows: {args['preferred_windows']}" if args.get("preferred_windows") else "") +
                    f"\n[chat session {sid}]").strip(),
        "how_found": "site chat",
        "attribution": {"source_page": session.get("page", ""), "landing_url": session.get("page", "")},
        "consent": {"nurture": False, "ts": ""},
    }
    try:
        from lead_intake import process_lead
        def _bg():
            try:
                s = process_lead(lead_data, dry_run=DRY_RUN, use_llm=(BACKEND != "fake"))
                _log(f"chat lead {json.dumps(s)}")
                session["lead_id"] = s.get("lead_id", "")
            except Exception as exc:  # noqa: BLE001
                _log(f"capture bg error: {exc}")
        session["lead_captured"] = True
        threading.Thread(target=_bg, daemon=True).start()
        _log_turn(sid, "system", "capture_lead", {"email_hash": _hash(email)})
        return {"ok": True, "message": "captured; the founder's office will reply within one business day"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"storage error: {type(exc).__name__}"}


# --------------------------------------------------------------------------- #
# LLM turn (claude_code via llm_client | fake)
# --------------------------------------------------------------------------- #

def _fake_turn(messages: list, session: dict, sid: str) -> str:
    last = messages[-1]["content"] if messages else ""
    if isinstance(last, list):
        last = " ".join(b.get("text", "") for b in last if isinstance(b, dict))
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", last)
    if m:
        name = re.search(r"(?:i am|i'm|my name is)\s+([A-Z][a-z]+)", last, re.I)
        res = capture_lead({"first_name": name.group(1) if name else "Visitor", "email": m.group(0),
                            "summary": last[:200], "site_location": "Malibu" if "malibu" in last.lower() else ""},
                           session, sid)
        return ("Thank you. The founder's office has your details and Paolo Mezzalama will reply within "
                "one business day." if res.get("ok") else f"I could not record that: {res.get('error')}")
    if "cost" in last.lower() or "price" in last.lower():
        return ("We do not quote prices in chat: every My Villa is priced on its site, its program and the "
                "finishes, and we share a site-specific range in the private briefing. Would you like to "
                "arrange a call with the founder? If so, may I have your first name and email?")
    return "Happy to help. Could you tell me a little about your site or your project?"


def _parse_turn(res: Any) -> dict:
    """LLMResult → {"reply": str, "capture_lead": dict|None} (tollerante)."""
    data = res.data if isinstance(getattr(res, "data", None), dict) else None
    if data is None:
        txt = (getattr(res, "text", "") or "").strip()
        start, end = txt.find("{"), txt.rfind("}")
        if start >= 0 and end > start:
            try:
                data = json.loads(txt[start:end + 1])
            except ValueError:
                data = None
        if not isinstance(data, dict):
            # il modello ha risposto in prosa: usala così com'è
            return {"reply": txt, "capture_lead": None}
    cap = data.get("capture_lead")
    return {"reply": str(data.get("reply") or "").strip(),
            "capture_lead": dict(cap) if isinstance(cap, dict) and cap else None}


def _claude_turn(messages: list, session: dict, sid: str) -> str:
    """Un solo turno via llm_client.chat (Claude Code, abbonamento): il modello
    restituisce {reply, capture_lead|null}; se capture_lead è valorizzato lo
    registriamo qui e adattiamo la risposta all'esito."""
    from llm_client import chat as llm_chat
    res = llm_chat(messages, system=system_prompt(), tier="cheap", json_schema=TURN_SCHEMA,
                   max_tokens=600, timeout=LLM_TIMEOUT_S, retries=0)
    turn = _parse_turn(res)
    reply = turn["reply"]
    cap = turn["capture_lead"]
    if not cap:
        return reply
    out = capture_lead(cap, session, sid)
    if out.get("ok"):
        return reply or ("Thank you. The founder's office has your details and will reply "
                         "within one business day.")
    err = str(out.get("error") or "")
    _log(f"capture_lead rejected: {err}")
    if "email" in err.lower():
        return ("Thank you. I could not record that email address as written; could you check it "
                "and send it again? Alternatively, write to info@myvilla.la and the founder's office "
                "will reply within one business day.")
    return reply or ("Thank you. Please write to info@myvilla.la and the founder's office will "
                     "reply within one business day.")


def llm_turn(messages: list, session: dict, sid: str) -> str:
    if BACKEND == "fake":
        return _fake_turn(messages, session, sid)
    try:
        return _claude_turn(messages, session, sid)
    except Exception as exc:  # noqa: BLE001 — include LLMUnavailable/LLMRefused: degradare, mai crashare
        _log(f"LLM error: {type(exc).__name__}: {exc}")
        return ("I am having trouble answering right now. Please write to info@myvilla.la and "
                "the founder's office will reply within one business day.")


def chat(sid: Optional[str], message: str, ip: str, page: str = "") -> tuple:
    """→ (response_dict, status)."""
    message = (message or "").strip()
    if not message:
        return {"ok": False, "error": "empty message"}, 400
    message = message[:MAX_MSG_CHARS]
    sid, session = get_session(sid, ip)
    if page and not session.get("page"):
        session["page"] = page[:200]
    if session["count"] >= MAX_MESSAGES:
        return {"ok": False, "session_id": sid, "error": "session limit reached",
                "reply": "We have reached the limit for this chat. Please write to info@myvilla.la "
                         "and the founder's office will reply within one business day."}, 429
    session["count"] += 1
    session["last"] = time.time()
    first_turn = not session["history"]
    session["history"].append({"role": "user", "content": message})
    session["history"] = session["history"][-(MAX_TURNS * 2):]
    _log_turn(sid, "user", message)
    reply = llm_turn(list(session["history"]), session, sid) or "…"
    disclosure = cfg_get("chat.disclosure", "You're chatting with My Villa's AI assistant, not a person.")
    if first_turn and disclosure.lower()[:25] not in reply.lower():
        reply = f"{disclosure}\n\n{reply}"
    session["history"].append({"role": "assistant", "content": reply})
    _log_turn(sid, "assistant", reply)
    return {"ok": True, "session_id": sid, "reply": reply, "turn": session["count"],
            "remaining": MAX_MESSAGES - session["count"],
            "disclosure": disclosure if first_turn else None,
            "lead_captured": bool(session.get("lead_captured"))}, 200


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    server_version = "MyVillaChatAPI/1.0"

    def log_message(self, fmt, *args):
        return

    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")

    def _json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _ip(self) -> str:
        xff = self.headers.get("X-Forwarded-For", "")
        return xff.split(",")[0].strip() if xff else self.client_address[0]

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if urlparse(self.path).path in ("/api/healthz", "/healthz"):
            self._json({"ok": True, "service": "chat_api", "backend": BACKEND, "dry_run": DRY_RUN,
                        "enabled": bool(cfg_get("chat.enabled", False)),
                        "sessions": len(_sessions), "uptime_s": int(time.time() - _started)})
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if urlparse(self.path).path not in ("/api/chat", "/chat"):
            self._json({"ok": False, "error": "not found"}, 404)
            return
        ip = self._ip()
        if not rate_ok(ip):
            self._json({"ok": False, "error": "too many requests"}, 429)
            return
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY:
            self._json({"ok": False, "error": "payload too large"}, 413)
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if not isinstance(data, dict):
                raise ValueError("object expected")
        except (ValueError, UnicodeDecodeError) as exc:
            self._json({"ok": False, "error": f"bad json: {exc}"}, 400)
            return
        resp, status = chat(str(data.get("session_id") or "") or None, str(data.get("message") or ""),
                            ip, str(data.get("page") or ""))
        self._json(resp, status)


def serve(host: str, port: int) -> None:
    srv = ThreadingHTTPServer((host, port), Handler)
    _log(f"listening on http://{host}:{port} backend={BACKEND} dry_run={DRY_RUN} "
         f"chat.enabled={cfg_get('chat.enabled', False)}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def self_test(live: bool) -> int:
    global BACKEND, DRY_RUN, RATE_PER_MIN
    BACKEND = "claude_code" if live else "fake"
    DRY_RUN = True
    RATE_PER_MIN = 1000   # il self-test verifica il tetto di sessione, non il rate-limit IP
    tmp = tempfile.mkdtemp(prefix="myvilla-chat-")
    os.environ[str(cfg_get("private_dir_env", "MYVILLA_PRIVATE_DIR"))] = tmp
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    import urllib.request
    base = f"http://127.0.0.1:{port}"

    def post(payload):
        req = urllib.request.Request(base + "/api/chat", data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Origin": "https://myvilla.la"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    st, r1 = post({"message": "How much does a villa cost?", "page": "/"})
    print(f"  turn1 {st}: disclosure={'yes' if r1.get('disclosure') else 'no'} | {r1.get('reply','')[:160]!r}")
    assert st == 200 and r1.get("disclosure"), "disclosure missing on first turn"
    sid = r1["session_id"]
    st, r2 = post({"session_id": sid, "message": "Yes please. I'm Alex, alex.chat@gmail.com, we have a lot in Malibu."})
    print(f"  turn2 {st}: lead_captured={r2.get('lead_captured')} | {r2.get('reply','')[:160]!r}")
    time.sleep(2.0)
    import lead_ledger
    leads = lead_ledger.list_leads()
    print(f"  ledger: {len(leads)} lead(s) source={leads[0].get('source') if leads else None} "
          f"tier={leads[0].get('tier') if leads else None}")
    log = os.path.join(tmp, "leads", "send_log.jsonl")
    if os.path.exists(log):
        print("  sends (dry-run):", [json.loads(l)["kind"] + ":" + str(json.loads(l).get("reason")) for l in open(log)])
    chat_logs = os.listdir(os.path.join(tmp, "chat")) if os.path.isdir(os.path.join(tmp, "chat")) else []
    print(f"  chat log files: {chat_logs}")
    # session cap: verifica il tetto con il backend finto (in --live sono già
    # state fatte le 2 chiamate reali + lo scoring del lead; niente altre 30)
    BACKEND = "fake"
    for _ in range(MAX_MESSAGES):
        st, r = post({"session_id": sid, "message": "hello"})
    print(f"  after {MAX_MESSAGES}+ messages: {st} {r.get('error')}")
    srv.shutdown()
    return 0


def _main(argv=None) -> int:
    global DRY_RUN, BACKEND
    p = argparse.ArgumentParser(description="My Villa chat API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8789)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--live", action="store_true", help="self-test con Claude reale")
    p.add_argument("--fake-llm", action="store_true", help="serve con backend finto (debug widget)")
    a = p.parse_args(argv)
    DRY_RUN = a.dry_run
    if a.fake_llm:
        BACKEND = "fake"
    if a.self_test:
        return self_test(a.live)
    serve(a.host, a.port)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
