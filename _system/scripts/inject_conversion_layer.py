#!/usr/bin/env python3
"""
inject_conversion_layer.py — Fase 2 "conversion layer" per il sito v1 live.

Applica in modo IDEMPOTENTE (marker CONV:<nome>:START/END) le patch di
conversione a index.html e inietta gli snippet condivisi (Consent Mode v2,
mvTrack, submit del form canonico) nelle altre pagine pubbliche.

Testi canonici e parametri (prezzo, timeline, promessa di risposta, landing,
thank-you, endpoint lead API, Formspree, GA4) vengono letti da
_system/config/lead_settings.yml: nessun valore hardcodato nel sito.

Uso
  python3 _system/scripts/inject_conversion_layer.py                    # patch completa di index.html
  python3 _system/scripts/inject_conversion_layer.py --dry-run          # solo diff, nessuna scrittura
  python3 _system/scripts/inject_conversion_layer.py --target v2/index.html --dry-run
  python3 _system/scripts/inject_conversion_layer.py --shared team.html privacy.html ...
        # solo snippet condivisi (consent + mvTrack + form JS + data-page-type)
  python3 _system/scripts/inject_conversion_layer.py --print-form home
        # stampa il markup del form canonico (per incollarlo in una nuova pagina)

Patch su index.html (--target)
  CONSENT   Consent Mode v2 default (UE/EEA/UK/CH denied, resto granted) prima di gtag('js')
  CSS       stili per hero-ctas, sticky, CTA contestuali, FAQ, trust note, cookie toast, form
  NAV       nav-cta e voce del menu mobile → landing ?src=nav
  HERO      due bottoni sotto la hero-sub
  VIDEO     video hero caricato via JS solo ≥ 900px (poster su mobile, preload none)
  CTX_*     tre CTA contestuali dopo #resilience, #process, #team
  TRUST_*   "Completed Works" → "DGU — Selected Works" + riga di trasparenza
  FAQ       FAQ visibili nella sezione Investment, allineate allo schema FAQPage
  SCHEMA    JSON-LD: un solo Organization (@id #organization) + Person founder + FAQ allineate
  FORM      form canonico al posto del vecchio #contactForm (stesso id, handleSubmit adattata)
  FORMJS    submit dual-post Formspree + lead API → redirect thank-you (blocco condiviso)
  COOKIE    banner: opt-in in UE (timezone Europe/*), toast altrove; consent update
  STICKY    CTA sticky mobile fino a 900px, sopra il cookie banner
  MVTRACK   delega click [data-ev], form_start, contact_email_click (blocco condiviso)
  COOKIE_UI (solo --shared) banner/toast + CSS per le pagine senza banner proprio
  GACONFIG  gtag('config') con debug_mode se la sessione è di test (?mv_debug=1)
  READTRACK qualità di lettura: read_25/50/75/100 (scroll) e read_time_30s/60s/120s/300s (scheda visibile), tutte le pagine
  --sweep   allinea tutte le pagine servite prive di CONSENT/GACONFIG/banner (Journal compreso; MVTRACK escluso sugli articoli)

Exit code sempre 0 (errori non fatali loggati), tranne argomenti errati.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

ROOT = Path(__file__).resolve().parents[2]
SETTINGS_PATH = ROOT / "_system" / "config" / "lead_settings.yml"

# Codici paese in cui il default è "denied" (UE 27 + EEA + UK + CH)
EU_REGIONS = [
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
    "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE",
    "IS", "LI", "NO", "GB", "CH",
]

# Profili social (2026-09-22): icone nel footer di tutte le pagine (home qui, le altre in inject_contact_links.py)
SOCIAL_IG = "https://www.instagram.com/myvilla.la/"
SOCIAL_LI = "https://www.linkedin.com/company/myvilla-la/"
SVG_IG = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 2.2c3.2 0 3.6 0 4.8.1 1.2.1 1.8.2 2.2.4.6.2 1 .5 1.4.9.4.4.7.8.9 1.4.2.4.4 1.1.4 2.2.1 1.3.1 1.6.1 4.8s0 3.6-.1 4.8c-.1 1.2-.2 1.8-.4 2.2-.2.6-.5 1-.9 1.4-.4.4-.8.7-1.4.9-.4.2-1.1.4-2.2.4-1.3.1-1.6.1-4.8.1s-3.6 0-4.8-.1c-1.2-.1-1.8-.2-2.2-.4-.6-.2-1-.5-1.4-.9-.4-.4-.7-.8-.9-1.4-.2-.4-.4-1.1-.4-2.2C2.2 15.6 2.2 15.2 2.2 12s0-3.6.1-4.8c.1-1.2.2-1.8.4-2.2.2-.6.5-1 .9-1.4.4-.4.8-.7 1.4-.9.4-.2 1.1-.4 2.2-.4C8.4 2.2 8.8 2.2 12 2.2M12 0C8.7 0 8.3 0 7.1.1 5.8.1 4.9.3 4.1.6c-.8.3-1.5.7-2.1 1.4C1.3 2.6.9 3.3.6 4.1.3 4.9.1 5.8.1 7.1 0 8.3 0 8.7 0 12s0 3.7.1 4.9c.1 1.3.3 2.2.6 2.9.3.8.7 1.5 1.4 2.1.6.7 1.3 1.1 2.1 1.4.8.3 1.6.5 2.9.6 1.2.1 1.6.1 4.9.1s3.7 0 4.9-.1c1.3-.1 2.2-.3 2.9-.6.8-.3 1.5-.7 2.1-1.4.7-.6 1.1-1.3 1.4-2.1.3-.8.5-1.6.6-2.9.1-1.2.1-1.6.1-4.9s0-3.7-.1-4.9c-.1-1.3-.3-2.2-.6-2.9-.3-.8-.7-1.5-1.4-2.1C21.4 1.3 20.7.9 19.9.6 19.1.3 18.2.1 16.9.1 15.7 0 15.3 0 12 0zm0 5.8a6.2 6.2 0 1 0 0 12.4 6.2 6.2 0 0 0 0-12.4zM12 16a4 4 0 1 1 0-8 4 4 0 0 1 0 8zm6.4-11.8a1.4 1.4 0 1 0 0 2.9 1.4 1.4 0 0 0 0-2.9z"/></svg>'
SVG_LI = '<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M20.4 20.4h-3.6v-5.6c0-1.3 0-3-1.8-3s-2.1 1.4-2.1 2.9v5.7H9.3V9h3.4v1.6c.5-.9 1.6-1.8 3.4-1.8 3.6 0 4.3 2.4 4.3 5.5v6.1zM5.3 7.4a2.1 2.1 0 1 1 0-4.2 2.1 2.1 0 0 1 0 4.2zM7.1 20.4H3.5V9h3.6v11.4zM22.2 0H1.8C.8 0 0 .8 0 1.7v20.5c0 1 .8 1.8 1.8 1.8h20.4c1 0 1.8-.8 1.8-1.8V1.7C24 .8 23.2 0 22.2 0z"/></svg>'

PAGE_TYPES = {
    "index.html": "home",
    "private-briefing.html": "landing",
    "briefing-received.html": "thank_you",
    "team.html": "team",
    "privacy.html": "privacy",
    "malibu-custom-home-builder.html": "pillar",
    "beverly-hills-custom-home.html": "pillar",
    "italian-villa-california-builder.html": "pillar",
    "icf-concrete-home-builder-los-angeles.html": "pillar",
    "pacific-palisades-rebuild.html": "pillar",
    "insurable-home-california.html": "hub",
    "westside-rebuild-tracker.html": "research",
}

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

DEFAULTS = {
    "brand": {
        "name": "My Villa",
        "site": "https://myvilla.la",
        "contact_email": "info@myvilla.la",
        "landing_url": "https://myvilla.la/private-briefing.html",
        "thank_you_url": "https://myvilla.la/briefing-received.html",
    },
    "canonical": {
        "price": "premium, site-specific pricing; we share a range in the private briefing rather than a brochure number",
        "price_faq": "Every My Villa is priced on its site, its program and the level of finish you choose. It is a premium, museum-grade build in reinforced concrete: in the private briefing we share a site-specific range rather than a brochure number, and the total cost of ownership we discuss includes insurance positioning and maintenance over decades, not only the build.",
        "timeline": "about 18 months after permit approval, roughly 20–24 months overall",
        "response_promise": "within one business day",
        "founder_name": "Paolo Mezzalama",
        "founder_title": "Founder & Architect",
        "promise_line": "We reply within one business day, and a My Villa partner joins your first call to answer every question.",
        "architect_of_record_note": "Architecture of record in California is carried by a California-licensed architect.",
        "built_disclaimer": "My Villa has not yet delivered a villa: the projects shown are concept designs and renders.",
    },
    "lead_api": {"endpoint": "https://content.myvilla.la/api/lead"},
    "formspree_id": "mgoljyjl",
    "ga4_measurement_id": "G-D6HJX7BNZN",
}


def load_settings() -> dict:
    data = {}
    if yaml is not None and SETTINGS_PATH.exists():
        try:
            data = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # pragma: no cover
            print(f"[settings] impossibile leggere {SETTINGS_PATH}: {exc} — uso i default", file=sys.stderr)
    merged = json.loads(json.dumps(DEFAULTS))
    for k, v in data.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update({kk: vv for kk, vv in v.items() if vv not in (None, "")})
        elif v not in (None, ""):
            merged[k] = v
    return merged


class S:
    """Accesso comodo ai valori canonici."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        b, c = cfg["brand"], cfg["canonical"]
        self.site = b["site"].rstrip("/")
        self.email = b["contact_email"]
        self.landing_url = b["landing_url"]
        self.landing_path = urlparse(b["landing_url"]).path or "/private-briefing.html"
        self.thank_you_url = b["thank_you_url"]
        # lead_api.enabled: false ⇒ il form non chiama l'API (endpoint non ancora deployato: evita errori CORS in console)
        self.lead_api = cfg["lead_api"]["endpoint"] if cfg["lead_api"].get("enabled", True) not in (False, "false", "no", 0) else ""
        self.formspree = f"https://formspree.io/f/{cfg['formspree_id']}"
        self.ga4 = cfg["ga4_measurement_id"]
        self.price = c["price"]
        self.price_faq = c.get("price_faq") or c["price"]
        self.timeline = c["timeline"]
        self.response_promise = c["response_promise"]
        self._c = c
        self.founder_name = c["founder_name"]
        self.founder_title = c["founder_title"]
        self.aor_note = c["architect_of_record_note"]
        self.built_disclaimer = c["built_disclaimer"]

    def landing(self, src: str) -> str:
        return f"{self.landing_path}?src={src}"

    @property
    def promise_line(self) -> str:
        return self._c.get("promise_line") or "We reply within one business day, and a My Villa partner joins your first call to answer every question."


# ---------------------------------------------------------------------------
# Marker helpers
# ---------------------------------------------------------------------------

HTML_C = ("<!-- ", " -->")
JS_C = ("/* ", " */")


def markers(name: str, comment=HTML_C) -> Tuple[str, str]:
    a, b = comment
    return f"{a}CONV:{name}:START{b}", f"{a}CONV:{name}:END{b}"


def upsert(html: str, name: str, content: str, locate: Callable[[str], Optional[Tuple[int, int]]],
           log: List[str], comment=HTML_C) -> str:
    """Inserisce/aggiorna il blocco marcato. locate(html) → (start, end) da rimpiazzare
    (start == end ⇒ inserimento). None ⇒ anchor assente ⇒ skip."""
    start, end = markers(name, comment)
    body = content.strip("\n")
    block = f"{start}\n{body}\n{end}"
    i, j = html.find(start), html.find(end)
    if i != -1 and j != -1 and j > i:
        j_end = j + len(end)
        if html[i:j_end] == block:
            log.append(f"  = {name:16s} unchanged")
            return html
        log.append(f"  ~ {name:16s} updated")
        return html[:i] + block + html[j_end:]
    loc = locate(html)
    if loc is None:
        log.append(f"  ! {name:16s} SKIPPED (anchor not found)")
        return html
    s, e = loc
    log.append(f"  + {name:16s} {'inserted' if s == e else 'replaced anchor'}")
    if s == e:  # pure insertion: keep the block on its own lines
        if s > 0 and html[s - 1] != "\n":
            block = "\n" + block
        if html[e:e + 1] != "\n":
            block = block + "\n"
    return html[:s] + block + html[e:]


def before(pattern: str, flags=0, last: bool = False) -> Callable[[str], Optional[Tuple[int, int]]]:
    def _loc(html):
        ms = list(re.finditer(pattern, html, flags))
        if not ms:
            return None
        m = ms[-1] if last else ms[0]
        return (m.start(), m.start())
    return _loc


def before_body_end() -> Callable[[str], Optional[Tuple[int, int]]]:
    """Ultimo </body> del documento (mai un'occorrenza dentro script o commenti)."""
    return before(r"</body>", last=True)


def after(pattern: str, flags=0) -> Callable[[str], Optional[Tuple[int, int]]]:
    def _loc(html):
        m = re.search(pattern, html, flags)
        return (m.end(), m.end()) if m else None
    return _loc


def replace(pattern: str, flags=0) -> Callable[[str], Optional[Tuple[int, int]]]:
    def _loc(html):
        m = re.search(pattern, html, flags)
        return (m.start(), m.end()) if m else None
    return _loc


def after_section_close(section_id: str) -> Callable[[str], Optional[Tuple[int, int]]]:
    """Posizione subito dopo il </section> che chiude <section ... id="X">."""
    def _loc(html):
        m = re.search(r'<section[^>]*\bid="%s"[^>]*>' % re.escape(section_id), html)
        if not m:
            return None
        j = html.find("</section>", m.end())
        if j == -1:
            return None
        j += len("</section>")
        return (j, j)
    return _loc


# ---------------------------------------------------------------------------
# Shared snippets (identici su tutte le pagine)
# ---------------------------------------------------------------------------

def consent_js(s: S) -> str:
    regions = json.dumps(EU_REGIONS, separators=(",", ":"))
    return f"""  // Consent Mode v2 — default denied in EU/EEA/UK/CH (opt-in), granted elsewhere (notice + opt-out); stored choice and GPC/DNT honoured
  gtag('consent', 'default', {{ ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied', analytics_storage: 'denied', region: {regions} }});
  gtag('consent', 'default', {{ ad_storage: 'denied', ad_user_data: 'denied', ad_personalization: 'denied', analytics_storage: 'granted' }});
  (function () {{ try {{ var c = localStorage.getItem('myvilla_cookie_consent'); if (c === 'accepted') gtag('consent', 'update', {{ analytics_storage: 'granted' }}); else if (c === 'declined' || navigator.globalPrivacyControl || navigator.doNotTrack === '1') gtag('consent', 'update', {{ analytics_storage: 'denied' }}); }} catch (e) {{}} }})();
  // GA4 DebugView for a whole test session: open any page with ?mv_debug=1 (never set for real visitors)
  (function () {{ try {{ var d = /[?&]mv_debug=1(&|$)/.test(location.search); if (d) sessionStorage.setItem('mv_debug', '1'); window.__mvDebug = d || sessionStorage.getItem('mv_debug') === '1'; }} catch (e) {{ window.__mvDebug = false; }} }})();"""


def mvtrack_js() -> str:
    return """<script>
(function () {
  var PT = (document.body && document.body.getAttribute('data-page-type')) || 'page';
  function send(ev, params) { try { if (typeof gtag === 'function') gtag('event', ev, params); } catch (e) {} }
  function snake(k) { return k.replace(/([A-Z])/g, '_$1').toLowerCase(); }
  document.addEventListener('click', function (e) {
    var t = e.target; if (!t || !t.closest) return;
    var el = t.closest('[data-ev]');
    if (el) {
      var p = { page_type: PT }, d = el.dataset;
      for (var k in d) { if (k !== 'ev') p[snake(k)] = d[k]; }
      if (!p.cta_id) p.cta_id = el.id || (el.getAttribute('href') || '').slice(0, 60) || el.tagName.toLowerCase();
      send(d.ev, p);
    }
    var a = t.closest('a[href^="mailto:"]');
    if (a) send('contact_email_click', { page_type: PT, cta_id: a.id || 'mailto', link_url: a.getAttribute('href') });
  }, true);
  // form_start / form_submit arrivano già da GA4 Enhanced Measurement (Form interactions): nessun evento custom qui.
  window.mvTrack = send;
})();
</script>"""


FORMJS_TEMPLATE = """<script>
(function () {
  var MV = { formspree: '@FORMSPREE@', leadApi: '@LEADAPI@', thankYou: '@THANKYOU@' };
  var UTM = ['utm_source', 'utm_medium', 'utm_campaign', 'utm_id', 'utm_content', 'utm_term'];
  var CLICK = ['oppref', 'gclid'];   // oppref = ChatGPT Ads click id, gclid = Google Ads
  var TTL = 30 * 24 * 3600 * 1000;   // attribution kept in the browser for 30 days
  if (!Promise.allSettled) { Promise.allSettled = function (ps) { return Promise.all(ps.map(function (p) { return Promise.resolve(p).then(function (v) { return { status: 'fulfilled', value: v }; }, function (r) { return { status: 'rejected', reason: r }; }); })); }; }
  function ss(k, v) { try { if (v === undefined) return sessionStorage.getItem(k); sessionStorage.setItem(k, v); } catch (e) { return null; } }
  function ls(k, v) { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }
  function declined() { try { return localStorage.getItem('myvilla_cookie_consent') === 'declined'; } catch (e) { return false; } }
  function parse(raw) { try { var o = JSON.parse(raw || 'null'); return (o && typeof o === 'object') ? o : null; } catch (e) { return null; } }
  var q = {}; try { new URLSearchParams(location.search).forEach(function (v, k) { q[k] = v; }); } catch (e) {}
  function blank() { var a = { referrer: document.referrer || '', landing_url: location.href.split('#')[0], ts: Date.now() }; UTM.concat(CLICK).forEach(function (k) { a[k] = ''; }); return a; }
  function fromUrl() { var a = blank(), has = false; UTM.concat(CLICK).forEach(function (k) { a[k] = String(q[k] || '').slice(0, 200); if (a[k]) has = true; }); return has ? a : null; }
  // Attribution (last tagged touch): a URL with utm_* or a click id overwrites; otherwise this session's;
  // otherwise the one kept in the browser (30 days; not stored when analytics cookies were declined); otherwise direct.
  var attrib = fromUrl();
  if (attrib) { ss('mv_attrib', JSON.stringify(attrib)); if (!declined()) ls('mv_attrib', JSON.stringify(attrib)); }
  else {
    attrib = parse(ss('mv_attrib'));
    if (!attrib) { var kept = parse(ls('mv_attrib')); if (kept && kept.ts && (Date.now() - kept.ts) < TTL) { attrib = kept; ss('mv_attrib', JSON.stringify(attrib)); } }
    if (!attrib) { attrib = blank(); ss('mv_attrib', JSON.stringify(attrib)); }
  }
  attrib.cta_src = q.src || attrib.cta_src || '';
  window.mvAttribution = attrib;
  function fill(form) {
    Object.keys(attrib).forEach(function (k) { var el = form.querySelector('input[type="hidden"][name="' + k + '"]'); if (el) el.value = attrib[k] || ''; });
    var sub = form.querySelector('input[name="_subject"]');
    if (sub) {
      var base = sub.getAttribute('data-base') || sub.value; sub.setAttribute('data-base', base);
      var via = attrib.utm_source ? (attrib.utm_source + '/' + (attrib.utm_medium || '-') + (attrib.utm_campaign ? ' · ' + attrib.utm_campaign : '')) : '';
      sub.value = via ? base + ' · via ' + via : base;
    }
  }
  function setError(form, on) { var el = form.querySelector('.cta-error'); if (el) el.classList.toggle('show', !!on); }
  function submit(form) {
    if (!form.checkValidity()) { form.reportValidity(); return; }
    if (form.getAttribute('data-mv-busy') === '1') return;
    fill(form); setError(form, false);
    var btn = form.querySelector('[type="submit"]'), orig = btn ? btn.textContent : '';
    if (btn) { btn.textContent = 'Sending…'; btn.disabled = true; btn.style.opacity = '0.7'; }
    form.setAttribute('data-mv-busy', '1');
    var fd = new FormData(form);
    var formId = fd.get('form_id') || form.id || 'form', how = fd.get('how_found') || '';
    var dest = MV.thankYou + '?form=' + encodeURIComponent(formId) + '&via=' + encodeURIComponent(how);
    if (fd.get('_gotcha')) { window.location.assign(dest); return; }  // honeypot: silent
    var eventId = 'lead_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
    fd.append('event_id', eventId);
    var payload = {}; fd.forEach(function (v, k) { if (k !== '_gotcha') payload[k] = v; });
    if (!payload.consent_nurture) payload.consent_nurture = 'no';
    payload.submitted_at = new Date().toISOString();
    var sends = [fetch(MV.formspree, { method: 'POST', body: fd, headers: { 'Accept': 'application/json' } })];
    if (MV.leadApi) sends.push(fetch(MV.leadApi, { method: 'POST', mode: 'cors', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }));
    Promise.allSettled(sends).then(function (rs) {
      var ok = rs.some(function (r) { return r.status === 'fulfilled' && r.value && r.value.ok; });
      if (!ok) throw new Error('send-failed');
      var wrap = form.parentElement, succ = wrap && wrap.querySelector('.cta-success, .brief-success');
      if (succ) succ.classList.add('show');
      var done = false; function go() { if (done) return; done = true; window.location.assign(dest); }
      // GA4 generate_lead: only after the request was accepted, once per form and session, never with personal data.
      var guard = 'mv_lead_sent_' + formId;
      if (ss(guard)) { go(); return; }
      ss(guard, '1');
      var pt = (document.body && document.body.getAttribute('data-page-type')) || 'page';
      var params = { form_id: formId, page_type: pt, how_found: how, lead_source: attrib.utm_source || '', lead_medium: attrib.utm_medium || '', lead_campaign: attrib.utm_campaign || '', lead_ref: eventId, transport_type: 'beacon', event_callback: go, event_timeout: 800 };
      if (window.__mvDebug) params.debug_mode = true;
      try { document.dispatchEvent(new CustomEvent('mv:lead', { detail: { form_id: formId, event_id: eventId, page_type: pt } })); } catch (e) {}
      try { if (typeof gtag === 'function') gtag('event', 'generate_lead', params); else go(); } catch (e) { go(); }
      setTimeout(go, 800);
    }).catch(function () {
      form.removeAttribute('data-mv-busy');
      setError(form, true);
      if (btn) { btn.textContent = orig; btn.disabled = false; btn.style.opacity = ''; }
    });
  }
  window.handleSubmit = function (e) { e.preventDefault(); submit(e.target); };
  function init() {
    var forms = document.querySelectorAll('form[data-mv-form]');
    for (var i = 0; i < forms.length; i++) { (function (f) {
      fill(f);
      if (!f.hasAttribute('onsubmit')) f.addEventListener('submit', function (e) { e.preventDefault(); submit(f); });
    })(forms[i]); }
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
</script>"""


def readtrack_js() -> str:
    return """<script>
(function () {
  // Reading quality (no personal data): scroll depth 25/50/75/100% and visible-tab reading time 30/60/120/300 s, once per threshold
  var PT = (document.body && document.body.getAttribute('data-page-type')) || 'page';
  function send(ev, p) { p = p || {}; p.page_type = PT; if (window.__mvDebug) p.debug_mode = true; try { if (typeof gtag === 'function') gtag('event', ev, p); } catch (e) {} }
  var marks = [25, 50, 75, 100], hit = {};
  function depth() {
    var doc = document.documentElement, h = Math.max(doc.scrollHeight, document.body ? document.body.scrollHeight : 0) - window.innerHeight;
    if (h <= 0) return 100;
    return Math.min(100, Math.round((window.scrollY || doc.scrollTop || 0) / h * 100));
  }
  function onScroll() { var d = depth(); for (var i = 0; i < marks.length; i++) { var m = marks[i]; if (!hit[m] && d >= m) { hit[m] = true; send('read_' + m, { percent: m }); } } }
  window.addEventListener('scroll', onScroll, { passive: true });
  setTimeout(onScroll, 1500);
  var secs = 0, tmarks = [30, 60, 120, 300], thit = {};
  setInterval(function () {
    if (document.hidden) return;
    secs += 5;
    for (var i = 0; i < tmarks.length; i++) { var m = tmarks[i]; if (!thit[m] && secs >= m) { thit[m] = true; send('read_time_' + m + 's', { seconds: m }); } }
  }, 5000);
})();
</script>"""


def formjs(s: S) -> str:
    return (FORMJS_TEMPLATE.replace("@FORMSPREE@", s.formspree)
            .replace("@LEADAPI@", s.lead_api).replace("@THANKYOU@", s.thank_you_url))


# ---------------------------------------------------------------------------
# Canonical form
# ---------------------------------------------------------------------------

HOW_FOUND = [
    ("Google search", "Google search"),
    ("ChatGPT or another AI assistant", "ChatGPT or another AI assistant"),
    ("Instagram", "Instagram"),
    ("LinkedIn", "LinkedIn"),
    ("X", "X"),
    ("Press or article", "Press or article"),
    ("Referral", "Referral"),
    ("Other", "Other"),
]
PROJECT_TYPES = [
    ("New custom build", "New custom build"),
    ("Rebuild after fire", "Rebuild after fire"),
    ("Future site / land search", "Future site — still searching"),
    ("General interest", "General interest"),
]
TIMELINES = [
    ("Ready now", "Ready now"),
    ("6-12 months", "Within 6–12 months"),
    ("12-24 months", "Within 1–2 years"),
    ("Exploring", "Just exploring"),
]


def _options(opts) -> str:
    out = ['<option value="" selected disabled>Select…</option>']
    out += [f'<option value="{v}">{label}</option>' for v, label in opts]
    return "".join(out)


def canonical_form(s: S, form_id: str, source_page: str, page_type: str, subject: str,
                   dom_id: str = "contactForm", onsubmit: bool = False) -> str:
    on = ' onsubmit="handleSubmit(event)"' if onsubmit else ""
    return f"""<form class="cta-form" id="{dom_id}"{on} data-mv-form aria-label="Request a private briefing">
  <div class="cta-field"><label class="cta-field-label" for="mv-first">First name *</label><input id="mv-first" type="text" class="cta-input" name="first_name" required autocomplete="given-name"></div>
  <div class="cta-field"><label class="cta-field-label" for="mv-last">Last name</label><input id="mv-last" type="text" class="cta-input" name="last_name" autocomplete="family-name"></div>
  <div class="cta-field"><label class="cta-field-label" for="mv-email">Email *</label><input id="mv-email" type="email" class="cta-input" name="email" required autocomplete="email"></div>
  <div class="cta-field"><label class="cta-field-label" for="mv-phone">Phone</label><input id="mv-phone" type="tel" class="cta-input" name="phone" autocomplete="tel"><span class="cta-field-hint">Used only to reach you about your briefing. No marketing texts.</span></div>
  <div class="cta-field"><label class="cta-field-label" for="mv-project">I'm exploring *</label><select id="mv-project" class="cta-input" name="project_type" required>{_options(PROJECT_TYPES)}</select></div>
  <div class="cta-field"><label class="cta-field-label" for="mv-timeline">Timeline *</label><select id="mv-timeline" class="cta-input" name="timeline" required>{_options(TIMELINES)}</select></div>
  <div class="cta-field"><label class="cta-field-label" for="mv-found">How did you find us? *</label><select id="mv-found" class="cta-input" name="how_found" required>{_options(HOW_FOUND)}</select></div>
  <div class="cta-field"><label class="cta-field-label" for="mv-referred">Referred by</label><input id="mv-referred" type="text" class="cta-input" name="referred_by" placeholder="Agent, broker, architect, friend…"></div>
  <div class="cta-field full"><label class="cta-field-label" for="mv-location">Site location</label><input id="mv-location" type="text" class="cta-input" name="site_location" placeholder="e.g. Malibu, Pacific Palisades, Bel Air…"></div>
  <div class="cta-field full"><label class="cta-field-label" for="mv-message">Your project</label><textarea id="mv-message" class="cta-input" name="message" rows="3" placeholder="A few lines about your site, your vision, or what drew you to My Villa."></textarea></div>
  <label class="cta-consent"><input type="checkbox" name="consent_nurture" value="yes"><span>Keep me posted with occasional insights on insurable building in California.</span></label>
  <input type="text" name="_gotcha" style="display:none" tabindex="-1" autocomplete="off" aria-hidden="true">
  <input type="hidden" name="_subject" value="{subject}">
  <input type="hidden" name="form_id" value="{form_id}">
  <input type="hidden" name="source_page" value="{source_page}">
  <input type="hidden" name="page_type" value="{page_type}">
  <input type="hidden" name="referrer" value="">
  <input type="hidden" name="landing_url" value="">
  <input type="hidden" name="utm_source" value="">
  <input type="hidden" name="utm_medium" value="">
  <input type="hidden" name="utm_campaign" value="">
  <input type="hidden" name="utm_content" value="">
  <input type="hidden" name="utm_term" value="">
  <input type="hidden" name="utm_id" value="">
  <input type="hidden" name="oppref" value="">
  <input type="hidden" name="gclid" value="">
  <input type="hidden" name="cta_src" value="">
  <button type="submit" class="cta-submit">Request a Private Briefing</button>
  <div class="cta-micro"><strong>{s.promise_line}</strong></div>
  <div class="cta-legal">By submitting you agree to our <a href="/privacy.html#what-we-collect">Privacy Notice</a>.</div>
  <div class="cta-error" role="alert">Something went wrong and your request did not go through. Please write to us directly at <a href="mailto:{s.email}">{s.email}</a>.</div>
</form>"""


FORM_EXTRA_CSS = """.cta-field-hint { font-size: 11px; line-height: 1.5; color: rgba(255,255,255,0.5); }
.cta-consent { grid-column: 1 / -1; display: flex; gap: 10px; align-items: flex-start; font-family: var(--sans); font-size: 12.5px; line-height: 1.5; color: rgba(255,255,255,0.72); cursor: pointer; text-align: left; }
.cta-consent input { margin-top: 3px; width: 16px; height: 16px; flex-shrink: 0; accent-color: var(--terracotta); }
.cta-legal { grid-column: 1 / -1; font-family: var(--sans); font-size: 11.5px; color: rgba(255,255,255,0.5); text-align: center; }
.cta-legal a { text-decoration: underline; color: rgba(255,255,255,0.75); }
.cta-error { grid-column: 1 / -1; display: none; padding: 12px 14px; border: 1px solid rgba(194,113,79,0.7); background: rgba(194,113,79,0.12); font-family: var(--sans); font-size: 13px; line-height: 1.6; color: var(--cream); }
.cta-error.show { display: block; }
.cta-error a { text-decoration: underline; color: var(--tuscan-gold); }
select.cta-input:invalid { color: rgba(255,255,255,0.75); }
.cta-form input.cta-input::placeholder, .cta-form textarea.cta-input::placeholder { color: rgba(255,255,255,0.45); opacity: 1; }"""


# CSS completo del form canonico per pagine che NON hanno già .cta-form (malibu, landing)
FORM_BASE_CSS = """.cta-form { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; text-align: left; }
.cta-form .full { grid-column: 1 / -1; }
.cta-field { display: flex; flex-direction: column; gap: 6px; text-align: left; }
.cta-field-label { font-family: var(--sans); font-size: 9.5px; letter-spacing: 0.2em; font-weight: 700; text-transform: uppercase; color: rgba(255,255,255,0.55); }
.cta-input { width: 100%; padding: 13px 14px; background: rgba(255,255,255,0.07); border: 1px solid rgba(255,255,255,0.22); color: #fff; font-family: var(--sans); font-size: 15px; outline: none; transition: border-color 0.3s; -webkit-appearance: none; appearance: none; border-radius: 0; }
.cta-input:focus { border-color: var(--tuscan-gold); }
select.cta-input { background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6'%3E%3Cpath d='M1 1l4 4 4-4' fill='none' stroke='%23FFFFFF' stroke-width='1.4'/%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 14px center; padding-right: 38px; cursor: pointer; }
select.cta-input option { color: var(--espresso); background: var(--cream); }
textarea.cta-input { min-height: 96px; resize: vertical; }
.cta-submit { grid-column: 1 / -1; padding: 16px 32px; background: var(--terracotta); color: #fff; border: none; font-family: var(--sans); font-size: 12px; font-weight: 600; letter-spacing: 0.15em; text-transform: uppercase; cursor: pointer; transition: background 0.3s, transform 0.2s; min-height: 48px; }
.cta-submit:hover { background: var(--tuscan-gold); transform: translateY(-1px); }
.cta-micro { grid-column: 1 / -1; font-family: var(--sans); font-size: 12px; color: rgba(255,255,255,0.6); text-align: center; }
.cta-micro strong { color: var(--cream); font-weight: 600; }
.cta-form-wrap { position: relative; }
.cta-success { position: absolute; inset: 0; z-index: 3; background: rgba(26,24,22,0.96); display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; padding: 40px 28px; opacity: 0; pointer-events: none; transition: opacity 0.5s; }
.cta-success.show { opacity: 1; pointer-events: auto; }
.cta-success h3 { font-family: var(--serif); font-size: 26px; font-weight: 500; color: var(--warm-sand); }
.cta-success p { margin-top: 10px; font-size: 14px; color: rgba(250,248,245,0.7); max-width: 380px; }
@media (max-width: 550px) { .cta-form { grid-template-columns: 1fr; } .cta-form .full { grid-column: auto; } }
""" + FORM_EXTRA_CSS


# ---------------------------------------------------------------------------
# FAQ (visibili + schema, stessa fonte)
# ---------------------------------------------------------------------------

def faqs(s: S) -> List[Tuple[str, str]]:
    return [
        ("What makes My Villa homes fire-resilient?",
         "My Villa uses double-skin precast reinforced concrete panels approximately 250mm thick with continuous thermal insulation. This monolithic, non-combustible construction provides a materially higher fire resistance than the wood-frame construction common in Los Angeles."),
        ("How long does it take to build a My Villa home?",
         f"Construction takes {s.timeline}. The path is fixed from the first conversation: discovery, typology selection, personalization, engineering and permits, then off-site prefabrication and on-site assembly with a fixed-price agreement."),
        ("What is fair-faced concrete?",
         "Fair-faced concrete is a museum-grade finish where the concrete surface is the final aesthetic, requiring no additional cladding or paint. My Villa uses oxide pigments to achieve warm, natural tones inspired by Italian architectural traditions seen in buildings like Palazzo Grassi and the Kimbell Art Museum."),
        ("Where does My Villa build?",
         "My Villa designs and delivers custom villas across the Los Angeles area, with a primary focus on Malibu, Pacific Palisades, Beverly Hills, Bel Air, Brentwood, Hidden Hills, Calabasas and the surrounding hillside and coastal neighborhoods."),
        ("What is the design philosophy behind My Villa?",
         "My Villa combines Italian architectural heritage with California's lifestyle — Italian Soul, Californian Body. The approach integrates biophilic design, reinforced concrete construction, and sustainable materials to create homes that are beautiful, resilient, and in harmony with their environment."),
        ("How do you work with my agent, broker or architect?",
         "As collaborators. Many briefings arrive through a buyer's agent, a broker holding a lot, or a family's architect or advisor. We share feasibility reads, keep your representative in every conversation you want them in, and can work alongside an architect of record you have already chosen. If you are the representative, say so in the form and we will address the briefing to you."),
    ]


def faq_html(s: S) -> str:
    items = "\n".join(
        f'      <details class="faq-item"><summary>{q}</summary><p>{a}</p></details>' for q, a in faqs(s)
    )
    return f"""    <div class="invest-faq reveal" id="faq">
      <div class="section-label">Questions we hear most</div>
{items}
    </div>"""


# ---------------------------------------------------------------------------
# CSS block for index.html
# ---------------------------------------------------------------------------

def conv_css(s: S) -> str:
    return f"""/* Journal research strip (home) */
.journal-research {{ margin: 44px 0 30px; padding-top: 30px; border-top: 1px solid rgba(62,47,43,0.12); }}
.journal-research-label {{ font-family: var(--sans); font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; color: var(--terracotta); font-weight: 600; margin-bottom: 16px; }}
.journal-research-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 18px; }}
.journal-research-card {{ display: flex; flex-direction: column; gap: 8px; padding: 22px 24px; background: #FFFFFF; border: 1px solid rgba(62,47,43,0.12); text-decoration: none; color: inherit; transition: border-color .25s ease, transform .25s ease; }}
.journal-research-card:hover {{ border-color: var(--terracotta); transform: translateY(-2px); }}
.jr-kicker {{ font-family: var(--sans); font-size: 10px; letter-spacing: 0.2em; text-transform: uppercase; color: var(--terracotta); font-weight: 600; }}
.jr-title {{ font-family: var(--serif); font-size: 20px; line-height: 1.3; color: #2B2622; }}
.jr-meta {{ font-family: var(--sans); font-size: 12px; color: #8A8072; }}
@media (max-width: 760px) {{ .journal-research-grid {{ grid-template-columns: 1fr; }} }}
/* Footer contact */
.footer-contact {{ display: flex; flex-wrap: wrap; gap: 10px 14px; align-items: baseline; padding: 18px 0 4px; margin-top: 8px; border-top: 1px solid rgba(255,255,255,0.08); font-size: 13px; color: rgba(255,255,255,0.75); }}
.footer-contact-label {{ font-size: 10px; letter-spacing: 0.2em; text-transform: uppercase; color: var(--warm-sand); margin-right: 6px; }}
.footer-contact a {{ color: #fff; opacity: 0.85; transition: color 0.3s, opacity 0.3s; }}
.footer-contact a:hover {{ opacity: 1; color: var(--pacific-blue); }}
.footer-contact-sep {{ opacity: 0.4; }}
.nav-social {{ display: inline-flex; align-items: center; gap: 10px; margin: 0 4px 0 2px; }}
.nav-social a {{ display: inline-flex; color: var(--warm-sand); opacity: 0.85; transition: opacity 0.3s, color 0.3s; }}
.nav-social a:hover {{ opacity: 1; color: #fff; }}
.nav-social svg {{ width: 15px; height: 15px; fill: currentColor; display: block; }}
.mobile-social {{ display: flex; gap: 22px; padding: 18px 0 4px; }}
.mobile-social a {{ display: inline-flex; color: var(--warm-sand); opacity: 0.9; }}
.mobile-social svg {{ width: 22px; height: 22px; fill: currentColor; display: block; }}
.footer-social {{ display: inline-flex; align-items: center; gap: 12px; margin-left: auto; }}
.footer-social a {{ display: inline-flex; color: var(--warm-sand); opacity: 0.85; }}
.footer-social a:hover {{ opacity: 1; color: #fff; }}
.footer-social svg {{ width: 17px; height: 17px; fill: currentColor; display: block; }}
@media (max-width: 600px) {{ .footer-social {{ margin-left: 0; width: 100%; }} }}
/* Hero CTAs */
.hero-ctas {{ display: flex; gap: 14px; justify-content: center; flex-wrap: wrap; margin-top: 34px; position: relative; z-index: 2; }}
.hero-btn {{ display: inline-flex; align-items: center; justify-content: center; min-height: 48px; padding: 14px 30px; font-family: var(--sans); font-size: 11px; letter-spacing: 0.2em; text-transform: uppercase; font-weight: 600; color: var(--white); transition: background 0.3s, border-color 0.3s, transform 0.2s; }}
.hero-btn-primary {{ background: var(--terracotta); border: 1px solid var(--terracotta); }}
.hero-btn-primary:hover {{ background: var(--pacific-blue); border-color: var(--pacific-blue); transform: translateY(-1px); }}
.hero-btn-ghost {{ border: 1px solid rgba(255,255,255,0.6); background: rgba(62,47,43,0.28); }}
.hero-btn-ghost:hover {{ border-color: var(--white); background: rgba(255,255,255,0.14); }}
@media (max-width: 640px) {{ .hero-ctas {{ flex-direction: column; align-items: stretch; padding: 0 8px; }} .hero-btn {{ width: 100%; }} }}
@media (max-height: 500px) and (orientation: landscape) {{ .hero-ctas {{ margin-top: 18px; }} }}
/* Sticky CTA: up to 900px, above the cookie banner, lifted by its height when visible */
.sticky-cta {{ z-index: 9995; bottom: calc(16px + var(--mv-banner-h, 0px) + env(safe-area-inset-bottom)); }}
@media (max-width: 900px) {{ .sticky-cta {{ display: block; }} }}
/* Contextual CTAs */
.ctx-cta {{ padding: 0 var(--side-pad) clamp(48px, 7vw, 88px); }}
.ctx-cta--offblack {{ background: var(--offblack); }}
.ctx-cta--espresso {{ background: var(--espresso); }}
.ctx-cta--charcoal {{ background: var(--charcoal); }}
.ctx-cta-inner {{ max-width: 1100px; margin: 0 auto; display: flex; align-items: center; justify-content: space-between; gap: 24px 40px; flex-wrap: wrap; border-top: 1px solid rgba(255,255,255,0.1); padding-top: 32px; }}
.ctx-cta-text {{ font-family: var(--serif); font-size: clamp(19px, 2.2vw, 24px); font-style: italic; font-weight: 300; color: var(--cream); opacity: 0.85; max-width: 620px; line-height: 1.4; }}
.ctx-cta-btn {{ display: inline-block; padding: 14px 28px; border: 1px solid var(--tuscan-gold); color: var(--cream); font-family: var(--sans); font-size: 11px; letter-spacing: 0.18em; text-transform: uppercase; font-weight: 600; white-space: nowrap; transition: background 0.3s, color 0.3s; }}
.ctx-cta-btn:hover {{ background: var(--tuscan-gold); color: var(--offblack); }}
/* Trust note under DGU works */
.material-trust-note {{ margin: -10px 0 clamp(28px, 3vw, 40px); font-family: var(--sans); font-size: 12.5px; line-height: 1.6; color: var(--white); opacity: 0.55; max-width: 720px; }}
/* Visible FAQ (Investment) */
.invest-faq {{ margin-top: 56px; }}
.invest-faq .section-label {{ margin-bottom: 18px; }}
.faq-item {{ border-top: 1px solid rgba(255,255,255,0.1); }}
.faq-item:last-child {{ border-bottom: 1px solid rgba(255,255,255,0.1); }}
.faq-item summary {{ list-style: none; cursor: pointer; padding: 18px 40px 18px 0; position: relative; font-family: var(--serif); font-size: clamp(18px, 2vw, 22px); font-weight: 400; color: var(--cream); }}
.faq-item summary::-webkit-details-marker {{ display: none; }}
.faq-item summary::after {{ content: '+'; position: absolute; right: 4px; top: 50%; transform: translateY(-50%); color: var(--tuscan-gold); font-family: var(--sans); font-size: 22px; font-weight: 300; transition: transform 0.3s; }}
.faq-item[open] summary::after {{ transform: translateY(-50%) rotate(45deg); }}
.faq-item p {{ font-size: 14px; line-height: 1.8; color: var(--white); opacity: 0.7; padding: 0 0 22px; max-width: 760px; }}
/* Cookie banner below the sticky CTA; discreet toast outside the EU */
body #cookieBanner {{ z-index: 9990; }}
body #cookieBanner.cookie-toast {{ left: auto; right: 16px; bottom: 16px; max-width: 380px; border: 1px solid rgba(196,162,101,0.3); border-radius: 4px; padding: 14px 16px; gap: 14px; flex-wrap: nowrap; }}
body #cookieBanner.cookie-toast .cookie-text {{ font-size: 12.5px; min-width: 0; }}
body #cookieBanner.cookie-toast .cookie-btn {{ padding: 8px 14px; font-size: 11px; }}
@media (max-width: 600px) {{ body #cookieBanner.cookie-toast {{ left: 16px; max-width: none; flex-direction: row; text-align: left; }} body #cookieBanner.cookie-toast .cookie-buttons {{ width: auto; }} }}
/* Canonical form extras */
{FORM_EXTRA_CSS}"""


# ---------------------------------------------------------------------------
# Cookie banner JS (replaces the old GDPR block)
# ---------------------------------------------------------------------------

def cookie_js() -> str:
    return """// ══════════════ COOKIE CONSENT — Consent Mode v2 (opt-in in EU, notice + opt-out elsewhere) ══════════════
(function () {
  var KEY = 'myvilla_cookie_consent';
  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function store(v) { try { localStorage.setItem(KEY, v); } catch (e) {} }
  function consent(granted) { try { gtag('consent', 'update', { analytics_storage: granted ? 'granted' : 'denied' }); } catch (e) {} }
  var tz = ''; try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) {}
  var isEU = /^Europe\\//.test(tz);
  window.mvIsEU = isEU;
  function close() {
    var b = document.getElementById('cookieBanner');
    document.documentElement.style.removeProperty('--mv-banner-h');
    if (b) { b.classList.remove('visible'); setTimeout(function () { b.remove(); }, 400); }
  }
  window.acceptCookies = function () { store('accepted'); consent(true); close(); };
  window.declineCookies = function () { store('declined'); consent(false); close(); };
  window.closeCookieBanner = close;
  if (stored()) return;
  var banner = document.createElement('div');
  banner.id = 'cookieBanner';
  banner.setAttribute('role', 'region');
  banner.setAttribute('aria-label', 'Cookie choices');
  if (isEU) {
    banner.innerHTML = '<div class="cookie-text">We use analytics cookies (Google Analytics) to understand how the site is used. Nothing is stored until you choose. <a href="/privacy.html#cookies">Privacy choices</a></div>' +
      '<div class="cookie-buttons"><button class="cookie-btn cookie-accept" onclick="acceptCookies()">Accept</button><button class="cookie-btn cookie-decline" onclick="declineCookies()">Decline</button></div>';
  } else {
    banner.className = 'cookie-toast';
    banner.innerHTML = '<div class="cookie-text">This site uses analytics cookies. <a href="/privacy.html#cookies">Privacy choices</a></div>' +
      '<div class="cookie-buttons"><button class="cookie-btn cookie-accept" onclick="acceptCookies()" aria-label="Dismiss">OK</button></div>';
  }
  document.body.appendChild(banner);
  setTimeout(function () {
    banner.classList.add('visible');
    document.documentElement.style.setProperty('--mv-banner-h', (banner.offsetHeight || 0) + 'px');
  }, 800);
})();"""


COOKIE_CSS = """#cookieBanner { position: fixed; bottom: 0; left: 0; right: 0; z-index: 9990; background: var(--offblack, #1a1816); border-top: 1px solid rgba(196,162,101,0.2); padding: 20px var(--side-pad, 24px); display: flex; align-items: center; justify-content: space-between; gap: 24px; flex-wrap: wrap; transform: translateY(100%); transition: transform 0.4s ease; }
#cookieBanner.visible { transform: translateY(0); }
.cookie-text { font-family: var(--sans, 'Montserrat', sans-serif); font-size: 14px; line-height: 1.6; color: rgba(255,255,255,0.75); flex: 1; min-width: 260px; }
.cookie-text a { color: var(--terracotta, #C2714F); text-decoration: underline; }
.cookie-buttons { display: flex; gap: 12px; flex-shrink: 0; }
.cookie-btn { font-family: var(--sans, 'Montserrat', sans-serif); font-size: 13px; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; padding: 10px 24px; border: none; border-radius: 2px; cursor: pointer; transition: background 0.3s, color 0.3s; }
.cookie-accept { background: var(--terracotta, #C2714F); color: #fff; }
.cookie-accept:hover { background: #a8593e; }
.cookie-decline { background: transparent; color: rgba(255,255,255,0.6); border: 1px solid rgba(255,255,255,0.2); }
.cookie-decline:hover { color: #fff; border-color: rgba(255,255,255,0.5); }
body #cookieBanner.cookie-toast { left: auto; right: 16px; bottom: 16px; max-width: 380px; border: 1px solid rgba(196,162,101,0.3); border-radius: 4px; padding: 14px 16px; gap: 14px; flex-wrap: nowrap; }
body #cookieBanner.cookie-toast .cookie-text { font-size: 12.5px; min-width: 0; }
body #cookieBanner.cookie-toast .cookie-btn { padding: 8px 14px; font-size: 11px; }
@media (max-width: 600px) { #cookieBanner { flex-direction: column; text-align: center; padding: 20px 16px; } .cookie-buttons { width: 100%; justify-content: center; } body #cookieBanner.cookie-toast { left: 16px; max-width: none; flex-direction: row; text-align: left; } body #cookieBanner.cookie-toast .cookie-buttons { width: auto; } }"""


def cookie_widget() -> str:
    """Banner/toast + CSS per le pagine diverse da index.html (che ha già stili e JS propri)."""
    return f"<style>\n{COOKIE_CSS}\n</style>\n<script>\n{cookie_js()}\n</script>"


def video_js() -> str:
    return """<script>
// Hero video: loaded only on wide viewports (≥ 900px) and without reduced-motion; poster elsewhere.
(function () {
  var v = document.querySelector('video.hero-video[data-src]');
  if (!v) return;
  var wide = window.matchMedia && window.matchMedia('(min-width: 900px)').matches;
  var reduced = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (!wide || reduced) return;
  var src = document.createElement('source');
  src.src = v.getAttribute('data-src'); src.type = 'video/mp4';
  v.appendChild(src);
  v.load();
  v.play().catch(function () {});
})();
</script>"""


# ---------------------------------------------------------------------------
# Schema (JSON-LD) transform for index.html
# ---------------------------------------------------------------------------

def person_node(s: S) -> dict:
    return {
        "@type": "Person",
        "@id": f"{s.site}/team.html#paolo-mezzalama",
        "name": s.founder_name,
        "jobTitle": s.founder_title,
        "url": f"{s.site}/team.html#paolo-mezzalama",
        "worksFor": {"@id": f"{s.site}/#organization"},
        "sameAs": ["https://its.vision/en/about/paolo-mezzalama/"],
    }


def patch_schema(html: str, s: S, log: List[str]) -> str:
    m = re.search(r'(<script type="application/ld\+json">\s*)(\{.*?\})(\s*</script>)', html, re.S)
    if not m:
        log.append("  ! SCHEMA           SKIPPED (no JSON-LD block)")
        return html
    try:
        data = json.loads(m.group(2))
    except Exception as exc:
        log.append(f"  ! SCHEMA           SKIPPED (JSON-LD not parseable: {exc})")
        return html
    graph = data.get("@graph")
    if not isinstance(graph, list):
        log.append("  ! SCHEMA           SKIPPED (no @graph)")
        return html

    org_id = f"{s.site}/#organization"
    orgs = [n for n in graph if n.get("@type") == "Organization"]
    if not orgs:
        log.append("  ! SCHEMA           SKIPPED (no Organization node)")
        return html
    org = orgs[0]
    # Fold a LocalBusiness duplicate into the single Organization node
    lb = [n for n in graph if n.get("@type") == "LocalBusiness"]
    for n in lb:
        for k in ("address", "geo", "image"):
            if k in n and k not in org:
                org[k] = n[k]
        graph.remove(n)
    org["@id"] = org_id
    org["name"] = "My Villa"
    org["alternateName"] = "My Villa LA"
    org["url"] = s.site
    org["logo"] = {"@type": "ImageObject", "url": f"{s.site}/img/myvilla-logo.png"}
    org["email"] = s.email
    org["contactPoint"] = {"@type": "ContactPoint", "email": s.email, "contactType": "sales",
                           "availableLanguage": ["English", "Italian"]}
    org["sameAs"] = ["https://www.instagram.com/myvilla.la/", "https://x.com/myvilla_la",
                     "https://www.linkedin.com/company/myvilla-la/"]
    person = person_node(s)
    org["founder"] = {"@id": person["@id"]}
    # Person node (single)
    graph[:] = [n for n in graph if not (n.get("@type") == "Person" and n.get("@id") == person["@id"])]
    graph.insert(graph.index(org) + 1, person)
    # FAQ aligned with the visible list
    faq_nodes = [n for n in graph if n.get("@type") == "FAQPage"]
    faq_node = faq_nodes[0] if faq_nodes else {"@type": "FAQPage", "@id": f"{s.site}/#faq"}
    if not faq_nodes:
        graph.append(faq_node)
    faq_node["mainEntity"] = [
        {"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q, a in faqs(s)
    ]
    new_json = json.dumps(data, indent=2, ensure_ascii=False)
    if new_json == m.group(2):
        log.append("  = SCHEMA           unchanged")
        return html
    log.append("  ~ SCHEMA           updated (single Organization + Person + FAQ aligned)")
    return html[:m.start(2)] + new_json + html[m.end(2):]


# ---------------------------------------------------------------------------
# Patch sets
# ---------------------------------------------------------------------------

def set_page_type(html: str, page_type: str, log: List[str]) -> str:
    m = re.search(r"<body\b([^>]*)>", html)
    if not m:
        log.append("  ! BODY             SKIPPED (no <body>)")
        return html
    attrs = m.group(1)
    if re.search(r'data-page-type="[^"]*"', attrs):
        new_attrs = re.sub(r'data-page-type="[^"]*"', f'data-page-type="{page_type}"', attrs)
    else:
        new_attrs = f'{attrs} data-page-type="{page_type}"'
    if new_attrs == attrs:
        log.append("  = BODY             unchanged")
        return html
    log.append(f"  + BODY             data-page-type={page_type}")
    return html[:m.start()] + f"<body{new_attrs}>" + html[m.end():]


def apply_shared(html: str, s: S, page_type: str, log: List[str], with_form: bool,
                 with_cookie_ui: bool = False, with_track: bool = True) -> str:
    html = upsert(html, "CONSENT", consent_js(s),
                  after(r"function gtag\(\)\{dataLayer\.push\(arguments\);\}\n"), log, JS_C)
    # gtag('config') con debug_mode quando la sessione è di test (?mv_debug=1) → DebugView GA4
    html = upsert(html, "GACONFIG",
                  f"  gtag('config', '{s.ga4}', window.__mvDebug ? {{ debug_mode: true }} : {{}});",
                  replace(r"[ \t]*gtag\('config',\s*'" + re.escape(s.ga4) + r"'\);"), log, JS_C)
    html = set_page_type(html, page_type, log)
    if with_form:
        html = upsert(html, "FORMJS", formjs(s), before_body_end(), log)
    if with_track:
        html = upsert(html, "MVTRACK", mvtrack_js(), before_body_end(), log)
    html = upsert(html, "READTRACK", readtrack_js(), before_body_end(), log)
    if with_cookie_ui:
        html = upsert(html, "COOKIE_UI", cookie_widget(), before_body_end(), log)
    return html


def apply_index(html: str, s: S, log: List[str]) -> str:
    html = apply_shared(html, s, "home", log, with_form=True)

    # CSS — appended at the end of the main <style> block (first </style>)
    html = upsert(html, "CSS", conv_css(s), before(r"</style>"), log, JS_C)

    # NAV
    nav = f'<a href="{s.landing("nav")}" class="nav-cta" data-ev="cta_click" data-cta-id="nav_briefing">Request Briefing</a>'
    html = upsert(html, "NAV", nav, replace(r'<a href="#(?:contact|briefing)" class="nav-cta">[^<]*</a>'), log)
    nav_m = f'<a href="{s.landing("nav_mobile")}" onclick="closeMobile()" data-ev="cta_click" data-cta-id="nav_mobile_briefing">Request Briefing</a>'
    html = upsert(html, "NAV_MOBILE", nav_m,
                  replace(r'<a href="#(?:contact|briefing)" onclick="closeMobile\(\)">Request Briefing</a>'), log)

    # CONTACT — voce di menu (desktop + mobile) e blocco nel footer (2026-09-17)
    html = upsert(html, "NAV_CONTACT",
                  '<a href="#contact" data-ev="cta_click" data-cta-id="nav_contact">Contact</a>',
                  before(re.escape("<!-- CONV:NAV:START -->")), log)
    html = upsert(html, "NAV_MOBILE_CONTACT",
                  '<a href="#contact" onclick="closeMobile()">Contact</a>',
                  before(re.escape("<!-- CONV:NAV_MOBILE:START -->")), log)
    # SOCIAL (2026-09-22): icone Instagram + LinkedIn nell'header (desktop e menu mobile)
    nav_social = (f'<span class="nav-social" aria-label="Social profiles">'
                  f'<a href="{SOCIAL_IG}" target="_blank" rel="noopener" aria-label="My Villa on Instagram" title="Instagram" data-ev="social_click" data-cta-id="nav_instagram">{SVG_IG}</a>'
                  f'<a href="{SOCIAL_LI}" target="_blank" rel="noopener" aria-label="My Villa on LinkedIn" title="LinkedIn" data-ev="social_click" data-cta-id="nav_linkedin">{SVG_LI}</a>'
                  f'</span>')
    html = upsert(html, "NAV_SOCIAL", nav_social, before(re.escape("<!-- CONV:NAV:START -->")), log)
    mobile_social = (f'<div class="mobile-social" aria-label="Social profiles">'
                     f'<a href="{SOCIAL_IG}" target="_blank" rel="noopener" aria-label="My Villa on Instagram" data-ev="social_click" data-cta-id="nav_mobile_instagram">{SVG_IG}</a>'
                     f'<a href="{SOCIAL_LI}" target="_blank" rel="noopener" aria-label="My Villa on LinkedIn" data-ev="social_click" data-cta-id="nav_mobile_linkedin">{SVG_LI}</a>'
                     f'</div>')
    html = upsert(html, "NAV_MOBILE_SOCIAL", mobile_social, before(re.escape("<!-- CONV:NAV_MOBILE:START -->")), log)
    html = upsert(html, "FOOTER_LINK_CONTACT", '        <a href="research/westside-rebuild-tracker.html">Research</a>\n        <a href="#contact">Contact</a>',
                  before(r'\n      </nav>\n    </div>\n    <div class="footer-bottom">'), log)
    footer_contact = f"""    <div class="footer-contact" id="footer-contact">
      <span class="footer-contact-label">Contact</span>
      <a href="mailto:info@myvilla.la" data-ev="contact_email_click">info@myvilla.la</a>
      <span class="footer-contact-sep">&middot;</span>
      <a href="{s.landing("footer")}" data-ev="cta_click" data-cta-id="footer_briefing">Request a private briefing</a>
      <span class="footer-contact-sep">&middot;</span>
      <a href="#contact">Contact form</a>
      <span class="footer-social" aria-label="Social profiles">
        <span class="footer-contact-label">Follow</span>
        <a href="{SOCIAL_IG}" target="_blank" rel="noopener" aria-label="My Villa on Instagram" title="Instagram" data-ev="social_click" data-cta-id="footer_instagram">{SVG_IG}</a>
        <a href="{SOCIAL_LI}" target="_blank" rel="noopener" aria-label="My Villa on LinkedIn" title="LinkedIn" data-ev="social_click" data-cta-id="footer_linkedin">{SVG_LI}</a>
      </span>
    </div>"""
    html = upsert(html, "FOOTER_CONTACT", footer_contact, before(r'    <div class="footer-bottom">'), log)

    # RESEARCH — blocco "My Villa Research" nella sezione Journal della home (2026-09-17)
    research = """    <div class="journal-research reveal reveal-delay-2" id="research" aria-label="My Villa Research">
      <div class="journal-research-label">My Villa Research &middot; open data</div>
      <div class="journal-research-grid">
        <a class="journal-research-card" href="research/westside-rebuild-tracker.html" data-ev="cta_click" data-cta-id="journal_research_tracker">
          <span class="jr-kicker">Westside Rebuild Tracker</span>
          <span class="jr-title">Every new-home permit filed on the Westside since the January 2025 fires, by area, status and construction type</span>
          <span class="jr-meta">Los Angeles open data &middot; refreshed every Monday &middot; CSV and JSON to download</span>
        </a>
        <a class="journal-research-card" href="insurable-home-california.html" data-ev="cta_click" data-cta-id="journal_research_insurable">
          <span class="jr-kicker">Insurable home in California</span>
          <span class="jr-title">What the 2026 data says: premiums, discounts, Zone 0 and how insurers read a concrete home</span>
          <span class="jr-meta">Every figure sourced from a Journal note with a primary reference</span>
        </a>
      </div>
    </div>
"""
    html = upsert(html, "JOURNAL_RESEARCH", research, before(r'    <div class="journal-cta reveal reveal-delay-3">'), log)

    # HERO CTAs — right after the hero-sub element
    hero = f"""    <div class="hero-ctas reveal reveal-delay-3">
      <a href="{s.landing("hero")}" class="hero-btn hero-btn-primary" data-ev="cta_click" data-cta-id="hero_briefing">Request a Private Briefing</a>
      <a href="#collection" class="hero-btn hero-btn-ghost" data-ev="cta_click" data-cta-id="hero_collection">See the Collection</a>
    </div>"""
    html = upsert(html, "HERO", hero,
                  after(r'<(div|p) class="hero-sub[^"]*"[^>]*>.*?</\1>\n', re.S), log)

    # VIDEO — no autoplay/source in markup; loaded by JS on wide viewports only
    video = """  <video class="hero-video" muted loop playsinline preload="none" poster="img/hero.webp" data-src="vid/backyard.mp4" aria-hidden="true"></video>"""
    html = upsert(html, "VIDEO", video,
                  replace(r'[ \t]*<video class="hero-video"[^>]*>.*?</video>', re.S), log)
    html = upsert(html, "VIDEOJS", video_js(), before_body_end(), log)

    # Contextual CTAs
    ctx = {
        "RESILIENCE": ("offblack", "Curious what an insurable, non-combustible villa would mean on your lot?", "resilience"),
        "PROCESS": ("espresso", "Twenty months from a conversation to keys. The conversation starts here.", "process"),
        "TEAM": ("charcoal", "Meet the people who would design your villa. A 30-minute call, no obligation.", "team"),
    }
    for key, (bg, text, src) in ctx.items():
        block = f"""<aside class="ctx-cta ctx-cta--{bg}" aria-label="Request a private briefing">
  <div class="ctx-cta-inner">
    <p class="ctx-cta-text">{text}</p>
    <a href="{s.landing(src)}" class="ctx-cta-btn" data-ev="cta_click" data-cta-id="ctx_{src}">Request a Private Briefing</a>
  </div>
</aside>"""
        html = upsert(html, f"CTX_{key}", "\n" + block, after_section_close(src), log)

    # Trust
    html = upsert(html, "TRUST_LABEL", '<div class="material-legacy-label reveal">Completed Works</div>',  # ripristinato (2026-09-17)
                  replace(r'<div class="material-legacy-label reveal">[^<]*</div>'), log)
    note = ""  # nota di trasparenza rimossa (decisione 2026-09-17: da chiarire di persona)

    html = upsert(html, "TRUST_NOTE", note, before(r'[ \t]*<!-- Amanvari: current project -->'), log)

    # FAQ — before the closing of the Investment section (inside invest-inner)
    def _faq_loc(h):
        m = re.search(r'<section[^>]*\bid="investment"[^>]*>', h)
        if not m:
            return None
        end = h.find("</section>", m.end())
        if end == -1:
            return None
        inner = h.rfind("</div>", m.end(), end)
        if inner == -1:
            return None
        return (inner, inner)
    html = upsert(html, "FAQ", faq_html(s) + "\n", _faq_loc, log)

    # Schema
    html = patch_schema(html, s, log)

    # Form — replace the whole #contactForm element, keeping id + onsubmit
    form = canonical_form(s, form_id="home", source_page="index", page_type="home",
                          subject="Private Briefing Request — myvilla.la", dom_id="contactForm", onsubmit=True)
    html = upsert(html, "FORM", form, replace(r'<form[^>]*\bid="contactForm"[^>]*>.*?</form>', re.S), log)
    # Success copy: allinea qualsiasi frase-promessa precedente al testo canonico
    new_promise = f"{s.promise_line} You will receive a confirmation by email in the meantime."
    pat = re.compile(r"(?:Our founding partner will personally respond within 48 hours with next steps for your private briefing\.|"
                     r"Paolo Mezzalama replies personally within one business day\. You will receive a confirmation by email in the meantime\.|"
                     r"We reply within one business day[^<]*?You will receive a confirmation by email in the meantime\.)")
    if pat.search(html) and new_promise not in html:
        html = pat.sub(new_promise, html, count=1)
        log.append("  ~ PROMISE          success copy aligned to canonical response promise")

    # Old submit code → replaced by the shared FORMJS block
    html = upsert(html, "FORM_LEGACY", "// Form submit: see the CONV:FORMJS block at the end of the page (dual-post Formspree + lead API, redirect to thank-you).",
                  replace(r"// Form — sends data via Formspree\n.*?(?=\n// Smooth anchor scrolling)", re.S), log, JS_C)

    # Cookie banner
    html = upsert(html, "COOKIE", cookie_js(),
                  replace(r"// ══════════════ GDPR COOKIE BANNER ══════════════\n.*?function closeCookieBanner\(\) \{.*?\n\}", re.S),
                  log, JS_C)

    # Sticky CTA
    sticky = f'<a href="{s.landing("sticky")}" class="sticky-cta" id="stickyCta" data-ev="cta_click" data-cta-id="sticky_briefing">Private Briefing</a>'
    html = upsert(html, "STICKY", sticky, replace(r'<a href="#briefing" class="sticky-cta" id="stickyCta">[^<]*</a>'), log)
    return html


# ---------------------------------------------------------------------------
# Sanity checks
# ---------------------------------------------------------------------------

def sanity(html: str, s: S, is_index: bool) -> List[str]:
    problems = []
    if s.ga4 not in html or "googletagmanager.com/gtag/js" not in html:
        problems.append("gtag block missing")
    if is_index:
        for mk in ("INSURANCE", "FIRE_CODE", "REBUILD", "MARKET"):
            if f"<!-- DESK:{mk}:START -->" not in html or f"<!-- DESK:{mk}:END -->" not in html:
                problems.append(f"DESK marker {mk} missing")
        for el_id in ("contactForm", "ctaSuccess", "stickyCta", "nav", "burger", "mobileMenu"):
            if f'id="{el_id}"' not in html:
                problems.append(f"id {el_id} missing")
    return problems


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def run_file(path: Path, s: S, mode: str, dry_run: bool) -> int:
    log: List[str] = []
    try:
        original = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"[{path}] impossibile leggere: {exc}")
        return 0
    page_type = PAGE_TYPES.get(path.name, "page")
    rel = str(path.resolve()).replace(str(ROOT.resolve()), "").lstrip("/")
    journal = rel.startswith("blog/")
    if journal:
        page_type = "journal" if path.name == "index.html" or "/category/" in rel else "article"
    elif rel.startswith("research/"):
        page_type = "research"
    if mode == "index":
        new = apply_index(original, s, log)
    else:
        has_form = bool(re.search(r"<form\b", original))
        has_banner = "id = 'cookieBanner'" in original or 'id="cookieBanner"' in original
        # Sul Journal gli articoli hanno già il proprio listener [data-ev]: niente MVTRACK (eviterebbe eventi doppi)
        new = apply_shared(original, s, page_type, log, with_form=has_form,
                           with_cookie_ui=not has_banner, with_track=not journal)
    print(f"[{path.relative_to(ROOT) if str(path).startswith(str(ROOT)) else path}] mode={mode} page_type={page_type}")
    print("\n".join(log))
    probs = sanity(new, s, mode == "index")
    for p in probs:
        print(f"  !! SANITY: {p}")
    if new == original:
        print("  → nessuna modifica")
        return 0
    if dry_run:
        diff = difflib.unified_diff(original.splitlines(), new.splitlines(), fromfile=str(path), tofile=f"{path} (patched)", lineterm="", n=2)
        out = "\n".join(diff)
        print(out[:20000] + ("\n… [diff troncato]" if len(out) > 20000 else ""))
        print("  → DRY RUN: nessuna scrittura")
        return 0
    if probs:
        print("  → SANITY FAILED: file NON scritto")
        return 0
    path.write_text(new, encoding="utf-8")
    print(f"  → scritto ({len(original)} → {len(new)} byte)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--target", default="index.html", help="pagina a cui applicare il set completo (default index.html)")
    ap.add_argument("--shared", nargs="*", default=None, help="pagine a cui applicare solo gli snippet condivisi")
    ap.add_argument("--sweep", action="store_true",
                    help="tutte le pagine servite (root, blog/**, research/**) prive di CONSENT/GACONFIG: applica gli snippet condivisi (idempotente, per la pipeline quotidiana)")
    ap.add_argument("--dry-run", action="store_true", help="mostra il diff senza scrivere")
    ap.add_argument("--print-form", metavar="FORM_ID", help="stampa il markup del form canonico con questo form_id ed esce")
    ap.add_argument("--print-form-css", action="store_true", help="stampa il CSS completo del form canonico ed esce")
    args = ap.parse_args(argv)

    s = S(load_settings())
    if args.print_form:
        print(canonical_form(s, form_id=args.print_form, source_page=args.print_form, page_type="landing",
                             subject="Private Briefing Request — myvilla.la", dom_id="briefForm"))
        return 0
    if args.print_form_css:
        print(FORM_BASE_CSS)
        return 0

    if args.sweep:
        pages = sorted(set(list(ROOT.glob("*.html")) + list(ROOT.glob("blog/**/*.html")) + list(ROOT.glob("research/**/*.html"))))
        todo = []
        for pth in pages:
            if pth.name == "index.html" and pth.parent == ROOT:
                continue  # la home ha il set completo (--target)
            try:
                txt = pth.read_text(encoding="utf-8")
            except Exception:
                continue
            if "googletagmanager.com/gtag/js" not in txt:
                continue  # pagine senza GA4 (pannelli privati): non toccarle
            if ("CONV:CONSENT:START" in txt and "CONV:GACONFIG:START" in txt and ("cookieBanner" in txt)
                    and "CONV:READTRACK:START" in txt):
                continue
            todo.append(pth)
        print(f"[sweep] {len(pages)} pagine, {len(todo)} da allineare")
        for pth in todo:
            run_file(pth, s, "shared", args.dry_run)
        return 0
    if args.shared is not None:
        for f in args.shared:
            p = Path(f) if Path(f).is_absolute() else ROOT / f
            run_file(p, s, "shared", args.dry_run)
        return 0

    target = Path(args.target) if Path(args.target).is_absolute() else ROOT / args.target
    if not target.exists():
        print(f"target non trovato: {target}")
        return 0
    return run_file(target, s, "index", args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
