#!/usr/bin/env python3
"""
llm_client.py — UNICO punto di accesso ai modelli Claude per tutta la pipeline.

Policy (decisa con Ivo il 2026-09-16): NON si spende in Claude API a consumo.
Ogni chiamata passa da Claude Code in modalità headless (`claude -p`), che
usa l'abbonamento claude.ai dell'utente (piano Max) e non fattura token.

    from llm_client import complete, complete_json, LLMUnavailable
    r = complete("Scrivi...", system="Sei...", tier="writer")      # r.text
    d = complete_json("Classifica...", schema={...}, tier="cheap")  # dict validato
    r = complete("Chi costruisce...", tier="balanced", web_search=True)

Backend:
  claude_code  (DEFAULT) — subprocess `claude -p --output-format json`
                 · prompt via stdin, system prompt con --system-prompt
                 · --tools "" (nessun tool) salvo web_search=True → WebSearch
                 · --json-schema per l'output strutturato (structured_output)
                 · --no-session-persistence, cwd neutro (niente CLAUDE.md)
                 · ANTHROPIC_API_KEY viene RIMOSSA dall'ambiente del figlio:
                   se fosse presente la CLI userebbe l'API a consumo.
                 · auth: login keychain (Mac) oppure CLAUDE_CODE_OAUTH_TOKEN
                   (CI / VPS, generato con `claude setup-token`).
  api          — SDK anthropic, SOLO se MYVILLA_ALLOW_API=1 (emergenze).
                 Senza quel flag la chiamata viene rifiutata.

Tier → modello CLI (alias risolti da Claude Code):
  writer → opus · heavy → opus · balanced → sonnet · cheap → haiku
  override: MYVILLA_CLI_MODEL_WRITER=... (e HEAVY/BALANCED/CHEAP).
  Un model id esplicito (es. da model_resolver) viene mappato per famiglia.

Robustezza: retry con backoff su limiti d'uso / errori transitori, poi
LLMUnavailable (gli script la catturano e saltano: la pipeline non si ferma).
Log: _system/logs/llm_calls.jsonl (una riga per chiamata, senza contenuti).

CLI di prova:
  python3 llm_client.py --self-test        # 3 chiamate reali (testo, json, web)
  python3 llm_client.py --status           # backend, binario, auth, modelli
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent.parent
LOG_PATH = ROOT_DIR / "_system" / "logs" / "llm_calls.jsonl"

TIER_ALIAS = {"writer": "opus", "heavy": "opus", "balanced": "sonnet", "cheap": "haiku"}
_FAMILY_ALIAS = (("haiku", "haiku"), ("sonnet", "sonnet"), ("opus", "opus"), ("fable", "opus"), ("mythos", "opus"))

_LOCK = threading.Semaphore(int(os.environ.get("MYVILLA_LLM_PARALLEL", "2") or 2))
_DOTENV_DONE = False


class LLMUnavailable(RuntimeError):
    """Modello non raggiungibile (limite d'uso, CLI assente, auth mancante)."""


class LLMRefused(RuntimeError):
    """Backend API richiesto senza MYVILLA_ALLOW_API=1."""


@dataclass
class LLMResult:
    text: str
    data: Optional[Any] = None
    model: str = ""
    backend: str = "claude_code"
    usage: Dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:  # comodo nei log
        return self.text


# ── env / dotenv ─────────────────────────────────────────────────────
def _load_dotenv() -> None:
    """Carica ROOT/.env solo per le chiavi assenti (senza dipendenze)."""
    global _DOTENV_DONE
    if _DOTENV_DONE:
        return
    _DOTENV_DONE = True
    p = ROOT_DIR / ".env"
    if not p.exists():
        return
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception:  # noqa: BLE001
        pass


def backend() -> str:
    _load_dotenv()
    return (os.environ.get("MYVILLA_LLM_BACKEND") or "claude_code").strip().lower()


def cli_path() -> Optional[str]:
    _load_dotenv()
    cand = [os.environ.get("MYVILLA_CLAUDE_BIN", ""), shutil.which("claude") or "",
            str(Path.home() / ".npm-global" / "bin" / "claude"),
            "/usr/local/bin/claude", "/opt/homebrew/bin/claude",
            str(Path.home() / ".claude" / "local" / "claude")]
    for c in cand:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def model_for(tier: str = "balanced", model: Optional[str] = None) -> str:
    """Alias CLI per il tier (o per un model id esplicito, mappato per famiglia)."""
    _load_dotenv()
    if model:
        m = model.lower()
        if m in ("opus", "sonnet", "haiku"):
            return m
        for fam, alias in _FAMILY_ALIAS:
            if fam in m:
                if fam in ("fable", "mythos") and os.environ.get("MYVILLA_ALLOW_FABLE") == "1":
                    return model
                return alias
    tier = (tier or "balanced").lower()
    env = os.environ.get(f"MYVILLA_CLI_MODEL_{tier.upper()}", "").strip()
    return env or TIER_ALIAS.get(tier, "sonnet")


def _neutral_cwd() -> str:
    """Directory senza CLAUDE.md: la CLI non deve caricare istruzioni di progetto."""
    _load_dotenv()
    base = os.environ.get("MYVILLA_PRIVATE_DIR") or str(Path.home() / ".myvilla-private")
    d = Path(base) / "llm-cwd"
    try:
        d.mkdir(parents=True, exist_ok=True)
        return str(d)
    except Exception:  # noqa: BLE001
        return "/tmp"


def _child_env() -> Dict[str, str]:
    env = dict(os.environ)
    # MAI lasciare la chiave API al figlio: la CLI la userebbe (a consumo).
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        env.pop(k, None)
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    return env


def _log(entry: Dict[str, Any]) -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _caller() -> str:
    try:
        return Path(sys.argv[0]).name or "python"
    except Exception:  # noqa: BLE001
        return "python"


# ── backend: Claude Code CLI ─────────────────────────────────────────
_TRANSIENT = ("rate limit", "usage limit", "limit reached", "overloaded", "529", "503",
              "timed out", "timeout", "econnreset", "network", "temporarily")


def _run_cli(prompt: str, *, system: Optional[str], model: str, json_schema: Optional[dict],
             web_search: bool, max_turns: int, timeout: int, effort: Optional[str]) -> Dict[str, Any]:
    exe = cli_path()
    if not exe:
        raise LLMUnavailable("Claude Code CLI non trovata (npm i -g @anthropic-ai/claude-code "
                             "oppure MYVILLA_CLAUDE_BIN=/percorso/claude)")
    cmd = [exe, "-p", "--output-format", "json", "--model", model,
           "--max-turns", str(max(1, max_turns)), "--no-session-persistence"]
    if web_search:
        cmd += ["--tools", "WebSearch", "--allowedTools", "WebSearch",
                "--permission-mode", "bypassPermissions"]
    else:
        cmd += ["--tools", ""]
    if system:
        cmd += ["--system-prompt", system]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema, ensure_ascii=False)]
    if effort:
        cmd += ["--effort", effort]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                              timeout=timeout, cwd=_neutral_cwd(), env=_child_env())
    except subprocess.TimeoutExpired as exc:
        raise LLMUnavailable(f"timeout {timeout}s") from exc
    out = (proc.stdout or "").strip()
    if not out:
        raise LLMUnavailable(f"CLI senza output (exit {proc.returncode}): {(proc.stderr or '')[-300:]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        # a volte la CLI stampa righe prima del JSON: prendi l'ultimo oggetto
        idx = out.rfind("\n{")
        data = json.loads(out[idx + 1:]) if idx >= 0 else {"result": out, "subtype": "text"}
    if data.get("is_error") or data.get("subtype") not in (None, "success", "text"):
        msg = str(data.get("result") or data.get("error") or data.get("subtype"))
        low = msg.lower()
        if "not logged in" in low or "login" in low:
            raise LLMUnavailable("Claude Code non autenticata: `claude` login sul Mac o "
                                 "CLAUDE_CODE_OAUTH_TOKEN (claude setup-token) in CI/VPS")
        if any(t in low for t in _TRANSIENT):
            raise _Transient(msg)
        raise LLMUnavailable(msg[:300])
    return data


class _Transient(RuntimeError):
    pass


def _complete_cli(prompt: str, system: Optional[str], tier: str, model: Optional[str],
                  json_schema: Optional[dict], web_search: bool, max_turns: int,
                  timeout: int, effort: Optional[str], retries: int) -> LLMResult:
    alias = model_for(tier, model)
    delays = [30, 120, 300, 600][:max(0, retries)]
    attempt = 0
    t0 = time.time()
    while True:
        try:
            with _LOCK:
                data = _run_cli(prompt, system=system, model=alias, json_schema=json_schema,
                                web_search=web_search, max_turns=max_turns, timeout=timeout,
                                effort=effort)
            break
        except _Transient as exc:
            if attempt >= len(delays):
                raise LLMUnavailable(f"limite/errore transitorio persistente: {exc}") from exc
            wait = delays[attempt]
            attempt += 1
            print(f"  [llm] transitorio ({str(exc)[:80]}), retry {attempt} tra {wait}s",
                  file=sys.stderr)
            time.sleep(wait)
    text = data.get("result") or ""
    parsed = data.get("structured_output")
    if json_schema and parsed is None and text:
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            parsed = None
    usage = data.get("usage") or {}
    mu = data.get("modelUsage") or {}
    used_model = next(iter(mu.keys()), alias)
    res = LLMResult(text=text if isinstance(text, str) else json.dumps(text),
                    data=parsed, model=used_model, backend="claude_code", usage=usage,
                    duration_ms=int(data.get("duration_ms") or (time.time() - t0) * 1000),
                    raw=data)
    _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "script": _caller(), "backend": "claude_code",
          "tier": tier, "model": used_model, "web_search": web_search,
          "json": bool(json_schema), "in": usage.get("input_tokens"),
          "cache_create": usage.get("cache_creation_input_tokens"),
          "cache_read": usage.get("cache_read_input_tokens"), "out": usage.get("output_tokens"),
          "ms": res.duration_ms, "cost_reported_usd": data.get("total_cost_usd"),
          "billed": 0})
    return res


# ── backend: API (solo emergenze, esplicitamente abilitato) ─────────
def _complete_api(prompt: str, system: Optional[str], tier: str, model: Optional[str],
                  json_schema: Optional[dict], max_tokens: int) -> LLMResult:
    if os.environ.get("MYVILLA_ALLOW_API") != "1":
        raise LLMRefused("Backend API disabilitato per policy (solo abbonamento). "
                         "Per un'emergenza: MYVILLA_ALLOW_API=1 MYVILLA_LLM_BACKEND=api")
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY_DISABLED", "")
    if not key:
        raise LLMUnavailable("ANTHROPIC_API_KEY assente")
    import anthropic  # import locale: non richiesto nel backend di default
    try:
        from model_resolver import resolve
        mid = model or resolve(tier)
    except Exception:  # noqa: BLE001
        mid = model or {"writer": "claude-opus-5", "heavy": "claude-opus-5",
                        "balanced": "claude-sonnet-5", "cheap": "claude-haiku-4-5"}.get(tier, "claude-sonnet-5")
    client = anthropic.Anthropic(api_key=key)
    kwargs: Dict[str, Any] = {"model": mid, "max_tokens": max_tokens,
                              "messages": [{"role": "user", "content": prompt}]}
    if system:
        kwargs["system"] = system
    if json_schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": json_schema}}
    t0 = time.time()
    resp = client.messages.create(**kwargs)
    text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
    parsed = None
    if json_schema:
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            parsed = None
    usage = {"input_tokens": getattr(resp.usage, "input_tokens", None),
             "output_tokens": getattr(resp.usage, "output_tokens", None)}
    res = LLMResult(text=text, data=parsed, model=mid, backend="api", usage=usage,
                    duration_ms=int((time.time() - t0) * 1000))
    _log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "script": _caller(), "backend": "api",
          "tier": tier, "model": mid, "in": usage["input_tokens"], "out": usage["output_tokens"],
          "ms": res.duration_ms, "billed": 1})
    return res


# ── API pubblica ─────────────────────────────────────────────────────
def complete(prompt: str, *, system: Optional[str] = None, tier: str = "balanced",
             model: Optional[str] = None, max_tokens: int = 4096,
             json_schema: Optional[dict] = None, web_search: bool = False,
             max_turns: int = 1, timeout: int = 900, effort: Optional[str] = None,
             retries: int = 3) -> LLMResult:
    """Una chiamata, una risposta. `max_tokens` è accettato per compatibilità
    (la CLI non lo espone; il limite lo dà il modello)."""
    if not prompt or not prompt.strip():
        raise ValueError("prompt vuoto")
    if backend() == "api":
        return _complete_api(prompt, system, tier, model, json_schema, max_tokens)
    turns = max_turns
    if web_search:
        turns = max(turns, 4)
    if json_schema:
        turns = max(turns, 3)   # l'output strutturato usa un turno di tool interno
    return _complete_cli(prompt, system, tier, model, json_schema, web_search,
                         turns, timeout, effort, retries)


def complete_json(prompt: str, schema: dict, **kw: Any) -> Dict[str, Any]:
    """Come complete() ma restituisce il dict validato dallo schema (o solleva)."""
    res = complete(prompt, json_schema=schema, **kw)
    if isinstance(res.data, dict):
        return res.data
    # fallback: estrai il primo oggetto JSON dal testo
    txt = res.text.strip()
    start, end = txt.find("{"), txt.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(txt[start:end + 1])
        except Exception:  # noqa: BLE001
            pass
    raise LLMUnavailable("risposta non in formato JSON valido")


def chat(messages: List[Dict[str, str]], *, system: Optional[str] = None,
         tier: str = "cheap", json_schema: Optional[dict] = None, **kw: Any) -> LLMResult:
    """Multi-turno appiattito in un solo prompt (la CLI headless è single-shot).
    messages: [{"role": "user"|"assistant", "content": str}, ...]"""
    lines = ["Conversation so far (reply as the assistant to the last user message):", ""]
    for m in messages:
        role = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {m.get('content', '').strip()}")
    lines.append("")
    lines.append("Assistant:")
    return complete("\n".join(lines), system=system, tier=tier, json_schema=json_schema, **kw)


def status() -> Dict[str, Any]:
    _load_dotenv()
    info: Dict[str, Any] = {"backend": backend(), "cli": cli_path(),
                            "models": {t: model_for(t) for t in TIER_ALIAS},
                            "api_allowed": os.environ.get("MYVILLA_ALLOW_API") == "1",
                            "api_key_in_env": bool(os.environ.get("ANTHROPIC_API_KEY")),
                            "oauth_token_in_env": bool(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))}
    exe = cli_path()
    if exe:
        try:
            out = subprocess.run([exe, "auth", "status"], capture_output=True, text=True,
                                 timeout=30, env=_child_env()).stdout
            info["auth"] = json.loads(out) if out.strip().startswith("{") else out.strip()[:200]
        except Exception as exc:  # noqa: BLE001
            info["auth"] = f"n/d ({exc})"
    return info


def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--prompt", help="prompt libero (stampa la risposta)")
    ap.add_argument("--tier", default="balanced")
    args = ap.parse_args()
    if args.status or not (args.self_test or args.prompt):
        print(json.dumps(status(), indent=2, ensure_ascii=False))
        return 0
    if args.prompt:
        r = complete(args.prompt, tier=args.tier)
        print(r.text)
        print(f"\n[{r.backend} · {r.model} · {r.duration_ms} ms]", file=sys.stderr)
        return 0
    ok = True
    try:
        r = complete("Reply with exactly the word OK.", system="You are terse.", tier="cheap")
        print("text:", repr(r.text[:40]), "|", r.model, r.duration_ms, "ms")
        d = complete_json("Give a tier A/B/C and a score 0-100 for: 'villa in Malibu within 12 months'.",
                          {"type": "object", "properties": {"tier": {"type": "string"}, "score": {"type": "integer"}},
                           "required": ["tier", "score"]}, tier="cheap")
        print("json:", d)
        r = complete("Search the web once: who builds fire-resistant homes in Malibu? Answer in one sentence with one URL.",
                     tier="balanced", web_search=True)
        print("web:", r.text[:160].replace("\n", " "))
        try:
            os.environ["MYVILLA_LLM_BACKEND"] = "api"
            complete("ping", tier="cheap")
            print("api refusal: FAILED (call went through!)"); ok = False
        except LLMRefused as exc:
            print("api refusal: OK ->", str(exc)[:60])
        finally:
            os.environ["MYVILLA_LLM_BACKEND"] = "claude_code"
    except Exception as exc:  # noqa: BLE001
        print("SELF-TEST ERROR:", exc); ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main())
