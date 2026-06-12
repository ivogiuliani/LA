#!/usr/bin/env python3
"""
My Villa — Content Review Server
Local HTTP server for reviewing, approving, or rejecting draft content.

Usage:
  python3 approve.py [--port 8787] [--no-browser]
"""

import argparse
import base64
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import webbrowser
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from api_health_banner import (
    render_api_health_banner_html,
    CSS_LIGHT as API_HEALTH_CSS,
)

# ── Paths ────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).resolve().parent

# ── Auto-model: tier risolto via model_resolver — upgrade automatico
# ai modelli più recenti appena compaiono su /v1/models (policy Ivo
# 2026-06-10). Fallback hardcoded se il resolver non è importabile:
# il modello non deve MAI bloccare la pipeline.
try:
    import sys as _sys
    if str(SCRIPT_DIR) not in _sys.path:
        _sys.path.insert(0, str(SCRIPT_DIR))
    from model_resolver import resolve as _resolve_model
except Exception:  # noqa: BLE001
    def _resolve_model(tier, _fb={"writer": "claude-fable-5",
                                  "heavy": "claude-opus-4-8",
                                  "balanced": "claude-sonnet-4-6",
                                  "cheap": "claude-haiku-4-5"}):
        return _fb.get(tier, "claude-sonnet-4-6")
_WRITER_MODEL = _resolve_model("writer")
SYSTEM_DIR = SCRIPT_DIR.parent
ROOT_DIR = SYSTEM_DIR.parent
DRAFTS_DIR = ROOT_DIR / "_drafts"
BLOG_DIR = ROOT_DIR / "blog"
ARCHIVE_DIR = ROOT_DIR / "_archive"


# ── Auto-archive stale drafts ────────────────────────────────────────
#
# Background: the radar generates ~10 social drafts/day (5 X + 5 IG),
# the journal pipeline occasionally drops articles, and the reply
# monitor produces email-reply drafts on incoming traffic. Without
# pruning, the home dashboard accumulates hundreds of stale items that
# the operator scrolls past every morning.
#
# Policy: at every review-server startup, sweep _drafts/ and move any
# file older than AUTO_ARCHIVE_DAYS to its corresponding _archive/
# subfolder. Non-destructive: every file is moved (shutil.move), never
# deleted. Operator can fish a file back out of _archive/<type>/ if
# they change their mind.

AUTO_ARCHIVE_DAYS = 14   # default for journal + email_replies

# Source draft folder → (extensions to consider, threshold in days).
# Per-type thresholds because the content types have very different
# half-lives:
#   - social reactive: 1 day (24h). Tightened from 7d on 2026-05-14 per
#     operator: a social post about news older than 24h reads as late;
#     the team should react fresh or skip. The mtime of the draft file
#     is ~the publication date of the source article (radar generates
#     within ~24h of finding the article), so 1-day mtime threshold
#     approximates "source published in the last day or two".
#   - journal articles: 14 days. They take longer to refine and the
#     operator may sit on a piece for two weeks before publishing.
#   - email_replies: 14 days. Journalist responses worth following up
#     for a couple of weeks; older = stale conversation.
_AUTO_ARCHIVE_TARGETS = (
    ("journal",        (".html", ".json"),  14),
    ("social",         (".md",),             1),
    ("email_replies",  (".json",),          14),
)


def auto_generate_ig_companions(verbose=True):
    """For every journal draft in _drafts/journal/<slug>.html, ensure a
    sibling <slug>.ig.md companion exists. Generate via Anthropic if it
    doesn't.

    Runs at server startup so the operator never has to click the
    "Genera Instagram companion" button manually — the companion is
    ready inline on the card by the time the dashboard loads.

    Cost: ~$0.01-0.02 per generation (Sonnet, ~300 token I/O). Skipped
    if the companion already exists, so steady-state cost is zero.

    Failures are logged per-article and the sweep continues; one bad
    sidecar (or one Anthropic blip) shouldn't block the server start.
    Returns a dict {slug: result} for the boot banner.
    """
    journal_dir = DRAFTS_DIR / "journal"
    if not journal_dir.exists():
        return {}

    # Build the to-generate list first so we can summarise upfront.
    pending = []
    for html_file in journal_dir.glob("*.html"):
        stem = html_file.stem
        companion = journal_dir / f"{stem}.ig.md"
        if companion.exists():
            continue
        sidecar = html_file.with_suffix(".json")
        if not sidecar.exists():
            if verbose:
                print(f"  [ig-companion] skip {stem}: no .json sidecar")
            continue
        pending.append((stem, sidecar, companion))

    if not pending:
        return {}

    if verbose:
        print(f"  [ig-companion] generating {len(pending)} missing companion(s)...")

    script = SCRIPT_DIR / "generate_ig_companion.py"
    if not script.exists():
        if verbose:
            print(f"  [ig-companion] generator script not found at {script}; skipping")
        return {}

    summary = {}
    for stem, sidecar, companion in pending:
        try:
            r = subprocess.run(
                [sys.executable, str(script),
                 "--article", str(sidecar),
                 "--output",  str(companion)],
                capture_output=True, text=True, timeout=60,
            )
            if r.returncode == 0:
                summary[stem] = "ok"
                if verbose:
                    print(f"  [ig-companion] ✓ {stem}")
            else:
                err = (r.stderr or r.stdout or "?").strip()[:200]
                summary[stem] = f"fail: {err}"
                if verbose:
                    print(f"  [ig-companion] ✗ {stem}: {err}")
        except subprocess.TimeoutExpired:
            summary[stem] = "timeout"
            if verbose:
                print(f"  [ig-companion] ✗ {stem}: timeout (>60s)")
        except Exception as e:  # noqa: BLE001
            summary[stem] = f"error: {type(e).__name__}"
            if verbose:
                print(f"  [ig-companion] ✗ {stem}: {type(e).__name__}: {e}")
    return summary


def auto_archive_old_drafts(threshold_days=None, verbose=True):
    """Move stale drafts from _drafts/<type>/ to _archive/<type>/.

    Runs at server startup. Returns a dict {type: count_moved} so the
    boot banner can summarise. Filenames are preserved; if a file with
    the same name already exists in _archive/ (rare), a numeric suffix
    is added so we never silently overwrite.

    `threshold_days`:
      - None (default): use the per-type threshold encoded in
        _AUTO_ARCHIVE_TARGETS (journal=14, social=7, replies=14).
      - int: override across all types (used by --archive-days CLI flag
        for one-off cleanups; e.g. --archive-days 3 to aggressively
        compact the dashboard before a demo).

    Errors per-file are logged and the sweep continues — one corrupt
    file shouldn't block the entire archive run.
    """
    import time as _time

    moved_summary = {}
    drafts_root = ROOT_DIR / "_drafts"
    now = _time.time()

    for type_name, exts, type_threshold in _AUTO_ARCHIVE_TARGETS:
        # CLI override beats the per-type default.
        effective_threshold = threshold_days if threshold_days is not None else type_threshold
        cutoff = now - (effective_threshold * 86400)

        src_dir = drafts_root / type_name
        if not src_dir.exists():
            continue
        dst_dir = ARCHIVE_DIR / type_name
        count = 0
        for f in src_dir.iterdir():
            if not f.is_file():
                continue
            if exts and f.suffix.lower() not in exts:
                continue
            try:
                if f.stat().st_mtime >= cutoff:
                    continue
                dst_dir.mkdir(parents=True, exist_ok=True)
                target = dst_dir / f.name
                # Disambiguate name clashes without overwriting.
                if target.exists():
                    stem, sfx = target.stem, target.suffix
                    i = 1
                    while target.exists():
                        target = dst_dir / f"{stem}.archived{i}{sfx}"
                        i += 1
                shutil.move(str(f), str(target))
                count += 1
            except Exception as e:  # noqa: BLE001 — log + continue
                if verbose:
                    print(f"  [auto-archive] could not move {f.name}: {e}")
        if count > 0:
            moved_summary[type_name] = count
            if verbose:
                print(f"  [auto-archive] {type_name}: moved {count} drafts "
                      f"older than {effective_threshold}d → _archive/{type_name}/")
    return moved_summary


# ── .env loader (same pattern as other scripts) ──────────────────────
def load_dotenv():
    env_file = ROOT_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            k, v = key.strip(), value.strip()
            if v and (k not in os.environ or not os.environ[k]):
                os.environ[k] = v


load_dotenv()

# Make sibling scripts importable for re-rendering on save
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# Live link-check gate: block publish if any source URL is 404/dead.
try:
    from validate_links import process_file as validate_links_file
    VALIDATE_LINKS_OK = True
except ImportError:
    VALIDATE_LINKS_OK = False

# ── WYSIWYG inline editor injection ─────────────────────────────────

def _build_wysiwyg_injection(filename: str, in_blog: bool = False) -> str:
    """Return CSS + HTML + JS to inject before </body> in a preview page,
    transforming it into an inline WYSIWYG editor.

    `in_blog`: True when the file being edited is in blog/ (already
    published). The injected toolbar shows a different "live mode"
    banner so the operator knows their edits will go straight to the
    public site (no draft → publish round-trip). Actual save logic is
    the same — /api/save_draft now falls back to blog/ when the file
    isn't in _drafts/journal/.
    """
    # We avoid Python f-strings for the JS/CSS blocks to prevent
    # escaping hell with JS template-literals and CSS braces.
    # Instead we use one FILENAME placeholder.
    raw = r"""
<!-- ═══ WYSIWYG EDITOR ═══ -->
<style>
.wysiwyg-fab{position:fixed;bottom:32px;right:32px;z-index:500;width:56px;height:56px;border-radius:50%;background:#C2714F;color:#fff;border:none;cursor:pointer;font-size:22px;box-shadow:0 4px 20px rgba(0,0,0,.3);transition:all .3s;display:flex;align-items:center;justify-content:center;}
.wysiwyg-fab:hover{transform:scale(1.1);background:#3E2F2B;}
body.editing .wysiwyg-fab{display:none;}

.wysiwyg-toolbar{position:fixed;top:0;left:0;right:0;z-index:200;background:#3E2F2B;padding:12px 24px;display:none;align-items:center;justify-content:space-between;box-shadow:0 4px 20px rgba(0,0,0,.3);}
body.editing .wysiwyg-toolbar{display:flex;}
body.editing .nav{top:52px!important;}
body.editing .article-hero{padding-top:160px!important;}
@media(max-width:768px){body.editing .article-hero{padding-top:140px!important;}}

.wysiwyg-toolbar-label{color:#C2714F;font-size:12px;font-weight:600;letter-spacing:.15em;text-transform:uppercase;}
.wysiwyg-toolbar-actions{display:flex;gap:12px;align-items:center;}
.wysiwyg-btn{padding:8px 20px;border-radius:4px;border:none;cursor:pointer;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;transition:all .3s;}
.wysiwyg-btn-save{background:#C2714F;color:#fff;}
.wysiwyg-btn-save:hover{background:#6B8F9E;}
.wysiwyg-btn-save:disabled{opacity:.5;cursor:not-allowed;}
.wysiwyg-btn-cancel{background:transparent;color:rgba(255,255,255,.7);border:1px solid rgba(255,255,255,.2);}
.wysiwyg-btn-cancel:hover{color:#fff;border-color:#fff;}
.wysiwyg-save-status{color:rgba(255,255,255,.6);font-size:11px;letter-spacing:.06em;margin-right:8px;}

body.editing [data-editable]{outline:2px dashed rgba(194,113,79,.25);outline-offset:4px;border-radius:2px;min-height:1em;cursor:text;}
body.editing [data-editable]:focus{outline:2px solid #C2714F;outline-offset:4px;}
body.editing [data-editable]:hover:not(:focus){outline-color:rgba(194,113,79,.5);}

body.editing .reveal{opacity:1!important;transform:none!important;}

.wysiwyg-change-image{display:none;position:absolute;bottom:16px;right:16px;padding:10px 20px;border-radius:4px;border:none;background:rgba(62,47,43,.85);color:#fff;font-size:11px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;transition:all .3s;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);box-shadow:0 2px 12px rgba(0,0,0,.3);z-index:10;}
.wysiwyg-change-image:hover{background:rgba(194,113,79,.9);}
body.editing .wysiwyg-change-image{display:flex;align-items:center;gap:8px;}
.wysiwyg-change-image svg{width:16px;height:16px;fill:none;stroke:#fff;stroke-width:1.5;}

/* Image picker modal (reused) */
.wys-img-backdrop{display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:600;align-items:center;justify-content:center;}
.wys-img-backdrop.show{display:flex;}
.wys-img-modal{background:#FAF8F5;border-radius:8px;width:90vw;max-width:900px;max-height:85vh;overflow-y:auto;padding:32px;position:relative;}
.wys-img-modal h3{font-family:'Cormorant Garamond',Georgia,serif;font-size:24px;color:#3E2F2B;margin-bottom:16px;}
.wys-img-search{display:flex;gap:10px;margin-bottom:16px;}
.wys-img-search input{flex:1;padding:10px 14px;border:1px solid #EDE6DC;border-radius:4px;font-size:14px;}
.wys-img-search button{padding:10px 20px;background:#3E2F2B;color:#fff;border:none;border-radius:4px;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;cursor:pointer;}
.wys-img-status{font-size:13px;color:#A09890;margin-bottom:12px;}
.wys-img-section{margin-bottom:16px;}
.wys-img-section-label{font-size:11px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:#C2714F;margin-bottom:10px;padding-bottom:6px;border-bottom:1px solid #EDE6DC;}
.wys-img-section-label .hint{color:#A09890;font-weight:400;letter-spacing:.04em;text-transform:none;font-size:10px;}
.wys-img-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;}
@media(max-width:700px){.wys-img-grid{grid-template-columns:repeat(2,1fr);}}
.wys-img-card{position:relative;cursor:pointer;border-radius:6px;overflow:hidden;border:3px solid transparent;transition:border-color .2s;}
.wys-img-card.selected{border-color:#C2714F;}
.wys-img-card img{width:100%;height:140px;object-fit:cover;display:block;}
.wys-img-card-cap{padding:6px 8px;font-size:10px;color:#A09890;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;background:#fff;}
.wys-img-badge{position:absolute;top:6px;left:6px;font-size:9px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;padding:3px 8px;border-radius:3px;color:#fff;}
.badge-source{background:rgba(92,107,79,.85);}
.badge-unsplash{background:rgba(62,47,43,.85);}
.wys-img-actions{display:flex;gap:12px;justify-content:flex-end;margin-top:20px;padding-top:16px;border-top:1px solid #EDE6DC;}
.wys-img-close{padding:10px 20px;background:transparent;color:#3E2F2B;border:1px solid #EDE6DC;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;}
.wys-img-confirm{padding:10px 24px;background:#C2714F;color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;}
.wys-img-confirm:disabled{opacity:.5;cursor:not-allowed;}

/* Excerpt editor */
.wysiwyg-excerpt-wrap{display:none;max-width:680px;margin:0 auto 24px;padding:16px;background:#fff;border:1px solid #EDE6DC;border-radius:4px;}
body.editing .wysiwyg-excerpt-wrap{display:block;}
.wysiwyg-excerpt-wrap label{font-size:10px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:#A09890;display:block;margin-bottom:6px;}
.wysiwyg-excerpt-wrap div[data-editable]{font-size:15px;line-height:1.6;color:#2C2C2C;padding:8px;min-height:3em;}
</style>

<!-- FAB -->
<button class="wysiwyg-fab" onclick="enterEditMode()" title="Modifica articolo">&#9998;</button>

<!-- Toolbar -->
<div class="wysiwyg-toolbar">
  <span class="wysiwyg-toolbar-label">Modalit&agrave; modifica</span>
  <div class="wysiwyg-toolbar-actions">
    <span class="wysiwyg-save-status" id="wys-save-status"></span>
    <button class="wysiwyg-btn wysiwyg-btn-cancel" onclick="cancelEdit()">Annulla</button>
    <button class="wysiwyg-btn wysiwyg-btn-save" id="wys-save-btn" onclick="saveArticle()">Salva</button>
  </div>
</div>

<!-- Image picker modal -->
<div class="wys-img-backdrop" id="wys-img-backdrop">
  <div class="wys-img-modal">
    <h3>Scegli immagine hero</h3>
    <div class="wys-img-search">
      <input type="text" id="wys-img-query" placeholder="Query (vuoto = automatica)">
      <button id="wys-img-search-btn" onclick="wysSearchImages()">Cerca</button>
    </div>
    <div class="wys-img-status" id="wys-img-status">Premi Cerca per trovare immagini.</div>
    <div id="wys-img-grid-wrap"></div>
    <div class="wys-img-actions">
      <button class="wys-img-close" onclick="wysCloseImagePicker()">Annulla</button>
      <button class="wys-img-confirm" id="wys-img-confirm" disabled onclick="wysConfirmImage()">Usa questa immagine</button>
    </div>
  </div>
</div>

<script>
(function(){
  const DRAFT_FILE = '%%FILENAME%%';
  const JSON_FILE = DRAFT_FILE.replace(/\.html$/, '.json');
  let isEditing = false;
  let imgCandidates = [];
  let imgSelectedIdx = -1;

  // ── Enter/exit edit mode ──
  window.enterEditMode = function() {
    isEditing = true;
    document.body.classList.add('editing');

    // Make key elements editable
    const title = document.querySelector('.article-hero-title');
    const subtitle = document.querySelector('.article-hero-subtitle');
    const body = document.querySelector('.article-content');
    const persp = document.querySelector('.perspective-text');

    if (title) { title.setAttribute('data-editable', 'title'); title.contentEditable = 'true'; }
    if (subtitle) { subtitle.setAttribute('data-editable', 'subtitle'); subtitle.contentEditable = 'true'; }
    if (persp) { persp.setAttribute('data-editable', 'our_perspective'); persp.contentEditable = 'true'; }

    // Body: make individual children editable, but NOT the perspective block
    if (body) {
      Array.from(body.children).forEach(function(child) {
        if (child.classList.contains('perspective')) return;
        child.setAttribute('data-editable', 'body');
        child.contentEditable = 'true';
      });
    }

    // Force all reveals visible
    document.querySelectorAll('.reveal').forEach(function(el) { el.classList.add('visible'); });

    // Insert excerpt editor before article-body
    insertExcerptEditor();

    // Insert "Cambia immagine" button
    insertChangeImageBtn();

    // Scroll to top
    window.scrollTo({top: 0, behavior: 'smooth'});
  };

  window.cancelEdit = function() {
    if (!confirm('Annullare le modifiche?')) return;
    location.reload();
  };

  // ── Excerpt editor ──
  function insertExcerptEditor() {
    if (document.getElementById('wys-excerpt')) return;
    const wrap = document.createElement('div');
    wrap.className = 'wysiwyg-excerpt-wrap';
    wrap.id = 'wys-excerpt-wrap';
    wrap.innerHTML = '<label>Excerpt (anteprima nella lista)</label>' +
      '<div id="wys-excerpt" data-editable="excerpt" contenteditable="true"></div>';
    const artBody = document.querySelector('.article-body');
    if (artBody) artBody.insertBefore(wrap, artBody.firstChild.nextSibling);
    // Load current excerpt from JSON
    fetch('/api/get_draft', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({file: JSON_FILE, type:'journal'})
    }).then(function(r){ return r.json(); }).then(function(resp) {
      if (resp.ok && resp.data) {
        var ex = document.getElementById('wys-excerpt');
        if (ex) ex.textContent = resp.data.excerpt || '';
      }
    });
  }

  // ── Change image button ──
  function insertChangeImageBtn() {
    if (document.getElementById('wys-change-img')) return;
    var cameraIcon = '<svg viewBox="0 0 24 24"><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z"/><circle cx="12" cy="13" r="4"/></svg>';

    var wrap = document.querySelector('.hero-image-wrap') || document.querySelector('.hero-image');
    if (wrap) {
      // Ensure wrapper is position:relative for absolute overlay
      wrap.style.position = 'relative';
      var btn = document.createElement('button');
      btn.className = 'wysiwyg-change-image';
      btn.id = 'wys-change-img';
      btn.innerHTML = cameraIcon + 'Cambia immagine';
      btn.onclick = wysOpenImagePicker;
      wrap.appendChild(btn);
    } else {
      // No hero image yet — create a placeholder area after article-hero
      var heroSection = document.querySelector('.article-hero');
      if (!heroSection) return;
      var placeholder = document.createElement('div');
      placeholder.className = 'hero-image-wrap';
      placeholder.style.position = 'relative';
      placeholder.style.background = 'var(--espresso)';
      placeholder.style.padding = '40px';
      placeholder.style.textAlign = 'center';
      var btn = document.createElement('button');
      btn.className = 'wysiwyg-change-image';
      btn.id = 'wys-change-img';
      btn.style.position = 'relative';
      btn.style.bottom = 'auto';
      btn.style.right = 'auto';
      btn.innerHTML = cameraIcon + 'Aggiungi immagine hero';
      btn.onclick = wysOpenImagePicker;
      placeholder.appendChild(btn);
      if (heroSection.nextSibling) heroSection.parentNode.insertBefore(placeholder, heroSection.nextSibling);
      else heroSection.parentNode.appendChild(placeholder);
    }
  }

  // ── Save ──
  window.saveArticle = function() {
    var statusEl = document.getElementById('wys-save-status');
    var btn = document.getElementById('wys-save-btn');
    btn.disabled = true;
    statusEl.textContent = 'Salvataggio...';

    // Collect data
    var title = '';
    var titleEl = document.querySelector('[data-editable="title"]');
    if (titleEl) title = titleEl.innerHTML;

    var subtitle = '';
    var subEl = document.querySelector('[data-editable="subtitle"]');
    if (subEl) subtitle = subEl.textContent;

    var excerpt = '';
    var exEl = document.getElementById('wys-excerpt');
    if (exEl) excerpt = exEl.textContent;

    var perspective = '';
    var perspEl = document.querySelector('[data-editable="our_perspective"]');
    if (perspEl) perspective = perspEl.innerHTML;

    // Reconstruct body_html from editable children
    var bodyParts = [];
    document.querySelectorAll('.article-content > [data-editable="body"]').forEach(function(el) {
      // Clone to clean up contenteditable artifacts
      var clone = el.cloneNode(true);
      clone.removeAttribute('contenteditable');
      clone.removeAttribute('data-editable');
      bodyParts.push(clone.outerHTML);
    });
    var bodyHtml = bodyParts.join('\n');

    var payload = {
      file: JSON_FILE,
      type: 'journal',
      data: {
        title: title,
        subtitle: subtitle,
        excerpt: excerpt,
        our_perspective: perspective,
        body_html: bodyHtml
      }
    };

    fetch('/api/save_draft', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    }).then(function(r){ return r.json(); }).then(function(resp) {
      btn.disabled = false;
      if (resp.ok) {
        statusEl.textContent = 'Salvato!';
        setTimeout(function() {
          // Reload to see re-rendered version
          location.reload();
        }, 800);
      } else {
        statusEl.textContent = 'Errore: ' + (resp.error || 'sconosciuto');
      }
    }).catch(function(err) {
      btn.disabled = false;
      statusEl.textContent = 'Errore rete: ' + err.message;
    });
  };

  // ── Image picker ──
  window.wysOpenImagePicker = function() {
    imgCandidates = [];
    imgSelectedIdx = -1;
    document.getElementById('wys-img-query').value = '';
    document.getElementById('wys-img-grid-wrap').innerHTML = '';
    document.getElementById('wys-img-status').textContent = 'Caricamento...';
    document.getElementById('wys-img-confirm').disabled = true;
    document.getElementById('wys-img-backdrop').classList.add('show');
    // Auto-search immediately
    setTimeout(wysSearchImages, 100);
  };

  window.wysCloseImagePicker = function() {
    document.getElementById('wys-img-backdrop').classList.remove('show');
  };

  window.wysSearchImages = function() {
    var q = document.getElementById('wys-img-query').value || '';
    var statusEl = document.getElementById('wys-img-status');
    var btn = document.getElementById('wys-img-search-btn');
    statusEl.textContent = 'Ricerca in corso...';
    btn.disabled = true;
    document.getElementById('wys-img-grid-wrap').innerHTML = '';

    fetch('/api/fetch_images', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({file: DRAFT_FILE, type:'journal', query: q})
    }).then(function(r){return r.json();}).then(function(resp) {
      btn.disabled = false;
      if (!resp.ok) { statusEl.textContent = 'Errore: ' + (resp.error||''); return; }
      imgCandidates = resp.candidates || [];
      imgSelectedIdx = -1;
      document.getElementById('wys-img-confirm').disabled = true;
      if (imgCandidates.length === 0) { statusEl.textContent = 'Nessun risultato.'; return; }
      statusEl.textContent = imgCandidates.length + ' risultati per "' + (resp.query_used||q) + '"';
      wysRenderGrid();
    }).catch(function(err) {
      btn.disabled = false;
      statusEl.textContent = 'Errore: ' + err.message;
    });
  };

  function wysRenderGrid() {
    var wrap = document.getElementById('wys-img-grid-wrap');
    wrap.innerHTML = '';
    var sources = [], unsplash = [];
    imgCandidates.forEach(function(c, i) {
      c.__idx = i;
      if ((c.origin||'unsplash') === 'source') sources.push(c); else unsplash.push(c);
    });
    function mkCard(c) {
      var div = document.createElement('div');
      div.className = 'wys-img-card' + (c.__idx === imgSelectedIdx ? ' selected' : '');
      div.onclick = function() { imgSelectedIdx = c.__idx; document.getElementById('wys-img-confirm').disabled = false; wysRenderGrid(); };
      var badge = document.createElement('span');
      var isSrc = (c.origin||'unsplash')==='source';
      badge.className = 'wys-img-badge ' + (isSrc ? 'badge-source' : 'badge-unsplash');
      badge.textContent = isSrc ? 'Fonte' : 'Unsplash';
      div.appendChild(badge);
      var img = document.createElement('img');
      img.src = c.thumb_url || c.full_url || '';
      img.alt = c.alt_description || '';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.onerror = function() { img.style.background = '#EDE6DC'; img.alt = 'Non caricabile'; };
      div.appendChild(img);
      var cap = document.createElement('div');
      cap.className = 'wys-img-card-cap';
      cap.textContent = isSrc ? ((c.source_publication||'Fonte')+' \u2014 '+(c.source_title||'').slice(0,50)) : ('by '+(c.author_name||'Unknown'));
      div.appendChild(cap);
      return div;
    }
    if (sources.length) {
      var sec = document.createElement('div');
      sec.className = 'wys-img-section';
      sec.innerHTML = '<div class="wys-img-section-label">Dalle fonti citate <span class="hint">(hotlink, pertinenza massima)</span></div>';
      var grid = document.createElement('div');
      grid.className = 'wys-img-grid';
      sources.forEach(function(c){ grid.appendChild(mkCard(c)); });
      sec.appendChild(grid);
      wrap.appendChild(sec);
    }
    if (unsplash.length) {
      var sec = document.createElement('div');
      sec.className = 'wys-img-section';
      sec.innerHTML = '<div class="wys-img-section-label">Da Unsplash <span class="hint">(download + credit)</span></div>';
      var grid = document.createElement('div');
      grid.className = 'wys-img-grid';
      unsplash.forEach(function(c){ grid.appendChild(mkCard(c)); });
      sec.appendChild(grid);
      wrap.appendChild(sec);
    }
  }

  window.wysConfirmImage = function() {
    if (imgSelectedIdx < 0) return;
    var c = imgCandidates[imgSelectedIdx];
    var statusEl = document.getElementById('wys-img-status');
    var btn = document.getElementById('wys-img-confirm');
    btn.disabled = true;
    statusEl.textContent = 'Applicazione immagine...';
    fetch('/api/select_image', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({file: DRAFT_FILE, type:'journal', candidate: c})
    }).then(function(r){return r.json();}).then(function(resp) {
      if (resp.ok) {
        // Update hero image in-place (no reload) so edit mode stays active
        var hero = resp.hero_image || {};
        var imgUrl = hero.web_path || c.full_url || '';
        var alt = hero.alt_description || c.alt_description || '';
        var authorName = hero.author_name || c.author_name || 'Unknown';
        var authorUrl = hero.author_url || hero.unsplash_url || c.source_page_url || '';
        var origin = (hero.origin || c.origin || 'unsplash').toLowerCase();

        var existingWrap = document.querySelector('.hero-image-wrap');
        var existingFig = document.querySelector('.hero-image');

        if (existingWrap) {
          // Update existing hero image
          var img = existingWrap.querySelector('img');
          if (img) { img.src = imgUrl; img.alt = alt; }
          var credit = existingWrap.querySelector('.hero-credit');
          if (credit) {
            if (origin === 'source') {
              credit.innerHTML = 'Photo: <a href="' + authorUrl + '" target="_blank" rel="noopener">' + authorName + '</a> \u2014 via <a href="' + authorUrl + '" target="_blank" rel="noopener">Original Article</a>';
            } else {
              credit.innerHTML = 'Photo: <a href="' + authorUrl + '" target="_blank" rel="noopener">' + authorName + '</a> / <a href="https://unsplash.com?utm_source=myvilla&utm_medium=referral" target="_blank" rel="noopener">Unsplash</a>';
            }
          }
        } else {
          // Insert new hero image block
          var creditHtml;
          if (origin === 'source') {
            creditHtml = 'Photo: <a href="' + authorUrl + '" target="_blank" rel="noopener">' + authorName + '</a> \u2014 via <a href="' + authorUrl + '" target="_blank" rel="noopener">Original Article</a>';
          } else {
            creditHtml = 'Photo: <a href="' + authorUrl + '" target="_blank" rel="noopener">' + authorName + '</a> / <a href="https://unsplash.com?utm_source=myvilla&utm_medium=referral" target="_blank" rel="noopener">Unsplash</a>';
          }
          var wrap = document.createElement('div');
          wrap.className = 'hero-image-wrap';
          wrap.innerHTML = '<figure class="hero-image">' +
            '<img src="' + imgUrl + '" alt="' + alt.replace(/"/g, '&quot;') + '" loading="eager" decoding="async" referrerpolicy="no-referrer">' +
            '<figcaption class="hero-credit">' + creditHtml + '</figcaption>' +
            '</figure>';
          // Insert after article-hero section
          var heroSection = document.querySelector('.article-hero');
          if (heroSection && heroSection.nextSibling) {
            heroSection.parentNode.insertBefore(wrap, heroSection.nextSibling);
          }
        }

        // Update change-image button text
        var changeBtn = document.getElementById('wys-change-img');
        if (changeBtn) changeBtn.textContent = 'Cambia immagine';

        // Close modal, stay in edit mode
        wysCloseImagePicker();
        statusEl.textContent = 'Immagine applicata!';
      } else {
        btn.disabled = false;
        statusEl.textContent = 'Errore: ' + (resp.error||'');
      }
    }).catch(function(err) {
      btn.disabled = false;
      statusEl.textContent = 'Errore: ' + err.message;
    });
  };

  // Close modal on backdrop click
  document.getElementById('wys-img-backdrop').addEventListener('click', function(e) {
    if (e.target === this) wysCloseImagePicker();
  });

  // Keyboard: Escape closes modal or exits edit
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      if (document.getElementById('wys-img-backdrop').classList.contains('show')) {
        wysCloseImagePicker();
      }
    }
  });

  // Auto-enter edit mode if ?edit=1
  if (new URLSearchParams(window.location.search).get('edit') === '1') {
    window.addEventListener('DOMContentLoaded', function() {
      setTimeout(enterEditMode, 300);
    });
    // Fallback if DOMContentLoaded already fired
    if (document.readyState !== 'loading') setTimeout(enterEditMode, 300);
  }
})();
</script>
"""
    return raw.replace('%%FILENAME%%', filename.replace("'", "\\'"))


# ── Draft scanning helpers ───────────────────────────────────────────

def _scan_articles_in_dir(directory):
    """Shared helper: scan a directory of journal HTML files and return
    a list of metadata dicts. Used by scan_journal_drafts (queue) and
    scan_published_articles (live blog/).
    """
    if not directory.exists():
        return []
    out = []
    for f in sorted(directory.glob("*.html")):
        # The blog/index.html is the journal landing page, not an article.
        if f.name == "index.html":
            continue
        content = f.read_text(encoding="utf-8", errors="replace")

        m = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
        title = html.unescape(m.group(1).strip()) if m else f.stem.replace("-", " ").title()
        title = re.sub(r"\s*[—-]\s*My Villa Journal\s*$", "", title)

        m = re.search(r'class="article-hero-tag"[^>]*>(.*?)<', content, re.DOTALL)
        section = html.unescape(m.group(1).strip()) if m else ""

        m = re.search(
            r'<meta\s+name="description"\s+content="(.*?)"',
            content, re.IGNORECASE,
        )
        excerpt = html.unescape(m.group(1).strip()) if m else ""

        m = re.search(
            r'<meta\s+property="article:published_time"\s+content="(.*?)"',
            content, re.IGNORECASE,
        )
        if not m:
            m = re.search(r'class="article-date"[^>]*>(.*?)<', content, re.DOTALL)
        date_str = m.group(1).strip() if m else ""

        out.append({
            "file": f.name,
            "title": title,
            "section": section,
            "excerpt": excerpt,
            "date": date_str,
            "size_kb": round(f.stat().st_size / 1024, 1),
            "mtime": f.stat().st_mtime,
        })
    return out


def scan_published_articles():
    """Scan blog/*.html — articles already live on myvilla.la.

    Returned items are sorted newest-first by file mtime so the
    dashboard surfaces the most-recent publishes at the top of the
    "Published" section.
    """
    items = _scan_articles_in_dir(BLOG_DIR)
    items.sort(key=lambda d: d.get("mtime", 0), reverse=True)
    return items


def scan_journal_drafts():
    """Scan _drafts/journal/*.html and extract metadata from each."""
    journal_dir = DRAFTS_DIR / "journal"
    if not journal_dir.exists():
        return []

    drafts = []
    for f in sorted(journal_dir.glob("*.html")):
        content = f.read_text(encoding="utf-8", errors="replace")

        # Title from <title> tag — unescape entities so the review UI
        # can re-escape once via _esc() without double-encoding
        # (e.g. California&#x27;s → California's, then _esc → California&#x27;s once)
        m = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
        title = html.unescape(m.group(1).strip()) if m else f.stem.replace("-", " ").title()
        # Strip trailing "— My Villa Journal" suffix (cosmetic, for cleaner card titles)
        title = re.sub(r"\s*[—-]\s*My Villa Journal\s*$", "", title)

        # Section from .article-hero-tag (unescape for same reason)
        m = re.search(r'class="article-hero-tag"[^>]*>(.*?)<', content, re.DOTALL)
        section = html.unescape(m.group(1).strip()) if m else ""

        # Meta description (unescape for same reason)
        m = re.search(
            r'<meta\s+name="description"\s+content="(.*?)"',
            content, re.IGNORECASE
        )
        excerpt = html.unescape(m.group(1).strip()) if m else ""

        # Date from meta or article-date
        m = re.search(
            r'<meta\s+property="article:published_time"\s+content="(.*?)"',
            content, re.IGNORECASE,
        )
        if not m:
            m = re.search(r'class="article-date"[^>]*>(.*?)<', content, re.DOTALL)
        date_str = m.group(1).strip() if m else ""

        size_kb = f.stat().st_size / 1024

        drafts.append({
            "file": f.name,
            "title": title,
            "section": section,
            "excerpt": excerpt,
            "date": date_str,
            "size_kb": round(size_kb, 1),
        })

    return drafts


def _social_source_dirs():
    """Le sorgenti REALI dei draft social, in ordine di scansione.

    Storia: il pannello leggeva solo _drafts/social/, ma dal passaggio
    all'automazione i generatori scrivono in _system/social/posts/
    reactive/ e i companion IG degli articoli vivono come blog/*.ig.md.
    Risultato: la sezione social sembrava sempre vuota. Ora si scandiscono
    tutte e tre.
    """
    return [
        DRAFTS_DIR / "social",                       # legacy
        SYSTEM_DIR / "social" / "posts" / "reactive",  # generate_social
    ]


def find_social_source(filename: str):
    """Trova il file di un draft social in QUALUNQUE sorgente.
    → Path | None. I companion .ig.md vivono in blog/."""
    name = Path(filename).name
    if name.endswith(".ig.md"):
        cand = BLOG_DIR / name
        return cand if cand.exists() else None
    for d in _social_source_dirs():
        cand = d / name
        if cand.exists():
            return cand
    return None


def _social_image_preview(frontmatter: dict) -> str:
    """URL pubblico dell'immagine del post (stessa logica di
    ig_publisher._resolve_image_url, versione preview)."""
    img = str(frontmatter.get("image") or "").strip()
    if img.startswith(("http://", "https://")):
        return img
    if img:
        local = ROOT_DIR / img.lstrip("/")
        if local.exists():
            return f"https://myvilla.la/{local.relative_to(ROOT_DIR).as_posix()}"
        return ""
    slug = str(frontmatter.get("journal_slug") or "").strip()
    if slug:
        for ext in ("jpg", "jpeg", "png", "webp"):
            cand = BLOG_DIR / "assets" / "img" / f"{slug}-hero.{ext}"
            if cand.exists():
                return (f"https://myvilla.la/"
                        f"{cand.relative_to(ROOT_DIR).as_posix()}")
    return ""


def _parse_social_file(f):
    """→ dict draft | None (None = già approved/published/rejected)."""
    raw = f.read_text(encoding="utf-8", errors="replace")
    frontmatter = {}
    body = raw
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2).strip()
        for line in fm_text.splitlines():
            kv = line.split(":", 1)
            if len(kv) == 2:
                frontmatter[kv[0].strip()] = kv[1].strip().strip('"').strip("'")

    status = (frontmatter.get("status") or "draft").lower()
    if status in ("approved", "published", "rejected"):
        return None

    # Freschezza: niente proposte più vecchie di 7 giorni — un post
    # "reattivo" su una notizia di 2 settimane fa è rumore, non valore.
    date_str = (frontmatter.get("date")
                or frontmatter.get("generated_at", ""))[:10]
    if date_str:
        try:
            age = (datetime.now() - datetime.strptime(date_str, "%Y-%m-%d")).days
            if age > 7:
                return None
        except ValueError:
            pass

    channel = frontmatter.get("channel", frontmatter.get("platform", ""))
    if f.name.endswith(".ig.md"):
        channel = channel or "ig"
    post_type = frontmatter.get("type", "")
    if f.name.endswith(".ig.md") and not post_type:
        post_type = "journal_companion"

    # Rilevanza: score del radar se presente; i companion (annuncio di
    # un NOSTRO articolo) valgono un mid-score fisso 13 — battono i
    # reattivi deboli, perdono dai reattivi forti (15+).
    try:
        score = float(frontmatter.get("radar_score") or 0)
    except (TypeError, ValueError):
        score = 0
    if not score:
        score = 13 if f.name.endswith(".ig.md") else 10

    return {
        "file": f.name,
        "channel": channel,
        "type": post_type,
        "score": score,
        "date": frontmatter.get("date",
                                frontmatter.get("generated_at", ""))[:10],
        "char_count": frontmatter.get("char_count", str(len(body))),
        "body": body[:500],
        "image_url": _social_image_preview(frontmatter),
        "journal_slug": frontmatter.get("journal_slug", ""),
        "article_url": frontmatter.get("article_url", ""),
    }


def scan_social_drafts():
    """Tutti i draft social in attesa di approvazione, da TUTTE le
    sorgenti: legacy _drafts/social, posts/reactive (generatori),
    blog/*.ig.md (companion degli articoli pubblicati)."""
    drafts = []
    seen = set()
    for d in _social_source_dirs():
        if not d.exists():
            continue
        for f in sorted(d.glob("*.md")):
            if f.name in seen:
                continue
            parsed = _parse_social_file(f)
            if parsed:
                seen.add(f.name)
                drafts.append(parsed)
    # Companion IG degli articoli live
    for f in sorted(BLOG_DIR.glob("*.ig.md")):
        if f.name in seen:
            continue
        parsed = _parse_social_file(f)
        if parsed:
            seen.add(f.name)
            drafts.append(parsed)
    # RILEVANZA prima (score del radar, "scegli tu i più rilevanti"),
    # freschezza come spareggio, IG prioritario su X.
    drafts.sort(key=lambda x: (x.get("score", 0), x["date"]), reverse=True)
    drafts.sort(key=lambda x: 0 if ("ig" in x["channel"].lower()
                                    or "instagram" in x["channel"].lower())
                else 1)
    return drafts


def scan_editorial_drafts():
    """Scan _drafts/social_editorial/*.md — the IG editorial pipeline (separate
    from reactive social posts in _drafts/social/). Uses PyYAML for richer
    frontmatter parsing (carousel slides, hashtag arrays, etc.).
    Returns drafts sorted by scheduled date ascending."""
    editorial_dir = DRAFTS_DIR / "social_editorial"
    if not editorial_dir.exists():
        return []

    try:
        import yaml as _yaml
    except ImportError:
        return []

    drafts = []
    for f in sorted(editorial_dir.glob("*.md")):
        raw = f.read_text(encoding="utf-8", errors="replace")
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
        if not fm_match:
            continue

        try:
            frontmatter = _yaml.safe_load(fm_match.group(1)) or {}
        except Exception:
            continue
        body = fm_match.group(2).strip()

        drafts.append({
            "file": f.name,
            "date": str(frontmatter.get("date", "")),
            "scheduled_time": str(frontmatter.get("scheduled_time", "09:00")),
            "timezone": str(frontmatter.get("timezone", "America/Los_Angeles")),
            "pillar": frontmatter.get("pillar", ""),
            "sub_topic": frontmatter.get("sub_topic", ""),
            "format": frontmatter.get("format", ""),
            "slug": frontmatter.get("slug", ""),
            "status": frontmatter.get("status", "draft"),
            "char_count": int(frontmatter.get("char_count") or len(body)),
            "hashtags": frontmatter.get("hashtags", []) or [],
            "image_filename": frontmatter.get("image_filename", ""),
            "image_web_path": frontmatter.get("image_web_path", ""),
            "image_alt": frontmatter.get("image_alt", ""),
            "voice_self_check": frontmatter.get("voice_self_check", ""),
            "slides": frontmatter.get("slides", []) or [],
            "partner_handle": frontmatter.get("partner_handle", ""),
            "partner_post_url": frontmatter.get("partner_post_url", ""),
            "partner_post_shortcode": frontmatter.get("partner_post_shortcode", ""),
            "partner_post_image_count": int(frontmatter.get("partner_post_image_count") or 1),
            "partner_post_thumbnails": frontmatter.get("partner_post_thumbnails", []) or [],
            "warning_forbidden_terms": frontmatter.get("warning_forbidden_terms", []) or [],
            "body": body,
        })

    drafts.sort(key=lambda d: (d["date"], d["scheduled_time"]))
    return drafts


# ── Publication reach estimates ──────────────────────────────────────
# Approximate monthly unique visitors (millions) — helps the user decide
# whether a pitch is worth the time. Numbers from Similarweb, Comscore,
# and publication media kits; refreshed periodically.
#
# Values are in monthly unique visitors (millions) unless noted.
PUBLICATION_REACH = {
    # Top-tier national (highest priority)
    "nytimes.com": 450,
    "nyt.com": 450,
    "washingtonpost.com": 200,
    "wsj.com": 80,
    "bloomberg.com": 100,
    "bloomberglaw.com": 5,
    "bloomberg law": 5,
    "cnn.com": 300,
    "forbes.com": 140,
    "fortune.com": 25,
    "reuters.com": 90,

    # Real estate / property specialty
    "robbreport.com": 8,
    "robb report": 8,
    "architecturaldigest.com": 20,
    "architectural digest": 20,
    "dwell.com": 3,
    "dezeen.com": 5,
    "archdaily.com": 6,
    "elledecor.com": 3,
    "housebeautiful.com": 3,
    "mansionglobal.com": 2.5,
    "therealdeal.com": 4,
    "the real deal": 4,
    "realtor.com": 80,
    "redfin.com": 50,
    "zillow.com": 250,
    "curbed.com": 10,
    "thedirt.com": 0.5,

    # LA / California regional
    "latimes.com": 35,
    "los angeles times": 35,
    "laist.com": 3,
    "lamag.com": 1.5,
    "calmatters.org": 1,
    "politico.com": 20,
    "sacbee.com": 3,
    "sfchronicle.com": 7,

    # Insurance / industry
    "insurancejournal.com": 1.5,
    "claimsjournal.com": 0.5,
    "propertycasualty360.com": 0.4,
    "coveragecat.com": 0.2,
    "policygenius.com": 3,
    "valuepenguin.com": 2,
    "bankrate.com": 25,
    "nerdwallet.com": 30,

    # National housing
    "nahb.org": 1,
    "prnewswire.com": 2,
    "nationaltoday.com": 2,

    # Mortgage / housing industry verticals
    "housingwire.com": 1.5,
    "housing wire": 1.5,
    "inman.com": 2,
    "inman": 2,
    "nar.realtor": 2,
    "nar.org": 2,
    "national association of realtors": 2,
    "freddiemac.com": 1,
    "fanniemae.com": 1,
    "hud.gov": 3,
    "urban.org": 0.5,
    "jchs.harvard.edu": 0.2,
    "joint center for housing studies": 0.2,
    "car.org": 0.5,
    "california association of realtors": 0.5,

    # Commercial / construction trade press
    "bisnow.com": 3,
    "connectcre.com": 1,
    "constructionowners.com": 0.1,
    "enr.com": 1,
    "engineering news-record": 1,
    "constructiondive.com": 0.8,
    "multihousingnews.com": 0.5,
    "multifamilyexecutive.com": 0.4,
    "builderonline.com": 0.7,
    "jlc-online.com": 0.3,
    "journaloflightconstruction": 0.3,
    "prosalesmagazine.com": 0.2,
    "residentialarchitect.com": 0.2,
    "concreteconstruction.net": 0.3,
    "concrete construction": 0.3,

    # Additional fire/resilience trade press
    "disastersafety.org": 0.2,
    "ibhs.org": 0.3,
    "firesafemarin.org": 0.1,
    "headwaterseconomics.org": 0.2,
    "fema.gov": 5,
    "osha.gov": 3,
    "calfire.ca.gov": 1,
    "fire.ca.gov": 1,
    "insurance.ca.gov": 0.5,
    "cdi.ca.gov": 0.5,
    "california department of insurance": 0.5,

    # Lifestyle / men's
    "gq.com": 15,
    "esquire.com": 5,
    "hollywoodreporter.com": 10,
    "variety.com": 15,

    # Tech / finance (lower priority for us)
    "tipranks.com": 10,
    "yahoo.com": 200,
    "yahoo finance": 70,
    "finance.yahoo.com": 70,
    "businessinsider.com": 80,
    "morningstar.com": 25,
    "morningstar": 25,
    "marketwatch.com": 35,
    "marketwatch": 35,
    "cnbc.com": 90,
    "cnbc": 90,
    "benzinga.com": 15,
    "benzinga": 15,
    "seekingalpha.com": 20,
    "seeking alpha": 20,
    "nasdaq.com": 30,
    "nasdaq": 30,
    "investing.com": 40,
    "streetinsider.com": 3,
    "street insider": 3,
    "thestreet.com": 10,
    "thestreet": 10,
    "barrons.com": 8,
    "barron's": 8,
    "ft.com": 25,
    "financial times": 25,
    "economist.com": 15,
    "the economist": 15,

    # Press-release wires (broad syndication)
    "businesswire.com": 3,
    "business wire": 3,
    "globenewswire.com": 2,
    "globe newswire": 2,
    "prweb.com": 1,
    "accesswire.com": 1,
    "newswire.com": 1,

    # News aggregators / low-signal (de-prioritize)
    "yahoo.com/news": 200,
    "msn.com": 200,
    "apnews.com": 30,
    "google news": 150,
    "news.google.com": 150,
    "smartnews.com": 10,
    "flipboard.com": 20,

    # Additional real estate / architecture
    "architecturalrecord.com": 1,
    "architectural record": 1,
    "metropolismag.com": 0.4,
    "surfacemag.com": 0.3,
    "wallpaper.com": 2,
    "theplan.it": 0.2,
    "domusweb.it": 0.8,
    "domus": 0.8,
    "abitare.it": 0.3,
    "designboom.com": 3,

    # LA / CA additions
    "dailynews.com": 4,
    "la daily news": 4,
    "patch.com": 15,
    "spectrumnews1.com": 2,
    "ktla.com": 8,
    "abc7.com": 10,
    "nbclosangeles.com": 6,
    "cbsnews.com": 80,
    "foxla.com": 5,

    # Insurance additions
    "iii.org": 0.3,
    "insurance information institute": 0.3,
    "reinsurancenews.com": 0.2,
    "artemis.bm": 0.2,
    "carriermanagement.com": 0.2,
}


def _log_unknown_publication(publication, url=""):
    """Append unknown publications to a log file for later review/mapping.
    Keeps a deduplicated set in memory; appends to disk on first encounter.
    Skips social platforms (reach handled via follower counts).
    """
    if not publication:
        return
    pub_lower = publication.lower().strip()
    # Skip social and URL-only items — those are handled differently
    _social_markers = ("x/twitter", "twitter", "reddit", "x.com", "twitter.com")
    if any(m in pub_lower for m in _social_markers):
        return
    log_path = SYSTEM_DIR / "radar" / "unknown_publications.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Cache seen to avoid re-logging in the same process
    seen = getattr(_log_unknown_publication, "_seen", None)
    if seen is None:
        seen = set()
        if log_path.exists():
            try:
                for line in log_path.read_text(encoding="utf-8").splitlines():
                    parts = line.split("\t")
                    if parts:
                        seen.add(parts[0].lower().strip())
            except Exception:
                pass
        _log_unknown_publication._seen = seen
    if pub_lower in seen:
        return
    seen.add(pub_lower)
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"{pub_lower}\t{url}\t{datetime.now().isoformat()}\n")
    except Exception:
        pass


def _lookup_publication_reach(publication, url="", item=None):
    """Return approximate monthly readers (in millions) for a publication,
    or None if unknown. Checks publication name + URL domain.

    When `item` is provided, also checks `item["reach_estimate"]` — an
    AI-estimated reach saved by generate_radar_report.py when the
    publication is unknown to the static dict.
    """
    if not publication and not url:
        return None

    pub_lower = (publication or "").lower().strip()
    url_lower = (url or "").lower()

    # Direct publication match
    if pub_lower in PUBLICATION_REACH:
        return PUBLICATION_REACH[pub_lower]

    # Partial match on publication
    for key, val in PUBLICATION_REACH.items():
        if key in pub_lower or pub_lower in key:
            return val

    # Try URL domain
    for key, val in PUBLICATION_REACH.items():
        if key in url_lower:
            return val

    # Fallback: AI-estimated reach saved by the radar pipeline
    if item:
        est = item.get("reach_estimate")
        if isinstance(est, (int, float)) and est > 0:
            return est

    # Still unknown — log for later review
    _log_unknown_publication(publication, url)
    return None


def _format_reach(millions):
    """Format a millions-of-visitors number for display.
    0.5 → "500K readers/mo"
    35 → "35M readers/mo"
    None → None (unknown)
    """
    if millions is None:
        return None
    if millions >= 1:
        if millions == int(millions):
            return f"{int(millions)}M readers/mo"
        return f"{millions:.1f}M readers/mo"
    return f"{int(millions * 1000)}K readers/mo"


def _format_follower_count(n):
    """Format follower/subscriber count: 12345 → '12.3K', 450000 → '450K', 1500000 → '1.5M'."""
    if not n:
        return None
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        if n < 10_000:
            return f"{n/1_000:.1f}K"
        return f"{n//1000}K"
    return str(n)


# ── Reply drafts (journalist reply follow-ups) ────────────────────────
#
# Reads the per-thread JSON files produced by `reply_drafter.py` into a
# uniform list the dashboard renders. Each entry is a "reply card" with
# the journalist's original message, the auto-drafted response, and the
# classification that tells the UI how to colour the card and which
# action buttons to show.
#
# Companion data in `_system/outreach/replies/<thread>.json` (produced
# by `reply_monitor.py`) is reused here only to surface the original
# reply's full body for the operator to read while reviewing the draft.

REPLY_DRAFTS_DIR = ROOT_DIR / "_drafts" / "email_replies"
REPLIES_STATE_DIR = ROOT_DIR / "_system" / "outreach" / "replies"
REPLIES_DISMISSED_DIR = REPLY_DRAFTS_DIR / "_dismissed"


def scan_reply_drafts():
    """Return a list of draft-reply dicts, newest-pending first.

    Each dict contains:
      thread_id, to, to_name, subject, body, classification, confidence,
      reasoning, attachments, needs_review, suggested_next_step,
      in_reply_to, outreach_to, outreach_subject, outreach_sent_at,
      replying_to_received_at, journalist_body, journalist_snippet,
      created_at, model.

    Dismissed drafts (under `_dismissed/`) are not returned.
    """
    if not REPLY_DRAFTS_DIR.exists():
        return []

    drafts = []
    for f in sorted(REPLY_DRAFTS_DIR.glob("*.json")):
        if f.parent.name.startswith("_"):
            continue  # _dismissed/ and other hidden subdirs
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        # Pull the full journalist body from the thread state file so
        # the dashboard can show it in a collapsible block.
        thread_id = d.get("thread_id", f.stem)
        state_path = REPLIES_STATE_DIR / f"{thread_id}.json"
        journalist_body = d.get("journalist_body_preview", "")
        journalist_snippet = ""
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                replies = [
                    r for r in (state.get("replies") or [])
                    if r.get("kind", "reply") == "reply"
                ]
                if replies:
                    latest = sorted(
                        replies, key=lambda r: r.get("received_at") or ""
                    )[-1]
                    journalist_body = latest.get("body") or journalist_body
                    journalist_snippet = latest.get("snippet") or ""
            except (json.JSONDecodeError, KeyError):
                pass

        drafts.append({
            "thread_id": thread_id,
            "to": d.get("to", ""),
            "to_name": d.get("to_name", ""),
            "subject": d.get("subject", ""),
            "body": d.get("body", ""),
            "classification": d.get("classification", "needs_human"),
            "confidence": float(d.get("confidence", 0.0) or 0.0),
            "reasoning": d.get("reasoning", ""),
            "attachments": list(d.get("attachments") or []),
            "needs_review": bool(d.get("needs_review", False)),
            "suggested_next_step": d.get("suggested_next_step"),
            "in_reply_to": d.get("in_reply_to"),
            "references": d.get("references"),
            "outreach_to": d.get("outreach_to") if "outreach_to" in d else None,
            "outreach_subject": d.get("outreach_subject"),
            "outreach_sent_at": d.get("outreach_sent_at"),
            "replying_to_received_at": d.get("replying_to_received_at", ""),
            "replying_to_message_id": d.get("replying_to_message_id", ""),
            "journalist_body": journalist_body,
            "journalist_snippet": journalist_snippet,
            "created_at": d.get("created_at", ""),
            "model": d.get("model", ""),
        })

    # Sort: needs_review first (human attention), then by reply recency.
    def _sort_key(d):
        needs_human_first = 0 if d["classification"] == "needs_human" else 1
        return (needs_human_first, -_ts_epoch(d["replying_to_received_at"]))

    drafts.sort(key=_sort_key)
    return drafts


def _ts_epoch(iso: str) -> float:
    """Parse an ISO-8601 timestamp (possibly with Z) to epoch seconds.
    Returns 0.0 on failure — used only for sorting."""
    if not iso:
        return 0.0
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


# ── Radar opportunities (news abstracts + journalist pitches) ────────

def scan_radar_opportunities():
    """Find the most recent radar JSON in _system/radar/reports/ and return
    its qualified items (abstracts + generated email/tweet drafts).

    Returns dict:
      {
        "date": "YYYY-MM-DD",
        "file": "radar_YYYY-MM-DD.json",
        "md_path": "radar_YYYY-MM-DD.md" (may be None),
        "news": [ ... ],         # all qualified items (abstracts)
        "emails": [ ... ],       # items where draft.type == "email"
        "tweets": [ ... ],       # items where draft.type == "tweet"
        "reddit": [ ... ],       # items where draft.type == "reddit_comment"
      }
    or None if no radar file is found.
    """
    reports_dir = SYSTEM_DIR / "radar" / "reports"
    if not reports_dir.exists():
        return None

    # Find most recent radar_YYYY-MM-DD.json (exclude _365day_, _60day_ variants)
    candidates = sorted(
        [
            f for f in reports_dir.glob("radar_*.json")
            if re.match(r"^radar_\d{4}-\d{2}-\d{2}\.json$", f.name)
        ],
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None

    radar_file = candidates[0]
    try:
        data = json.loads(radar_file.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [radar] Failed to parse {radar_file.name}: {e}")
        return None

    date_str = data.get("date", radar_file.stem.replace("radar_", ""))
    qualified = data.get("qualified", []) or []
    viral_raw = data.get("viral_opportunities", []) or []
    early_raw = data.get("early_signals", []) or []

    # ── Filter out user-dismissed URLs ────────────────────────────────
    # Items the operator explicitly dismissed via "Discard" on a radar
    # card are persisted in previously_reported.json with
    # action_type="user_dismissed". We exclude them here so a page
    # refresh confirms the dismissal immediately, without waiting for
    # the next radar cycle (radar.py also honors this file natively, so
    # the URL is filtered at both layers).
    dismissed_urls = set()
    dedup_path = SYSTEM_DIR / "radar" / "previously_reported.json"
    if dedup_path.exists():
        try:
            dedup_data = json.loads(dedup_path.read_text(encoding="utf-8"))
            for a in dedup_data.get("reported_articles", []):
                if a.get("action_type") == "user_dismissed" and a.get("url"):
                    dismissed_urls.add(a["url"])
        except Exception as e:
            print(f"  [radar] Failed to load dismissals from {dedup_path.name}: {e}")

    if dismissed_urls:
        before = len(qualified) + len(viral_raw) + len(early_raw)
        qualified = [x for x in qualified if x.get("url") not in dismissed_urls]
        viral_raw = [x for x in viral_raw  if x.get("url") not in dismissed_urls]
        early_raw = [x for x in early_raw  if x.get("url") not in dismissed_urls]
        after = len(qualified) + len(viral_raw) + len(early_raw)
        if before != after:
            print(f"  [radar] hid {before - after} user-dismissed item(s) "
                  f"from the dashboard view")

    # Markdown path (if generate_radar_report was run with --markdown)
    md_file = radar_file.with_suffix(".md")
    md_path = md_file.name if md_file.exists() else None
    dashboard_file = reports_dir / f"radar_dashboard_{date_str}.html"
    dashboard_path = dashboard_file.name if dashboard_file.exists() else None

    # Bucket by draft type
    emails = []
    tweets = []
    reddit = []
    news_items = []

    for idx, item in enumerate(qualified):
        draft = item.get("draft") or {}
        dtype = (draft.get("type") or "").lower()
        pub = item.get("publication", "") or item.get("source", "")
        url = item.get("url", "")

        # Reach lookup (publications → monthly readers, X/Reddit → followers)
        reach_millions = _lookup_publication_reach(pub, url, item=item)
        reach_label = _format_reach(reach_millions)
        reach_estimated = bool(item.get("reach_estimate")) and (
            (pub or "").lower().strip() not in PUBLICATION_REACH
            and not any(
                k in (pub or "").lower() or k in (url or "").lower()
                for k in PUBLICATION_REACH
            )
        )

        # Fallback for social sources (X, Reddit) — use follower/subscriber count
        if not reach_label:
            src = (item.get("source") or "").lower()
            eng = item.get("engagement") or {}
            if src in ("grok_x", "x", "twitter") or "x/twitter" in pub.lower():
                followers = eng.get("author_followers", 0)
                if followers:
                    reach_label = f"{_format_follower_count(followers)} followers"
            elif src == "reddit":
                subs = eng.get("subreddit_subscribers", 0)
                if subs:
                    reach_label = f"{_format_follower_count(subs)} subscribers"

        entry = {
            "idx": idx,
            "title": item.get("title", "")[:160],
            "publication": pub,
            "url": url,
            "snippet": (item.get("summary") or item.get("engagement_angle") or item.get("snippet", ""))[:400],
            # Actionable insight fields — filled by radar.py's AI scoring.
            # Kept as separate fields (in addition to `snippet`) so the
            # dashboard can render a structured Summary + Angle block on
            # non-email news items (tweets, reddit) just like it does for
            # Early Signals.
            "summary": (item.get("summary") or "").strip(),
            "engagement_angle": (item.get("engagement_angle") or "").strip(),
            "score": item.get("ai_score", item.get("preliminary_score", 0)),
            "cluster": item.get("cluster", ""),
            "date": item.get("date", item.get("published", ""))[:10],
            "author_name": item.get("author_name", ""),
            "draft_type": dtype,
            "draft_subject": draft.get("subject", ""),
            "draft_body": draft.get("body", ""),
            "contact_name": draft.get("contact_name", "") or item.get("author_name", ""),
            "contact_role": draft.get("contact_role", ""),
            "contact_email": draft.get("contact_email", ""),
            "email_source": draft.get("email_source", ""),
            "email_routing": draft.get("email_routing", ""),
            "reach_label": reach_label,
            "reach_millions": reach_millions,
            "reach_estimated": reach_estimated,
        }

        news_items.append(entry)

        if dtype == "email":
            emails.append(entry)
        elif dtype == "tweet":
            tweets.append(entry)
        elif dtype == "reddit_comment":
            reddit.append(entry)

    # Helper to build a consistent viral/signal entry with reach
    def _build_social_entry(item, idx, include_reply=True):
        eng = item.get("engagement") or {}
        reply = item.get("viral_reply") or {} if include_reply else {}
        source = item.get("source", "")
        platform = "x" if source == "grok_x" else (
            "reddit" if source == "reddit" else source)

        url = item.get("url", "")
        reply_url = ""
        if platform == "x" and url and include_reply:
            m = re.search(r'/status/(\d+)', url)
            if m and reply.get("body") and not reply.get("skip"):
                import urllib.parse as _urlparse
                tweet_id = m.group(1)
                reply_url = (
                    f"https://x.com/intent/tweet?in_reply_to={tweet_id}"
                    f"&text={_urlparse.quote(reply['body'])}"
                )

        # Reach: for X use author followers, for Reddit use subreddit subscribers
        if platform == "x":
            reach_count = eng.get("author_followers", 0)
            reach_label = _format_follower_count(reach_count)
            reach_unit = "followers" if reach_count else ""
        elif platform == "reddit":
            reach_count = eng.get("subreddit_subscribers", 0)
            reach_label = _format_follower_count(reach_count)
            reach_unit = "subscribers" if reach_count else ""
        else:
            reach_count = 0
            reach_label = None
            reach_unit = ""

        return {
            "idx": idx,
            "platform": platform,
            "author": item.get("author", ""),
            "title": item.get("title", "")[:160],
            "snippet": item.get("snippet", "")[:500],
            "url": url,
            "reply_url": reply_url,
            "date": item.get("date", "")[:10],
            "likes": eng.get("likes", 0) or eng.get("score", 0),
            "retweets": eng.get("retweets", 0),
            "replies": eng.get("replies", 0) or eng.get("num_comments", 0),
            "views": eng.get("views", 0),
            "reach_count": reach_count,
            "reach_label": reach_label,
            "reach_unit": reach_unit,
            "virality_score": eng.get("virality_score", 0),
            # Actionable insight fields — filled by radar.py's AI scoring.
            # summary = what it is; engagement_angle = why/how to engage.
            "summary": (item.get("summary") or "").strip(),
            "engagement_angle": (item.get("engagement_angle") or "").strip(),
            "ai_score": item.get("ai_score", item.get("preliminary_score", 0)),
            "reply_body": reply.get("body", ""),
            "reply_skip": reply.get("skip", False),
            "reply_skip_reason": reply.get("skip_reason", ""),
            "reply_char_count": reply.get("char_count", 0),
            "reply_tone": reply.get("tone", ""),
        }

    # Viral opportunities — high-engagement X/Reddit posts for public reply
    viral = [_build_social_entry(item, idx, include_reply=True)
             for idx, item in enumerate(viral_raw)]

    # Early signals — on-topic but low engagement. No AI-generated reply
    # (too small to be worth drafting); user can still open post and watch.
    early = [_build_social_entry(item, idx, include_reply=False)
             for idx, item in enumerate(early_raw)]

    # Reorder Radar News so ACTIONABLE items come first:
    #   1) email pitches (sorted by score desc)
    #   2) tweet drafts that have a body (sorted by score desc)
    #   3) reddit comments (sorted by score desc)
    #   4) anything else
    # Rationale: emails are the highest-leverage outreach; tweets are
    # secondary and only worth listing when a draft reply is ready.
    def _news_sort_key(entry):
        dt = entry.get("draft_type", "")
        has_body = bool((entry.get("draft_body") or "").strip())
        if dt == "email":
            bucket = 0
        elif dt == "tweet" and has_body:
            bucket = 1
        elif dt == "reddit_comment" and has_body:
            bucket = 2
        elif dt == "tweet":
            bucket = 3  # tweets without a ready draft — deprioritize
        else:
            bucket = 4
        # Within a bucket, higher score first
        return (bucket, -(entry.get("score") or 0))

    news_items.sort(key=_news_sort_key)

    # ── Filter email-type items so the dashboard never shows a pitch
    # that the operator can't actually send. Three conditions to keep
    # an email card visible:
    #   (a) contact_email is non-empty — no address means no Send. Keeping
    #       these visible just produced "?email@publication.com" placeholder
    #       cards the operator had to scroll past every day.
    #   (b) URL not already user_dismissed — covered upstream when this
    #       same function loads previously_reported.json above (we filter
    #       dismissed URLs from qualified/viral/early before this point).
    #   (c) Implicit: items always come from the LATEST radar JSON
    #       (find_latest_radar above picks `radar_YYYY-MM-DD.json` with
    #       the most recent date). Items from old radar runs are never
    #       loaded into news_items, so "ultima scansione" is the default.
    # Other draft types (tweet, reddit_comment) are NOT filtered — those
    # don't depend on having a destination address.
    filtered_news = []
    hidden_no_email = 0
    for it in news_items:
        dtype = (it.get("draft_type") or "").lower()
        if dtype == "email":
            ct_email = (it.get("contact_email") or "").strip()
            if not ct_email:
                hidden_no_email += 1
                continue
        filtered_news.append(it)
    if hidden_no_email:
        print(f"  [radar] hid {hidden_no_email} email pitch(es) with no contact_email")
    news_items = filtered_news

    return {
        "date": date_str,
        "file": radar_file.name,
        "md_path": md_path,
        "dashboard_path": dashboard_path,
        "news": news_items,
        "emails": emails,
        "tweets": tweets,
        "reddit": reddit,
        "viral": viral,
        "early": early,
        "total_qualified": len(qualified),
        "total_watchlist": len(data.get("watchlist", []) or []),
        "total_viral": len(viral),
        "total_early": len(early),
        "api_health": data.get("api_health", {}) or {},
    }


# ── Dashboard HTML generation ────────────────────────────────────────

def build_dashboard():
    """Generate the full HTML review dashboard."""
    journal = scan_journal_drafts()
    published = scan_published_articles()
    social = scan_social_drafts()
    editorial = scan_editorial_drafts()
    radar = scan_radar_opportunities()
    # API health banner — rendered above the header so failures of any
    # radar-source API (CSE, Gemini, xAI…) are immediately visible without
    # opening the full radar dashboard. Explicit guard for the "no radar
    # yet" case so the empty-string fallback is intentional, not a
    # side-effect of the helper's None handling.
    if not radar or not radar.get("api_health"):
        api_health_banner = ""
    else:
        api_health_banner = render_api_health_banner_html(
            radar["api_health"],
            radar_date=radar.get("date"),
        )
    # Sprint 2 — semi-automatic follow-up on outreach replies. Reads
    # draft-reply JSONs produced by reply_drafter.py. Needs-human items
    # surface first, then the rest sorted by reply recency.
    reply_drafts = scan_reply_drafts()
    today = datetime.now().strftime("%A, %B %-d, %Y")
    total = len(journal) + len(social)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build journal cards
    journal_cards = ""
    journal_dir = DRAFTS_DIR / "journal"
    for d in journal:
        section_badge = ""
        if d["section"]:
            section_badge = (
                f'<span class="badge badge-section">{_esc(d["section"])}</span>'
            )

        # ── IG companion block ──────────────────────────────────────
        # Each journal draft can have a sibling .ig.md file with the
        # Instagram announcement caption. If it exists, render an
        # editable textarea inline so the operator approves both with
        # a single "Pubblica" click. If it doesn't exist, show a small
        # button to generate it on demand.
        journal_stem = Path(d["file"]).stem
        companion_path = journal_dir / f"{journal_stem}.ig.md"
        if companion_path.exists():
            try:
                raw = companion_path.read_text(encoding="utf-8")
                # Strip optional YAML frontmatter for the textarea preview;
                # we keep the file on disk untouched and re-merge frontmatter
                # at save-time if the operator edits.
                m = re.match(r"^---\s*\n.*?\n---\s*\n+(.*)", raw, re.DOTALL)
                ig_body_preview = (m.group(1) if m else raw).strip()
            except Exception:
                ig_body_preview = ""
            ig_companion_block = (
                f'<div class="ig-companion-block">'
                f'<div class="ig-companion-head">'
                f'<span class="ig-companion-label">📷 Instagram companion (sotto l\'articolo, approvato qui)</span>'
                f'<button class="btn btn-mini" onclick="regenerateIGCompanion(\'{_esc_js(d["file"])}\', this)" '
                f'title="Re-genera con Anthropic (sovrascrive il testo qui sotto)">↻ Rigenera</button>'
                f'</div>'
                f'<textarea class="ig-companion-editor" '
                f'data-companion-for="{_esc(d["file"])}" '
                f'oninput="updateIGCompanionCount(this)">{_esc(ig_body_preview)}</textarea>'
                f'<div class="ig-companion-foot">'
                f'<span class="ig-companion-charcount">{len(ig_body_preview)} chars</span>'
                f'<span class="ig-companion-hint">Pubblica: il caption viene salvato e l\'IG va in <code>_system/social/posts/approved/</code></span>'
                f'</div>'
                f'</div>'
            )
        else:
            ig_companion_block = (
                f'<div class="ig-companion-block ig-companion-empty">'
                f'<button class="btn btn-mini btn-ig-generate" '
                f'onclick="generateIGCompanion(\'{_esc_js(d["file"])}\', this)">'
                f'📷 Genera Instagram companion'
                f'</button>'
                f'<span class="ig-companion-hint">Anteprima del post che annuncia l\'articolo. Verrà approvato insieme.</span>'
                f'</div>'
            )

        journal_cards += f"""
        <div class="card" id="card-journal-{_esc(d['file'])}" data-file="{_esc(d['file'])}" data-type="journal">
          <div class="card-status-stripe"></div>
          <div class="card-body">
            <div class="card-header">
              <h3 class="card-title" data-field="title">{_esc(d['title'])}</h3>
              {section_badge}
            </div>
            <p class="card-excerpt" data-field="excerpt">{_esc(d['excerpt']) or '<em>No description</em>'}</p>
            <div class="card-meta">
              <span class="meta-date">{_esc(d['date'])}</span>
              <span class="meta-size">{d['size_kb']} KB</span>
            </div>
            {ig_companion_block}
            <div class="card-actions">
              <a class="btn btn-preview" href="/preview?file={_esc(d['file'])}" target="_blank">Anteprima</a>
              <a class="btn btn-edit" href="/preview?file={_esc(d['file'])}&edit=1" target="_blank">Modifica</a>
              <button class="btn btn-image" onclick="openImagePicker('{_esc_js(d['file'])}', this)">Scegli immagine</button>
              <button class="btn btn-opus" onclick="openReviseModal('{_esc_js(d['file'])}', 'journal', this)">Revise</button>
              <button class="btn btn-approve" onclick="doAction('approve', '{_esc_js(d['file'])}', 'journal', this)">📝 Pubblica journal</button>
              <button class="btn btn-approve-ig btn-disabled" disabled
                      title="Da attivare quando l'account Instagram sarà configurato. Per ora il caption resta nel draft.">📷 Pubblica IG</button>
              <button class="btn btn-reject" onclick="doAction('reject', '{_esc_js(d['file'])}', 'journal', this)">Cancella</button>
            </div>
          </div>
        </div>"""

    # Build published-articles rows (compact, list-style — 58+ live
    # articles would be too many full cards). One row per article with
    # title, date, section badge, and 3 small actions: View live,
    # Modifica, Unpublish.
    published_rows = ""
    for p in published:
        section_badge = (
            f'<span class="badge badge-section published-row-section">{_esc(p["section"])}</span>'
            if p["section"] else ""
        )
        public_url = f"https://myvilla.la/blog/{p['file']}"
        date_short = (p.get("date") or "")[:10]  # YYYY-MM-DD if ISO
        published_rows += f"""
        <div class="published-row" id="published-{_esc(p['file'])}" data-file="{_esc(p['file'])}">
          <div class="published-row-main">
            <div class="published-row-title">{_esc(p['title'])}</div>
            <div class="published-row-meta">
              {section_badge}
              <span class="published-row-date">{_esc(date_short)}</span>
              <span class="published-row-size">{p['size_kb']} KB</span>
            </div>
          </div>
          <div class="published-row-actions">
            <a class="btn btn-mini" href="{_esc(public_url)}" target="_blank" rel="noreferrer" title="Apri pagina live">↗ Live</a>
            <a class="btn btn-mini" href="/preview?file={_esc(p['file'])}&edit=1" target="_blank" title="Modifica inline">✏ Modifica</a>
            <button class="btn btn-mini btn-mini-danger" onclick="unpublishArticle('{_esc_js(p['file'])}', this)" title="Rimuove dal sito (sposta in archivio)">🗑 Rimuovi</button>
          </div>
        </div>"""

    # Build social cards
    social_cards_ig = []   # card IG (in ordine di scan: fresche prima)
    social_cards_x = []    # card X
    social_cards = ""      # (legacy var — composta più sotto con i cap)
    for d in social:
        ch = d["channel"].lower()
        if "x" in ch or "twitter" in ch:
            channel_label = "X"
            channel_class = "badge-x"
        elif "instagram" in ch or "ig" in ch:
            channel_label = "Instagram"
            channel_class = "badge-ig"
        else:
            channel_label = d["channel"] or "Social"
            channel_class = "badge-section"

        body_raw = d["body"] or ""

        # Determine publish platform from channel
        if "x" in ch or "twitter" in ch:
            pub_platform = "x"
            pub_icon = "&#120143;"  # 𝕏
            pub_label = "Post on X"
            copy_label = "Copy &amp; open X"
        elif "instagram" in ch or "ig" in ch:
            pub_platform = "instagram"
            pub_icon = "&#128247;"  # 📷
            pub_label = "Post on Instagram"
            copy_label = "Copy &amp; open IG"
        else:
            pub_platform = ""
            pub_icon = ""
            pub_label = "Publish"
            copy_label = "Copy"

        img_url = d.get("image_url") or ""
        img_block = (
            f'<div class="social-img-preview"><img src="{_esc(img_url)}" '
            f'alt="" loading="lazy"></div>' if img_url else
            ('<div class="social-img-missing">⚠ Nessuna immagine — il '
             'publisher IG la richiede (aggiungi <code>image:</code> nel '
             'frontmatter o verrà saltato)</div>'
             if pub_platform == "instagram" else "")
        )
        art_link = (f'<a href="{_esc(d.get("article_url",""))}" '
                    f'target="_blank" style="font-size:0.78rem;">↗ articolo</a>'
                    if d.get("article_url") else "")
        _card_html = f"""
        <div class="card" id="card-social-{_esc(d['file'])}" data-file="{_esc(d['file'])}" data-type="social" data-channel="{_esc(ch)}">
          <div class="card-status-stripe"></div>
          <div class="card-body">
            <div class="card-header">
              <span class="badge {channel_class}">{_esc(channel_label)}</span>
              {'<span class="badge badge-type">' + _esc(d["type"]) + '</span>' if d["type"] else ''}
              {art_link}
            </div>
            {img_block}
            <textarea class="card-post-editor" data-field="body" oninput="updateSocialCharCount(this)">{_esc(body_raw)}</textarea>
            <div class="card-meta">
              <span class="meta-date">{_esc(d['date'])}</span>
              <span class="meta-chars"><span class="char-count-live">{len(body_raw)}</span> chars</span>
            </div>
            <div class="card-actions">
              <button class="btn btn-approve" onclick="doAction('approve', '{_esc_js(d['file'])}', 'social', this)" title="Mette in coda: esce in automatico col cap giornaliero">✅ Approva → coda (auto)</button>
              <button class="btn btn-publish-social" data-platform="{pub_platform}" onclick="publishSocial('{_esc_js(d['file'])}', '{pub_platform}', this)" title="Pubblica adesso via API, bypassando la coda">{pub_icon} Pubblica subito</button>
              <button class="btn btn-edit" onclick="saveSocialDraft('{_esc_js(d['file'])}', this)">💾 Salva</button>
              <button class="btn btn-opus" onclick="openReviseModal('{_esc_js(d['file'])}', 'social', this)">Revise</button>
              <button class="btn btn-copy-open" onclick="copyAndOpen('{pub_platform}', this)">{copy_label}</button>
              <button class="btn btn-reject" onclick="doAction('reject', '{_esc_js(d['file'])}', 'social', this)">🗑 Scarta</button>
            </div>
          </div>
        </div>"""
        if pub_platform == "instagram":
            social_cards_ig.append(_card_html)
        else:
            social_cards_x.append(_card_html)

    # ── Composizione daily-first: il social manager vede i TOP, il
    #    resto sta in un <details> ripiegato. Budget visivo: 3 IG + 2 X.
    def _capped(cards, cap, label):
        """Cap RIGIDO: si mostrano solo le `cap` proposte più fresche.
        Le altre restano su disco, invisibili, finché la rotazione
        della pipeline non le archivia (niente liste infinite da
        smaltire: il social manager vede solo la scelta del giorno)."""
        if not cards:
            return ('<p style="color:#999;font-size:0.85rem;">'
                    f'Nessuna proposta {label} oggi.</p>')
        return "".join(cards[:cap])

    def _sub_heading(emoji, title, shown, total):
        extra = (f'<span style="font-weight:400;color:#b5a88f;'
                 f'font-size:0.78rem;margin-left:8px;">'
                 f'(+{total - shown} in magazzino, ruotano da sole)</span>'
                 if total > shown else '')
        return (f'<h3 class="channel-sub">{emoji} {title}{extra}</h3>')

    social_cards = (
        _sub_heading("📸", "Instagram — scegline 1, scarta le altre",
                     min(3, len(social_cards_ig)), len(social_cards_ig))
        + _capped(social_cards_ig, 3, "Instagram")
        + _sub_heading("𝕏", "X — scegline 1",
                       min(2, len(social_cards_x)), len(social_cards_x))
        + _capped(social_cards_x, 2, "X")
    )

    # ── Editorial IG cards (brand-foundation pipeline) ────────────────
    editorial_cards = ""
    PILLAR_ICONS = {
        "vision": "🪟",
        "archetype": "🏛",
        "system": "🧱",
        "partner_echo": "🔗",
    }
    PILLAR_LABELS = {
        "vision": "Vision",
        "archetype": "Archetype",
        "system": "System",
        "partner_echo": "Partner Echo",
    }

    # Group by ISO week so the dashboard reads as a calendar
    from collections import OrderedDict
    by_week = OrderedDict()
    for d in editorial:
        try:
            iso = datetime.strptime(d["date"], "%Y-%m-%d").isocalendar()
            week_key = f"{iso[0]}-W{iso[1]:02d}"
        except Exception:
            week_key = "unscheduled"
        by_week.setdefault(week_key, []).append(d)

    for week_key, week_drafts in by_week.items():
        if week_key == "unscheduled":
            week_label = "Unscheduled"
        else:
            year, w = week_key.split("-W")
            try:
                # Monday of that ISO week
                wk_mon = datetime.strptime(f"{year}-W{int(w):02d}-1", "%G-W%V-%u")
                wk_sun = wk_mon + timedelta(days=6)
                week_label = (
                    f"Week {int(w)} · "
                    f"{wk_mon.strftime('%b %-d')}–{wk_sun.strftime('%b %-d')}"
                )
            except Exception:
                week_label = week_key

        editorial_cards += f"""
        <div class="editorial-week-header">
          <span class="editorial-week-label">{_esc(week_label)}</span>
          <span class="editorial-week-count">{len(week_drafts)} post{'s' if len(week_drafts)!=1 else ''}</span>
        </div>"""

        for d in week_drafts:
            pillar = d["pillar"] or "vision"
            icon = PILLAR_ICONS.get(pillar, "📝")
            pillar_lbl = PILLAR_LABELS.get(pillar, pillar.title())

            # Image preview — prefer the full image_web_path stored by the
            # generator (covers partner-cache thumbnails living under
            # /_system/social/partner_cache/<handle>/) and fall back to /img/
            # only when web_path is missing (older drafts).
            img_block = ""
            if d["image_filename"]:
                src = d.get("image_web_path") or f"/img/{d['image_filename']}"
                img_block = (
                    f'<div class="editorial-img-wrap">'
                    f'<img src="{_esc(src)}" '
                    f'alt="{_esc(d["image_alt"])}" class="editorial-img" />'
                    f'<span class="editorial-img-name">{_esc(d["image_filename"])}</span>'
                    f'</div>'
                )

            # Hashtag chips
            hashtag_chips = ""
            if d["hashtags"]:
                hashtag_chips = (
                    '<div class="editorial-hashtags">' +
                    ''.join(
                        f'<span class="editorial-htag">#{_esc(h.lstrip("#"))}</span>'
                        for h in d["hashtags"]
                    ) + '</div>'
                )

            # Carousel slides preview
            slides_block = ""
            if d["slides"]:
                slide_html = ''.join(
                    f'<div class="editorial-slide">'
                    f'<span class="editorial-slide-num">{i+1}</span>'
                    f'<div class="editorial-slide-content">'
                    f'<strong>{_esc(s.get("headline",""))}</strong>'
                    f'<p>{_esc(s.get("body",""))}</p>'
                    f'</div></div>'
                    for i, s in enumerate(d["slides"])
                )
                slides_block = (
                    f'<details class="editorial-slides">'
                    f'<summary>{len(d["slides"])} carousel slides</summary>'
                    f'{slide_html}'
                    f'</details>'
                )

            # Voice self-check
            voice_block = ""
            if d["voice_self_check"]:
                voice_block = (
                    f'<div class="editorial-voice-check">'
                    f'<span class="editorial-voice-label">Voice self-check:</span> '
                    f'{_esc(d["voice_self_check"])}'
                    f'</div>'
                )

            # Forbidden term warning
            warn_block = ""
            if d["warning_forbidden_terms"]:
                warn_block = (
                    f'<div class="editorial-warn">⚠ Caption contains forbidden terms: '
                    f'{_esc(", ".join(d["warning_forbidden_terms"]))}</div>'
                )

            # Status badge color
            status = d["status"]
            status_class = {
                "draft": "status-draft",
                "approved": "status-approved",
                "scheduled": "status-scheduled",
                "published": "status-published",
            }.get(status, "status-draft")

            partner_badge = ""
            partner_link_html = ""
            if d["partner_handle"]:
                partner_badge = (
                    f'<span class="badge badge-partner">@{_esc(d["partner_handle"])}</span>'
                )
                if d.get("partner_post_url"):
                    img_count = int(d.get("partner_post_image_count") or 1)
                    count_label = f" · {img_count} images" if img_count > 1 else ""
                    partner_link_html = (
                        f'<a class="editorial-partner-link" '
                        f'href="{_esc(d["partner_post_url"])}" target="_blank" '
                        f'rel="noreferrer">↗ View original on Instagram{count_label}</a>'
                    )

            editorial_cards += f"""
        <div class="card editorial-card" id="card-editorial-{_esc(d['file'])}"
             data-file="{_esc(d['file'])}" data-type="editorial"
             data-pillar="{_esc(pillar)}">
          <div class="card-status-stripe"></div>
          <div class="card-body">
            <div class="card-header editorial-header">
              <span class="badge badge-ig">📷 IG Editorial</span>
              <span class="badge badge-pillar">{icon} {_esc(pillar_lbl)}</span>
              {partner_badge}
              <span class="badge {status_class}">{_esc(status)}</span>
              <span class="editorial-when">{_esc(d['date'])} · {_esc(d['scheduled_time'])} {_esc(d['timezone'].split('/')[-1])}</span>
            </div>
            <div class="editorial-subtopic">{_esc(d['sub_topic'] or d['slug'])}</div>
            {partner_link_html}
            {warn_block}
            {img_block}
            <textarea class="card-post-editor editorial-caption" data-field="body"
                      oninput="updateEditorialCharCount(this)">{_esc(d['body'])}</textarea>
            <div class="card-meta">
              <span class="meta-chars"><span class="char-count-live">{d['char_count']}</span> caption chars</span>
              <span class="meta-tags">{len(d['hashtags'])} hashtags</span>
              <span class="meta-format">{_esc(d['format'])}</span>
            </div>
            {hashtag_chips}
            {slides_block}
            {voice_block}
            <div class="card-actions">
              <button class="btn btn-edit" onclick="saveEditorialDraft('{_esc_js(d['file'])}', this)">Save changes</button>
              <button class="btn btn-image" onclick="openEditorialImagePicker('{_esc_js(d['file'])}', this)">🖼 Choose image</button>
              <button class="btn btn-opus" onclick="regenerateEditorial('{_esc_js(d['file'])}', this)">Regenerate</button>
              <button class="btn btn-publish-social" onclick="buildEditorialPackage('{_esc_js(d['file'])}', this)">📦 Build package</button>
              <button class="btn btn-copy-open" onclick="markEditorialPublished('{_esc_js(d['file'])}', this)">✓ Mark published</button>
              <button class="btn btn-reject" onclick="discardEditorial('{_esc_js(d['file'])}', this)">Discard</button>
            </div>
          </div>
        </div>"""

    # ── Radar News + Journalist Pitches cards ────────────────────────
    import urllib.parse as _urlparse

    news_cards = ""
    viral_cards = ""
    early_cards = ""

    if radar and radar.get("viral"):
        def _fmt_num(n):
            """Format 1234 → 1.2K, 15000 → 15K, 1200000 → 1.2M."""
            if n >= 1_000_000:
                return f"{n/1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n/1_000:.1f}K" if n < 10_000 else f"{n//1000}K"
            return str(n)

        _viral_list = []
        for v in radar["viral"]:
            platform_icon = "𝕏" if v["platform"] == "x" else "🔥"
            platform_label = "X" if v["platform"] == "x" else "Reddit"
            platform_class = "badge-tweet" if v["platform"] == "x" else "badge-reddit"

            # Metrics bar
            metrics_parts = []
            if v["likes"]:
                label = "upvotes" if v["platform"] == "reddit" else "likes"
                metrics_parts.append(f'<span class="viral-metric"><span class="viral-metric-icon">♥</span>{_fmt_num(v["likes"])} <em>{label}</em></span>')
            if v["retweets"]:
                metrics_parts.append(f'<span class="viral-metric"><span class="viral-metric-icon">🔁</span>{_fmt_num(v["retweets"])} <em>retweets</em></span>')
            if v["replies"]:
                label = "comments" if v["platform"] == "reddit" else "replies"
                metrics_parts.append(f'<span class="viral-metric"><span class="viral-metric-icon">💬</span>{_fmt_num(v["replies"])} <em>{label}</em></span>')
            if v["views"]:
                metrics_parts.append(f'<span class="viral-metric"><span class="viral-metric-icon">👁</span>{_fmt_num(v["views"])} <em>views</em></span>')
            if v.get("reach_count"):
                unit = v.get("reach_unit") or "followers"
                metrics_parts.append(f'<span class="viral-metric viral-followers">👥 {_fmt_num(v["reach_count"])} <em>{unit}</em></span>')
            metrics_html = '<div class="viral-metrics">' + ''.join(metrics_parts) + '</div>' if metrics_parts else ''

            # Virality score badge (colour by bucket)
            vs = v["virality_score"]
            if vs >= 70:
                vs_class = "virality-hot"
                vs_icon = "🔥"
            elif vs >= 50:
                vs_class = "virality-rising"
                vs_icon = "📈"
            else:
                vs_class = "virality-warm"
                vs_icon = "·"

            # Reply section
            if v["reply_skip"]:
                reply_section = (
                    f'<div class="viral-skip">'
                    f'<strong>⊘ Skipped by AI</strong> — {_esc(v["reply_skip_reason"] or "not suitable for reply")}'
                    f'</div>'
                )
                actions_section = (
                    f'<div class="card-actions">'
                    f'  <a class="btn btn-copy-open" href="{_esc(v["url"])}" target="_blank" rel="noreferrer">Open post</a>'
                    f'</div>'
                )
            else:
                tone_label = f'<span class="reply-tone">{_esc(v["reply_tone"])}</span>' if v["reply_tone"] else ''
                char_warn = " over-limit" if v["reply_char_count"] > 280 else ""
                reply_section = (
                    f'<div class="viral-reply-wrap">'
                    f'  <div class="viral-reply-label">'
                    f'    <span class="pitch-label">Suggested reply</span>'
                    f'    {tone_label}'
                    f'    <span class="reply-char-count{char_warn}">{v["reply_char_count"]} chars</span>'
                    f'  </div>'
                    f'  <textarea class="viral-reply-editor" data-platform="{v["platform"]}" oninput="updateViralCharCount(this)">{_esc(v["reply_body"])}</textarea>'
                    f'</div>'
                )
                # Build action buttons
                url_js = _esc_js(v["url"])
                if v["platform"] == "x" and v["reply_url"]:
                    actions_section = (
                        f'<div class="card-actions">'
                        f'  <button class="btn btn-viral-reply" onclick="openViralReplyX(this)">Reply on X</button>'
                        f'  <button class="btn btn-copy-pitch" onclick="copyViralReply(this)">Copy reply</button>'
                        f'  <a class="btn btn-copy-open" href="{_esc(v["url"])}" target="_blank" rel="noreferrer">Open tweet</a>'
                        f'  <button class="btn btn-reject" onclick="skipViral(this)">Skip</button>'
                        f'</div>'
                    )
                elif v["platform"] == "reddit":
                    # Reddit: pubblicazione DIRETTA via API ufficiale
                    actions_section = (
                        f'<div class="card-actions">'
                        f'  <button class="btn btn-publish-social" data-url="{_esc(v["url"])}" onclick="publishRedditComment(this)" title="Pubblica il commento su Reddit via API (cap 5/giorno)">🚀 Commenta su Reddit</button>'
                        f'  <a class="btn btn-copy-open" href="{_esc(v["url"])}" target="_blank" rel="noreferrer">Apri thread</a>'
                        f'  <button class="btn btn-copy-pitch" onclick="copyViralReply(this)">Copy</button>'
                        f'  <button class="btn btn-reject" onclick="skipViral(this)">Skip</button>'
                        f'</div>'
                    )
                else:
                    # Instagram/altro: flusso assistito (no API commenti)
                    actions_section = (
                        f'<div class="card-actions">'
                        f'  <a class="btn btn-viral-reply" href="{_esc(v["url"])}" target="_blank" rel="noreferrer">Open to reply</a>'
                        f'  <button class="btn btn-copy-pitch" onclick="copyViralReply(this)">Copy reply</button>'
                        f'  <button class="btn btn-reject" onclick="skipViral(this)">Skip</button>'
                        f'</div>'
                    )

            _viral_list.append(f"""
        <div class="card viral-card" data-virality="{vs}" data-platform="{v['platform']}">
          <div class="card-status-stripe"></div>
          <div class="card-body">
            <div class="card-header">
              <span class="badge {platform_class}">{platform_icon} {platform_label}</span>
              <span class="badge badge-virality {vs_class}">{vs_icon} Virality {vs}</span>
              <span class="news-pub">{_esc(v['author'] or v['title'][:40])}</span>
              {f'<span class="news-date">{_esc(v["date"])}</span>' if v['date'] else ''}
            </div>
            {metrics_html}
            <p class="viral-content">{_esc(v['snippet'])}</p>
            {reply_section}
            {actions_section}
          </div>
        </div>""")

        # Daily-first: 5 commenti visibili (i più virali — la lista è
        # già ordinata per virality), il resto ripiegato.
        viral_cards = "".join(_viral_list[:5])
        if len(_viral_list) > 5:
            viral_cards += (f'<details class="more-cards"><summary>'
                            f'▸ altri {len(_viral_list) - 5} post virali'
                            f'</summary>{"".join(_viral_list[5:])}</details>')

    # ── Early signals cards — on-topic, low engagement, no auto-reply ──
    if radar and radar.get("early"):
        def _fmt_num(n):
            if n >= 1_000_000:
                return f"{n/1_000_000:.1f}M"
            if n >= 1_000:
                return f"{n/1_000:.1f}K" if n < 10_000 else f"{n//1000}K"
            return str(n)

        for e in radar["early"]:
            platform_icon = "𝕏" if e["platform"] == "x" else "🔥"
            platform_label = "X" if e["platform"] == "x" else "Reddit"
            platform_class = "badge-tweet" if e["platform"] == "x" else "badge-reddit"

            # Inline metrics (compact)
            metrics_bits = []
            if e["likes"]:
                lbl = "upvotes" if e["platform"] == "reddit" else "likes"
                metrics_bits.append(f'♥{_fmt_num(e["likes"])} {lbl}')
            if e["replies"]:
                lbl = "comments" if e["platform"] == "reddit" else "replies"
                metrics_bits.append(f'💬{_fmt_num(e["replies"])} {lbl}')
            if e.get("reach_count"):
                unit = e.get("reach_unit") or "followers"
                metrics_bits.append(f'👥{_fmt_num(e["reach_count"])} {unit}')
            metrics_inline = ' · '.join(metrics_bits) if metrics_bits else 'no engagement yet'

            # Actionable insight block — summary (what it is) + engagement
            # angle (why it matters / how to engage). Falls back gracefully
            # when the radar hasn't filled the fields.
            _summary = (e.get("summary") or "").strip()
            _angle = (e.get("engagement_angle") or "").strip()
            insight_block = ""
            if _summary or _angle:
                insight_parts = []
                if _summary:
                    insight_parts.append(
                        f'<div class="early-insight-row">'
                        f'<span class="early-insight-label">Summary</span>'
                        f'<span class="early-insight-text">{_esc(_summary)}</span>'
                        f'</div>'
                    )
                if _angle:
                    insight_parts.append(
                        f'<div class="early-insight-row">'
                        f'<span class="early-insight-label">Angle</span>'
                        f'<span class="early-insight-text">{_esc(_angle)}</span>'
                        f'</div>'
                    )
                insight_block = (
                    f'<div class="early-insight">{"".join(insight_parts)}</div>'
                )

            early_cards += f"""
        <div class="card early-card" data-platform="{e['platform']}">
          <div class="card-status-stripe"></div>
          <div class="card-body">
            <div class="card-header">
              <span class="badge {platform_class}">{platform_icon} {platform_label}</span>
              <span class="early-meta">{_esc(metrics_inline)}</span>
              <span class="news-pub">{_esc(e['author'] or '')}</span>
              {f'<span class="news-date">{_esc(e["date"])}</span>' if e['date'] else ''}
            </div>
            <p class="early-content">{_esc(e['snippet'][:280])}</p>
            {insight_block}
            <div class="card-actions early-actions">
              <a class="btn btn-copy-open" href="{_esc(e['url'])}" target="_blank" rel="noreferrer">Open post →</a>
              <span class="early-hint">Monitor for growth — reply if thread picks up</span>
            </div>
          </div>
        </div>"""

    if radar and radar.get("news"):
        # Unified Radar News cards — each email-type news has the pitch inline
        for item in radar["news"]:
            score = item["score"]
            pub = item["publication"] or "—"
            title = item["title"] or "(no title)"
            snippet = item["snippet"] or ""
            url = item["url"]
            dtype = (item.get("draft_type") or "").lower()
            date_str = item.get("date", "")

            badge_class = (
                "badge-email" if dtype == "email"
                else "badge-tweet" if dtype == "tweet"
                else "badge-reddit" if dtype == "reddit_comment"
                else "badge-section"
            )
            badge_label = (
                "EMAIL PITCH" if dtype == "email"
                else "TWEET" if dtype == "tweet"
                else "REDDIT" if dtype == "reddit_comment"
                else (dtype.upper() or "NEWS")
            )

            # Build inline pitch block for email-type news
            pitch_block = ""
            if dtype == "email":
                subject = (item.get("draft_subject") or "").strip()
                body = (item.get("draft_body") or "").strip()
                contact_name = item.get("contact_name", "") or "Newsroom"
                contact_role = item.get("contact_role", "")
                contact_email = (item.get("contact_email") or "").strip()

                if contact_email and subject and body:
                    mailto = (f"mailto:{contact_email}"
                              f"?subject={_urlparse.quote(subject)}"
                              f"&body={_urlparse.quote(body)}")
                elif contact_email:
                    mailto = f"mailto:{contact_email}"
                else:
                    mailto = ""

                email_source = item.get("email_source", "")
                email_routing = item.get("email_routing", "")
                _source_labels = {
                    "apollo": ("badge-email-verified", "✓ Verified (Apollo)"),
                    "apollo_likely": ("badge-email-guess", "Apollo (likely)"),
                    # editorial_scraped = pulled live from the article
                    # page / /contact / /tips — highest confidence of the
                    # editorial sources because it's current and specific
                    # to the publication that ran the piece we're pitching.
                    "editorial_scraped": ("badge-email-scraped", "🟢 Editorial (from article)"),
                    "editorial_fallback": ("badge-email-editorial", "Editorial fallback"),
                    # Legacy labels — shouldn't appear in drafts generated
                    # after the 2026-04 pipeline refactor, but old reports
                    # regenerated without the new scraper still carry them.
                    "opus": ("badge-email-risky", "⚠️ LLM-inferred (risky, legacy)"),
                    "pattern_guess": ("badge-email-risky", "⚠️ Pattern guess (risky, legacy)"),
                }
                _src_cls, _src_lbl = _source_labels.get(email_source, ("", ""))
                source_chip = (
                    f'<span class="badge {_src_cls}" title="{_esc(_src_lbl)}">{_esc(_src_lbl)}</span>'
                    if _src_cls else ''
                )

                # Routing chip: "→ author" means the email reaches the
                # journalist directly (Apollo hit). "→ editorial" means we
                # didn't find the author and are sending to a newsroom
                # address — the draft intro has already been rewritten
                # upstream to explain this to the recipient.
                _routing_labels = {
                    "author": ("badge-route-author", "→ Author"),
                    "editorial": ("badge-route-editorial", "→ Newsroom"),
                }
                _route_cls, _route_lbl = _routing_labels.get(email_routing, ("", ""))
                routing_chip = (
                    f'<span class="badge {_route_cls}" title="Email routing: {_esc(_route_lbl)}">{_esc(_route_lbl)}</span>'
                    if _route_cls else ''
                )

                # Check the bounce blacklist for this exact address. Any
                # previously-bounced address must block the Send button
                # regardless of source — this is the most trustworthy
                # signal we have (we literally saw it fail before).
                _blacklisted = False
                _blacklist_reason = ""
                if contact_email:
                    try:
                        from reply_monitor import (
                            is_invalid_address as _is_invalid,
                            _load_invalid_addresses as _load_inv,
                        )
                        if _is_invalid(contact_email):
                            _blacklisted = True
                            info = (_load_inv().get("addresses") or {}).get(
                                contact_email.strip().lower(), {}
                            )
                            _blacklist_reason = (info.get("reason") or "")[:120]
                    except ImportError:
                        pass

                contact_badge = (
                    f'<span class="badge badge-email-ok">{_esc(contact_email)}</span>'
                    if contact_email
                    else '<span class="badge badge-email-missing">No email found</span>'
                )
                if _blacklisted:
                    contact_badge = (
                        f'<span class="badge badge-email-bounced" '
                        f'title="Previously bounced: {_esc(_blacklist_reason)}">'
                        f'❌ {_esc(contact_email)} (bounced)</span>'
                    )

                # Risk classification drives the Send button state.
                #   - "safe"    : Apollo verified OR scraped/fallback newsroom
                #                 → Send enabled, normal green
                #   - "confirm" : Apollo likely — need a one-click confirm
                #                 → Send enabled but yellow, prompts before POST
                #   - "risky"   : Opus-inferred or pattern_guess (pure guess)
                #                 → Send DISABLED, user must override
                #                 (should only appear on legacy reports; the
                #                 new pipeline discards these sources upstream)
                #   - "blocked" : on blacklist → Send HARD DISABLED
                if _blacklisted:
                    _risk = "blocked"
                elif email_source == "apollo":
                    _risk = "safe"
                elif email_source in ("editorial_scraped", "editorial_fallback"):
                    _risk = "safe"
                elif email_source == "apollo_likely":
                    _risk = "confirm"
                elif email_source in ("opus", "pattern_guess"):
                    _risk = "risky"
                elif contact_email:
                    # Unknown source but we have an email — treat as risky.
                    _risk = "risky"
                else:
                    _risk = "no_email"

                if mailto:
                    _mailto_btn = f'<a class="btn btn-mailto" href="{mailto}">Open in Mail</a>'
                else:
                    _mailto_btn = '<span class="btn btn-disabled" title="No email address available">Open in Mail</span>'

                if contact_email:
                    _email_js = _esc_js(contact_email)
                    _copy_addr_btn = f'<button class="btn btn-copy-addr" onclick="copyAddr(&#39;{_email_js}&#39;, this)">Copy address</button>'
                else:
                    _copy_addr_btn = ""

                if _risk == "blocked":
                    _send_btn = (
                        f'<span class="btn btn-disabled btn-send-blocked" '
                        f'title="Blacklisted: {_esc(_blacklist_reason)}">'
                        f'🚫 Send blocked</span>'
                    )
                elif _risk == "risky":
                    # Send disabled by default, but we offer a one-click
                    # override button that requires explicit confirmation.
                    _email_js = _esc_js(contact_email) if contact_email else ""
                    _send_btn = (
                        f'<button class="btn btn-send-risky" '
                        f'onclick="sendRiskyOutreach(this, &#39;{_email_js}&#39;, &#39;{_esc_js(email_source)}&#39;)">'
                        f'⚠️ Send anyway</button>'
                    )
                elif _risk == "confirm":
                    _email_js = _esc_js(contact_email)
                    _send_btn = (
                        f'<button class="btn btn-send-confirm" '
                        f'onclick="sendOutreachEmail(this, &#39;{_email_js}&#39;, &#39;apollo_likely&#39;)">'
                        f'Send (confirm)</button>'
                    )
                elif _risk == "safe":
                    _email_js = _esc_js(contact_email)
                    _send_btn = (
                        f'<button class="btn btn-send-email" '
                        f'onclick="sendOutreachEmail(this, &#39;{_email_js}&#39;)">'
                        f'Send now</button>'
                    )
                else:  # no_email
                    _send_btn = (
                        '<span class="btn btn-disabled" '
                        'title="No email address available">Send now</span>'
                    )

                role_span = (
                    f'<span class="pitch-role">({_esc(contact_role)})</span>'
                    if contact_role else ""
                )

                # Risk banner — renders at the top of the card only for
                # risky or blocked addresses, tells the user exactly
                # why Send is gated.
                if _risk == "blocked":
                    _risk_banner = (
                        f'<div class="pitch-risk-banner risk-blocked">'
                        f'🚫 <strong>Address blacklisted</strong> — this email bounced before: '
                        f'<em>{_esc(_blacklist_reason)}</em>. '
                        f'Send is blocked. Remove this address or find a new contact.'
                        f'</div>'
                    )
                elif _risk == "risky":
                    _risk_banner = (
                        f'<div class="pitch-risk-banner risk-risky">'
                        f'⚠️ <strong>Unverified address</strong> — source '
                        f'<code>{_esc(email_source or "?")}</code> is a guess, not a verified lookup. '
                        f'~50-60% of these bounce. Verify manually or set <code>APOLLO_API_KEY</code> '
                        f'and re-run the radar.'
                        f'</div>'
                    )
                elif _risk == "confirm":
                    _risk_banner = (
                        f'<div class="pitch-risk-banner risk-confirm">'
                        f'🟡 <strong>Apollo likely match</strong> — confirmation before send. '
                        f'Check the name/role match the journalist you intended.'
                        f'</div>'
                    )
                elif _risk == "safe" and email_routing == "editorial":
                    # Informational (not a warning): the pitch body has
                    # been auto-adjusted upstream to open with a
                    # newsroom-directed intro. Explain this to the user
                    # so they know *why* the copy reads differently and
                    # can still edit it before sending.
                    _source_hint = (
                        "pulled live from the article page"
                        if email_source == "editorial_scraped"
                        else "from our curated newsroom address table"
                    )
                    _risk_banner = (
                        f'<div class="pitch-risk-banner risk-editorial">'
                        f'📰 <strong>Routed to newsroom</strong> — we didn\'t find a '
                        f'direct address for the author, so this will go to a '
                        f'general editorial inbox ({_source_hint}). '
                        f'The intro has been auto-rewritten to explain this '
                        f'to whoever opens it — feel free to tweak below.'
                        f'</div>'
                    )
                else:
                    _risk_banner = ""

                pitch_block = f"""
            <div class="inline-pitch">
              <div class="inline-pitch-header">
                <span class="pitch-label">✉️ Email pitch ready</span>
                {contact_badge}
                {source_chip}
                {routing_chip}
              </div>
              {_risk_banner}
              <div class="pitch-recipient">
                <span class="pitch-label">To:</span>
                <strong>{_esc(contact_name)}</strong>
                {role_span}
              </div>
              <div class="pitch-subject">
                <span class="pitch-label">Subject:</span>
                <input type="text" class="pitch-subject-editor" value="{_esc(subject)}" />
              </div>
              <div class="pitch-body-wrap">
                <textarea class="pitch-body-editor">{_esc(body)}</textarea>
              </div>
              <div class="pitch-extra-attachments">
                <div class="pitch-attach-head">
                  <span class="pitch-label">📎 Extra attachments</span>
                  <span class="pitch-attach-hint">Policy: niente allegati nella prima mail. Usa solo se strettamente utile.</span>
                </div>
                <label class="btn btn-attach-file">
                  <input type="file" multiple class="pitch-attach-input" onchange="addExtraAttachment(this)" />
                  + Add file
                </label>
                <ul class="extra-attach-list"></ul>
              </div>
              <div class="card-actions">
                {_send_btn}
                {_mailto_btn}
                <button class="btn btn-copy-pitch" onclick="copyPitch(this)">Copy email</button>
                {_copy_addr_btn}
              </div>
            </div>"""

            # ── Insight block (Summary + Angle) for non-email news ──
            # Emails already have the full pitch_block (subject + body),
            # so they don't need the structured insight card. Tweets and
            # reddit comments benefit the most — same treatment as Early
            # Signals, so the user sees WHY the item matters and HOW to
            # engage without having to expand anything.
            insight_block = ""
            if dtype != "email":
                _summary = (item.get("summary") or "").strip()
                _angle = (item.get("engagement_angle") or "").strip()
                if _summary or _angle:
                    insight_parts = []
                    if _summary:
                        insight_parts.append(
                            f'<div class="early-insight-row">'
                            f'<span class="early-insight-label">Summary</span>'
                            f'<span class="early-insight-text">{_esc(_summary)}</span>'
                            f'</div>'
                        )
                    if _angle:
                        insight_parts.append(
                            f'<div class="early-insight-row">'
                            f'<span class="early-insight-label">Angle</span>'
                            f'<span class="early-insight-text">{_esc(_angle)}</span>'
                            f'</div>'
                        )
                    insight_block = (
                        f'<div class="early-insight">{"".join(insight_parts)}</div>'
                    )

            # ── Tweet reply block — if a draft body is available ──
            # Tweets in Radar News had ready reply drafts that were never
            # being displayed. Render the same textarea + action buttons
            # used by the viral cards so the user can send or copy the
            # reply without leaving the dashboard.
            tweet_reply_block = ""
            if dtype == "tweet":
                _tweet_body = (item.get("draft_body") or "").strip()
                if _tweet_body:
                    _char_count = len(_tweet_body)
                    _char_warn = " over-limit" if _char_count > 280 else ""
                    tweet_reply_block = f"""
            <div class="viral-reply-wrap">
              <div class="viral-reply-label">
                <span class="pitch-label">Suggested reply</span>
                <span class="reply-char-count{_char_warn}">{_char_count} chars</span>
              </div>
              <textarea class="viral-reply-editor" data-platform="x" oninput="updateViralCharCount(this)">{_esc(_tweet_body)}</textarea>
              <div class="card-actions">
                <button class="btn btn-viral-reply" onclick="openViralReplyX(this)">Reply on X</button>
                <button class="btn btn-copy-pitch" onclick="copyViralReply(this)">Copy reply</button>
                <a class="btn btn-copy-open" href="{_esc(url)}" target="_blank" rel="noreferrer">Open tweet</a>
              </div>
            </div>"""

            reach_label = item.get("reach_label")
            reach_estimated = item.get("reach_estimated", False)
            if reach_label:
                _tooltip = "AI-estimated reach" if reach_estimated else "Verified reach"
                _est_marker = " ⓘ" if reach_estimated else ""
                _est_class = " badge-reach-estimated" if reach_estimated else ""
                reach_badge = (
                    f'<span class="badge badge-reach{_est_class}" '
                    f'title="{_tooltip}">👥 {_esc(reach_label)}{_est_marker}</span>'
                )
            else:
                reach_badge = ''

            author_name = item.get("author_name", "")
            author_block = (
                f'<p class="news-byline">By <strong>{_esc(author_name)}</strong></p>'
                if author_name else ''
            )

            # Avoid rendering the short snippet when a richer structured
            # insight block is already being shown — both fields typically
            # draw from the same `summary` string, so duplicating the text
            # above the insight block is visual noise.
            snippet_block = (
                f'<p class="news-snippet">{_esc(snippet)}</p>'
                if snippet and not insight_block else ''
            )

            # Discard footer — single button at the bottom of every Radar
            # News card. Adds the URL to previously_reported.json so the
            # item disappears immediately AND is excluded from every
            # future radar scan. URL is escaped for the JS context so a
            # URL with quotes can't break the handler.
            discard_block = (
                f'<div class="news-card-discard">'
                f'<button class="btn btn-discard-news" '
                f'onclick="dismissRadarItem(\'{_esc_js(url)}\', this)" '
                f'title="Hide this item and prevent it from re-appearing in future radar scans">'
                f'🗑 Discard'
                f'</button>'
                f'</div>'
            )

            news_cards += f"""
        <div class="card news-card" data-score="{score}" data-type="{dtype}">
          <div class="card-status-stripe"></div>
          <div class="card-body">
            <div class="card-header">
              <span class="badge {badge_class}">{badge_label}</span>
              <span class="badge badge-score">Score {score}</span>
              <span class="news-pub">{_esc(pub)}</span>
              {reach_badge}
              {f'<span class="news-date">{_esc(date_str)}</span>' if date_str else ''}
            </div>
            <h3 class="card-title news-title"><a href="{_esc(url)}" target="_blank" rel="noreferrer">{_esc(title)}</a></h3>
            {author_block}
            {snippet_block}
            {insight_block}
            {tweet_reply_block}
            {pitch_block}
            {discard_block}
          </div>
        </div>"""

    # ── Reply cards (Sprint 2 — follow-up on journalist replies) ─────
    # Each card shows the journalist's reply (collapsible), then the
    # drafted response (editable), plus attachment toggles and the
    # action buttons. `needs_human` cards disable Send and display a red
    # banner so the operator knows to step in manually.
    reply_classifications = {
        "request_material": ("badge-reply-material", "📎 Material request",
                              "Send press kit + fact sheet."),
        "request_call":     ("badge-reply-call", "📅 Call request",
                              "Propose 3 slots with Paolo."),
        "request_both":     ("badge-reply-both", "📎📅 Material + call",
                              "Send material AND propose slots."),
        "polite_decline":   ("badge-reply-decline", "🙏 Polite decline",
                              "Thank them, leave the door open."),
        "needs_human":      ("badge-reply-human", "🚨 Needs human review",
                              "Drafter bailed — step in manually."),
    }
    reply_cards = ""
    for rd in reply_drafts:
        cls = rd["classification"] or "needs_human"
        badge_cls, badge_lbl, badge_hint = reply_classifications.get(
            cls, reply_classifications["needs_human"]
        )
        conf = rd["confidence"] or 0.0
        conf_badge = (
            f'<span class="badge badge-reply-conf" title="Drafter confidence">'
            f'conf {conf:.2f}</span>'
        )

        # Human-readable recipient chip — falls back to raw address.
        to_name = rd["to_name"] or ""
        to_addr = rd["to"] or ""
        if to_name and to_addr:
            to_chip = f'<strong>{_esc(to_name)}</strong> &lt;{_esc(to_addr)}&gt;'
        else:
            to_chip = f'<strong>{_esc(to_addr)}</strong>'

        # First-touch outreach context (what we sent originally).
        outreach_meta = ""
        if rd.get("outreach_subject"):
            _outreach_when = rd.get("outreach_sent_at", "") or ""
            if _outreach_when:
                _outreach_when = f" · sent {_esc(_outreach_when[:10])}"
            outreach_meta = (
                f'<div class="reply-outreach-meta">'
                f'<span class="pitch-label">First touch:</span> '
                f'<em>{_esc(rd["outreach_subject"])}</em>{_outreach_when}'
                f'</div>'
            )

        # The journalist's actual reply body — collapsible so the card
        # stays scannable. Snippet is shown by default as the preview.
        j_body = (rd.get("journalist_body") or "").strip()
        j_snippet = (rd.get("journalist_snippet") or "").strip()
        if not j_snippet and j_body:
            j_snippet = j_body[:220] + ("…" if len(j_body) > 220 else "")
        _received_at = (rd.get("replying_to_received_at") or "")[:16].replace("T", " ")
        _received_chip = (
            f'<span class="reply-received-chip">received {_esc(_received_at)}</span>'
            if _received_at else ""
        )
        if j_body:
            journalist_block = f"""
              <details class="reply-journalist">
                <summary>
                  <span class="pitch-label">Journalist said:</span>
                  {_received_chip}
                  <span class="reply-journalist-preview">{_esc(j_snippet)}</span>
                </summary>
                <pre class="reply-journalist-body">{_esc(j_body)}</pre>
              </details>"""
        else:
            journalist_block = (
                f'<div class="reply-journalist reply-journalist-empty">'
                f'<span class="pitch-label">Journalist reply body unavailable</span>'
                f'</div>'
            )

        # Drafter reasoning (why this classification + short rationale).
        reasoning = (rd.get("reasoning") or "").strip()
        reasoning_block = (
            f'<div class="reply-reasoning"><span class="pitch-label">'
            f'Why this pattern:</span> {_esc(reasoning)}</div>'
            if reasoning else ""
        )

        # Needs-human banner + suggested next step (no Send button).
        banner_block = ""
        if cls == "needs_human":
            _next_step = (rd.get("suggested_next_step") or "").strip()
            _step_line = (
                f' <em>{_esc(_next_step)}</em>' if _next_step else ""
            )
            banner_block = (
                f'<div class="reply-needs-human">'
                f'🚨 This reply needs human attention.{_step_line}'
                f'</div>'
            )

        # Attachments — press kit + fact sheet. Pre-checked unless the
        # drafter said not to include them (polite_decline / needs_human).
        default_attachments = {
            "_system/outreach/attachments/MyVilla_Press_Kit.pdf": "Press Kit",
            "_system/outreach/attachments/MyVilla_Fact_Sheet.pdf": "Fact Sheet",
        }
        rd_attachments = set(rd.get("attachments") or [])
        attach_checks = []
        for path, label in default_attachments.items():
            checked = "checked" if path in rd_attachments else ""
            attach_checks.append(
                f'<label class="reply-attach-item">'
                f'<input type="checkbox" class="reply-attach-toggle" '
                f'data-path="{_esc(path)}" {checked} /> '
                f'<span>{label}</span> '
                f'<span class="reply-attach-path">{_esc(path.rsplit("/", 1)[-1])}</span>'
                f'</label>'
            )
        attachments_block = (
            f'<div class="reply-attachments">'
            f'<span class="pitch-label">Attachments:</span>'
            f'{"".join(attach_checks)}'
            f'</div>'
        )

        # Action buttons — Send disabled when the card needs human review.
        if cls == "needs_human":
            _send_btn = (
                '<span class="btn btn-disabled" '
                'title="Needs human — Send is disabled">Send reply</span>'
            )
        else:
            _send_btn = (
                '<button class="btn btn-send-reply" '
                'onclick="sendReplyEmail(this)">Send reply</button>'
            )
        _redraft_btn = (
            '<button class="btn btn-redraft-reply" '
            'onclick="redraftReply(this)">Re-draft</button>'
        )
        _dismiss_btn = (
            '<button class="btn btn-reject" '
            'onclick="dismissReply(this)">Dismiss</button>'
        )

        # Thread id is the card key — encoded for every POST handler.
        thread_id = rd["thread_id"]
        in_reply_to = rd.get("in_reply_to") or ""
        references = rd.get("references") or in_reply_to or ""

        reply_cards += f"""
        <div class="card reply-card"
             data-thread-id="{_esc(thread_id)}"
             data-to="{_esc(to_addr)}"
             data-in-reply-to="{_esc(in_reply_to)}"
             data-references="{_esc(references)}"
             data-classification="{_esc(cls)}">
          <div class="card-status-stripe"></div>
          <div class="card-body">
            <div class="card-header">
              <span class="badge {badge_cls}">{badge_lbl}</span>
              {conf_badge}
              <span class="reply-hint">{_esc(badge_hint)}</span>
            </div>
            <div class="reply-recipient">
              <span class="pitch-label">To:</span>
              {to_chip}
            </div>
            {outreach_meta}
            {journalist_block}
            {reasoning_block}
            {banner_block}
            <div class="pitch-subject">
              <span class="pitch-label">Subject:</span>
              <input type="text" class="reply-subject-editor"
                     value="{_esc(rd['subject'])}" />
            </div>
            <div class="pitch-body-wrap">
              <textarea class="reply-body-editor">{_esc(rd['body'])}</textarea>
            </div>
            {attachments_block}
            <div class="pitch-extra-attachments">
              <div class="pitch-attach-head">
                <span class="pitch-label">📎 Extra attachments</span>
                <span class="pitch-attach-hint">Aggiungi file specifici se il giornalista li ha chiesti.</span>
              </div>
              <label class="btn btn-attach-file">
                <input type="file" multiple class="pitch-attach-input" onchange="addExtraAttachment(this)" />
                + Add file
              </label>
              <ul class="extra-attach-list"></ul>
            </div>
            <div class="card-actions">
              {_send_btn}
              {_redraft_btn}
              {_dismiss_btn}
            </div>
          </div>
        </div>"""

    # Empty state
    empty_msg = ""
    has_radar = radar and (radar.get("news") or radar.get("emails"))
    has_replies = bool(reply_drafts)
    if total == 0 and not has_radar and not has_replies:
        empty_msg = """
        <div class="empty-state">
          <div class="empty-icon">&#10024;</div>
          <h2>No pending drafts &mdash; all clear!</h2>
          <p>There are no articles, social posts, radar news, or replies awaiting review.</p>
        </div>"""

    # ── Strip "coda & pubblicati" social (IG + X) ─────────────────────
    # Stato delle code di pubblicazione automatica: quanti approvati in
    # attesa per canale + cosa è uscito oggi (con permalink).
    def _social_state_strip():
        from datetime import timezone as _tz
        posts_root = SYSTEM_DIR / "social" / "posts"
        today = datetime.now(_tz.utc).strftime("%Y-%m-%d")
        q_ig = q_x = 0
        pub_today = []
        fm_re = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)
        for sub, is_pub in (("approved", False), ("published", True)):
            d = posts_root / sub
            if not d.exists():
                continue
            for f in d.glob("*.md"):
                try:
                    m = fm_re.match(f.read_text(encoding="utf-8",
                                                errors="replace"))
                except OSError:
                    continue
                fm_txt = (m.group(1) if m else "").lower()
                ch = "ig" if ("channel: ig" in fm_txt
                              or "channel: instagram" in fm_txt) else \
                     "x" if ("channel: x" in fm_txt
                             or "channel: twitter" in fm_txt) else "?"
                if not is_pub:
                    if ch == "ig":
                        q_ig += 1
                    elif ch == "x":
                        q_x += 1
                elif f"published_at: {today}" in fm_txt \
                        or f"published_at: '{today}" in fm_txt \
                        or f'published_at: "{today}' in fm_txt:
                    perma = ""
                    pm = re.search(r"ig_permalink:\s*(\S+)", fm_txt)
                    if pm:
                        perma = pm.group(1).strip("'\"")
                    pub_today.append((ch, f.name, perma))
        # Commenti Reddit pubblicati oggi (dal log del client)
        rc_today = 0
        rc_log = SYSTEM_DIR / "outreach" / "reddit_comment_log.jsonl"
        if rc_log.exists():
            for line in rc_log.read_text(encoding="utf-8").splitlines():
                try:
                    rr = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rr.get("ok") and str(rr.get("timestamp", "")).startswith(today):
                    rc_today += 1

        items = []
        items.append(f"<strong>In coda</strong> — Instagram: {q_ig} · X: {q_x}")
        items.append(f"<strong>Commenti Reddit oggi</strong>: {rc_today}/5")
        if pub_today:
            links = " · ".join(
                (f'<a href="{_esc(pl)}" target="_blank">{ch.upper()} ✓</a>'
                 if pl else f"{ch.upper()} ✓ {_esc(fn[:30])}")
                for ch, fn, pl in pub_today)
            items.append(f"<strong>Pubblicati oggi</strong>: {links}")
        else:
            items.append("<strong>Pubblicati oggi</strong>: —")
        spans = "".join(f"<span>{it}</span>" for it in items)
        return ('<div class="section" style="padding:0.8rem 1.2rem;">'
                '<div style="display:flex;gap:1.6rem;flex-wrap:wrap;'
                'font-size:0.85rem;color:#5a5247;align-items:center;">'
                '<strong style="color:#1a1a2e;">📋 Il lavoro di oggi: '
                'approva 1 post IG + 1 X, pubblica 2-3 commenti</strong>'
                + spans +
                '<span style="color:#999;">la coda esce da sola, 1/canale '
                'al giorno</span>'
                '</div></div>')

    try:
        ig_queue_strip = _social_state_strip()
    except Exception as _e:  # noqa: BLE001 — la strip non deve rompere la pagina
        ig_queue_strip = ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>My Villa &mdash; Content Review</title>
<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:ital,wght@0,400;0,600;0,700;1,400&family=Montserrat:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --terracotta: #C2714F;
    --olive: #5C6B4F;
    --tuscan-gold: #C4A265;
    --warm-sand: #D4B896;
    --espresso: #3E2F2B;
    --pacific-blue: #6B8F9E;
    --sand-white: #F0EBE3;
    --charcoal: #2C2C2C;
    --cream: #FAF8F5;
    --serif: 'Cormorant Garamond', Georgia, serif;
    --sans: 'Montserrat', 'Helvetica Neue', Arial, sans-serif;
  }}

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  body {{
    font-family: var(--sans);
    background: var(--cream);
    color: var(--charcoal);
    line-height: 1.6;
    min-height: 100vh;
  }}

  /* ── Header ──────────────────────────────────────── */
  .header {{
    background: var(--espresso);
    color: var(--sand-white);
    padding: 2rem 2rem 1.8rem;
    text-align: center;
  }}
  .header h1 {{
    font-family: var(--serif);
    font-size: 2.4rem;
    font-weight: 600;
    letter-spacing: 0.02em;
    margin-bottom: 0.3rem;
  }}
  .header h1 span {{ color: var(--tuscan-gold); }}
  .header .subtitle {{
    font-size: 0.85rem;
    font-weight: 300;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--warm-sand);
  }}
  .header .counts {{
    margin-top: 1rem;
    display: flex;
    justify-content: center;
    gap: 1.5rem;
  }}
  .header .count-pill {{
    background: rgba(255,255,255,0.1);
    border: 1px solid rgba(255,255,255,0.15);
    border-radius: 20px;
    padding: 0.35rem 1rem;
    font-size: 0.8rem;
    font-weight: 500;
    letter-spacing: 0.04em;
  }}
  .header .count-pill strong {{ color: var(--tuscan-gold); }}

  /* ── Main ─────────────────────────────────────────── */
  .main {{
    max-width: 960px;
    margin: 0 auto;
    padding: 2.5rem 1.5rem 4rem;
  }}

  .section-heading {{
    font-family: var(--serif);
    font-size: 1.6rem;
    font-weight: 600;
    color: var(--espresso);
    margin-bottom: 1.2rem;
    padding-bottom: 0.5rem;
    border-bottom: 2px solid var(--warm-sand);
  }}
  .section-heading .section-count {{
    font-family: var(--sans);
    font-size: 0.75rem;
    font-weight: 500;
    background: var(--sand-white);
    color: var(--espresso);
    border-radius: 12px;
    padding: 0.2rem 0.7rem;
    margin-left: 0.6rem;
    vertical-align: middle;
  }}

  .section + .section {{ margin-top: 3rem; }}

  /* ── Cards ────────────────────────────────────────── */
  .card {{
    background: #fff;
    border-radius: 12px;
    box-shadow: 0 2px 12px rgba(62,47,43,0.07);
    margin-bottom: 1.2rem;
    overflow: hidden;
    display: flex;
    transition: box-shadow 0.25s, opacity 0.3s;
  }}
  .card:hover {{
    box-shadow: 0 4px 20px rgba(62,47,43,0.12);
  }}

  .card-status-stripe {{
    width: 5px;
    flex-shrink: 0;
    background: transparent;
    transition: background 0.3s;
  }}
  .card.approved .card-status-stripe {{ background: var(--olive); }}
  .card.rejected .card-status-stripe {{ background: var(--terracotta); }}

  .card.approved, .card.rejected {{
    opacity: 0.55;
  }}

  .card-body {{
    padding: 1.4rem 1.6rem;
    flex: 1;
    min-width: 0;
  }}

  .card-header {{
    display: flex;
    align-items: center;
    gap: 0.6rem;
    flex-wrap: wrap;
    margin-bottom: 0.6rem;
  }}

  .card-title {{
    font-family: var(--serif);
    font-size: 1.25rem;
    font-weight: 600;
    color: var(--espresso);
    margin: 0;
  }}

  .badge {{
    display: inline-block;
    font-size: 0.65rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    padding: 0.2rem 0.6rem;
    border-radius: 4px;
  }}
  .badge-section {{
    background: var(--sand-white);
    color: var(--olive);
    border: 1px solid var(--olive);
  }}
  .badge-type {{
    background: var(--sand-white);
    color: var(--pacific-blue);
    border: 1px solid var(--pacific-blue);
  }}
  .badge-x {{
    background: var(--charcoal);
    color: #fff;
  }}
  .badge-ig {{
    background: linear-gradient(135deg, #833AB4, #E1306C, #F77737);
    color: #fff;
  }}
  .badge-email {{
    background: var(--pacific-blue);
    color: #fff;
  }}
  .badge-tweet {{
    background: var(--charcoal);
    color: #fff;
  }}
  .badge-reddit {{
    background: #ff4500;
    color: #fff;
  }}
  .badge-score {{
    background: var(--tuscan-gold);
    color: var(--espresso);
  }}
  .badge-email-ok {{
    background: rgba(92,107,79,0.15);
    color: var(--olive);
    border: 1px solid var(--olive);
    font-family: ui-monospace, monospace;
    text-transform: lowercase;
    letter-spacing: 0;
  }}
  .badge-email-missing {{
    background: rgba(194,113,79,0.12);
    color: var(--terracotta);
    border: 1px solid var(--terracotta);
  }}
  .badge-email-verified {{
    background: rgba(76,175,80,0.15);
    color: #2e7d32;
    border: 1px solid #2e7d32;
    font-weight: 600;
  }}
  .badge-email-guess {{
    background: rgba(196,162,101,0.12);
    color: #8a6a1f;
    border: 1px dashed #8a6a1f;
    font-size: 0.65rem;
  }}
  .badge-email-editorial {{
    background: rgba(107,143,158,0.12);
    color: #4a6878;
    border: 1px solid #4a6878;
    font-size: 0.65rem;
  }}
  .badge-email-scraped {{
    /* Fresher than editorial_fallback — pulled live from the article
       page. Slightly saturated green to distinguish from Apollo's
       verified green and from the muted editorial_fallback blue. */
    background: rgba(52,168,83,0.14);
    color: #1f6e37;
    border: 1px solid #1f6e37;
    font-size: 0.65rem;
    font-weight: 600;
  }}
  .badge-route-author {{
    background: rgba(30,108,158,0.10);
    color: #1f5a8c;
    border: 1px dashed #1f5a8c;
    font-size: 0.6rem;
    letter-spacing: 0.02em;
    padding: 0.15rem 0.45rem;
  }}
  .badge-route-editorial {{
    background: rgba(148,90,34,0.10);
    color: #865018;
    border: 1px dashed #865018;
    font-size: 0.6rem;
    letter-spacing: 0.02em;
    padding: 0.15rem 0.45rem;
  }}
  .badge-email-risky {{
    background: rgba(194,113,79,0.15);
    color: #a03b0e;
    border: 1px solid #a03b0e;
    font-weight: 600;
    font-size: 0.65rem;
  }}
  .badge-email-bounced {{
    background: rgba(176,42,42,0.15);
    color: #8b0000;
    border: 1px solid #8b0000;
    font-weight: 700;
    font-family: ui-monospace, monospace;
    text-transform: lowercase;
  }}

  /* ── Risk banner inside pitch card ──────────────── */
  .pitch-risk-banner {{
    margin: 0.6rem 0 0.8rem;
    padding: 0.65rem 0.85rem;
    border-radius: 6px;
    font-size: 0.82rem;
    line-height: 1.45;
  }}
  .pitch-risk-banner code {{
    font-family: ui-monospace, SFMono-Regular, Consolas, monospace;
    background: rgba(0,0,0,0.06);
    padding: 0.05rem 0.35rem;
    border-radius: 3px;
    font-size: 0.92em;
  }}
  .pitch-risk-banner.risk-blocked {{
    background: rgba(176,42,42,0.1);
    border: 1px solid #8b0000;
    color: #8b0000;
  }}
  .pitch-risk-banner.risk-risky {{
    background: rgba(212,118,21,0.1);
    border: 1px solid #a03b0e;
    color: #a03b0e;
  }}
  .pitch-risk-banner.risk-confirm {{
    background: rgba(196,162,101,0.12);
    border: 1px solid #8a6a1f;
    color: #6b4e0e;
  }}
  .pitch-risk-banner.risk-editorial {{
    /* Informational, not a warning — the pitch is safe to send, we just
       want the user to understand why the intro copy is worded for a
       newsroom rather than an individual author. */
    background: rgba(52,168,83,0.08);
    border: 1px solid #2e7d32;
    color: #1f5a27;
  }}

  /* ── Send button variants (risk-based) ───────────── */
  .btn-send-risky {{
    background: #d47615 !important;
    color: #fff !important;
    border: 1px solid #a03b0e !important;
  }}
  .btn-send-risky:hover {{
    background: #a03b0e !important;
  }}
  .btn-send-confirm {{
    background: #c4a265 !important;
    color: #3b2a0e !important;
    border: 1px solid #8a6a1f !important;
  }}
  .btn-send-confirm:hover {{
    background: #a88846 !important;
  }}
  .btn-send-blocked {{
    background: #8b0000 !important;
    color: #fff !important;
    cursor: not-allowed !important;
    opacity: 0.75;
  }}

  /* ── Published Articles rows (compact list) ──────────── */
  .published-row {{
    display: flex;
    align-items: center;
    gap: 1rem;
    padding: 0.75rem 1rem;
    background: #fff;
    border: 1px solid rgba(0,0,0,0.06);
    border-radius: 6px;
    margin-bottom: 0.5rem;
    transition: background 0.15s;
  }}
  .published-row:hover {{ background: #faf6f0; }}
  .published-row-main {{
    flex: 1;
    min-width: 0;
  }}
  .published-row-title {{
    font-weight: 600;
    color: var(--espresso);
    font-size: 0.95rem;
    line-height: 1.3;
    margin-bottom: 0.3rem;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .published-row-meta {{
    display: flex;
    align-items: center;
    gap: 0.7rem;
    font-size: 0.75rem;
    color: #888;
  }}
  .published-row-section {{ font-size: 0.7rem; }}
  .published-row-date {{ font-variant-numeric: tabular-nums; }}
  .published-row-actions {{
    display: flex;
    gap: 0.3rem;
    flex-shrink: 0;
  }}
  .btn-mini-danger {{
    background: rgba(168,93,63,0.08);
    color: #a85d3f;
    border: 1px solid rgba(168,93,63,0.25);
  }}
  .btn-mini-danger:hover {{
    background: rgba(168,93,63,0.18);
    border-color: rgba(168,93,63,0.45);
  }}
  @media (max-width: 720px) {{
    .published-row {{
      flex-direction: column;
      align-items: stretch;
    }}
    .published-row-actions {{ justify-content: flex-end; }}
  }}

  /* ── IG companion block (inside journal cards) ──────── */
  .ig-companion-block {{
    margin: 0.9rem 0 0.6rem;
    padding: 0.7rem 0.9rem;
    background: linear-gradient(135deg, #fff5f5, #fef0f5);
    border: 1px solid rgba(220,100,140,0.18);
    border-radius: 7px;
  }}
  .ig-companion-block.ig-companion-empty {{
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
  }}
  .ig-companion-head {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    margin-bottom: 0.5rem;
  }}
  .ig-companion-label {{
    font-size: 0.78rem;
    color: #b03060;
    font-weight: 600;
    letter-spacing: 0.02em;
  }}
  .ig-companion-editor {{
    width: 100%;
    min-height: 95px;
    padding: 0.55rem 0.7rem;
    border: 1px solid rgba(0,0,0,0.1);
    border-radius: 5px;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    font-size: 0.83rem;
    line-height: 1.45;
    resize: vertical;
    background: #fff;
    color: #2c2c2c;
    box-sizing: border-box;
  }}
  .ig-companion-editor:focus {{
    outline: none;
    border-color: #b03060;
    box-shadow: 0 0 0 2px rgba(176,48,96,0.1);
  }}
  .ig-companion-foot {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 0.4rem;
    font-size: 0.72rem;
    color: #888;
  }}
  .ig-companion-charcount {{ font-variant-numeric: tabular-nums; }}
  .ig-companion-charcount.over-limit {{ color: #c0392b; font-weight: 600; }}
  .ig-companion-hint {{ font-style: italic; }}
  .ig-companion-hint code {{
    background: rgba(0,0,0,0.05);
    padding: 1px 4px;
    border-radius: 3px;
    font-size: 0.9em;
  }}
  .btn-mini {{
    background: rgba(176,48,96,0.08);
    color: #b03060;
    border: 1px solid rgba(176,48,96,0.25);
    padding: 0.28rem 0.65rem;
    font-size: 0.72rem;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.15s;
  }}
  .btn-mini:hover {{
    background: rgba(176,48,96,0.16);
    border-color: rgba(176,48,96,0.45);
  }}
  .btn-mini:disabled {{ opacity: 0.5; cursor: wait; }}
  .btn-ig-generate {{ background: rgba(176,48,96,0.12); }}

  /* ── Radar News cards ─────────────────────────────── */
  .section-subtitle {{
    font-size: 0.85rem;
    color: #888;
    margin: -0.6rem 0 1rem;
    font-style: italic;
  }}

  /* Discreet Discard footer at the bottom of every Radar News card.
     Right-aligned, neutral tone — it's a permanent dismissal so we
     don't want it visually competing with Send/Copy buttons above. */
  .news-card-discard {{
    display: flex;
    justify-content: flex-end;
    margin-top: 0.9rem;
    padding-top: 0.7rem;
    border-top: 1px dashed rgba(0,0,0,0.08);
  }}
  .btn-discard-news {{
    background: transparent;
    color: #888;
    border: 1px solid rgba(0,0,0,0.12);
    padding: 0.35rem 0.85rem;
    font-size: 0.78rem;
    cursor: pointer;
    border-radius: 4px;
    transition: all 0.15s;
  }}
  .btn-discard-news:hover {{
    color: #a85d3f;
    border-color: #a85d3f;
    background: rgba(168,93,63,0.05);
  }}
  .btn-discard-news:disabled {{
    opacity: 0.5;
    cursor: wait;
  }}
  .radar-meta {{
    margin-top: 0.6rem;
    font-size: 0.75rem;
    color: var(--warm-sand);
    letter-spacing: 0.04em;
  }}
  .radar-meta a {{
    color: var(--tuscan-gold);
    text-decoration: none;
    border-bottom: 1px solid rgba(196,162,101,0.4);
  }}
  .radar-meta a:hover {{ border-bottom-color: var(--tuscan-gold); }}
  .news-card .card-body {{ padding: 1rem 1.3rem; }}
  .news-pub {{
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--stone-grey, #666);
  }}
  .news-date {{
    font-size: 0.7rem;
    color: #999;
    margin-left: auto;
  }}
  .news-title {{
    font-size: 1.05rem;
    line-height: 1.35;
    margin: 0.5rem 0 0.3rem;
  }}
  .news-title a {{
    color: var(--espresso);
    text-decoration: none;
    border-bottom: 1px solid transparent;
  }}
  .news-byline {{
    font-size: 0.75rem;
    color: #888;
    margin: 0 0 0.6rem;
    font-style: italic;
  }}
  .news-byline strong {{
    color: var(--olive);
    font-weight: 600;
    font-style: normal;
  }}
  .news-title a:hover {{
    color: var(--terracotta);
    border-bottom-color: var(--terracotta);
  }}
  .news-snippet {{
    font-size: 0.85rem;
    color: #555;
    line-height: 1.55;
  }}

  /* ── Journalist Pitch cards ───────────────────────── */
  .pitch-card .card-body {{ padding: 1.2rem 1.4rem; }}
  .pitch-label {{
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: #999;
    display: inline-block;
    min-width: 60px;
  }}
  .pitch-article-ref,
  .pitch-recipient,
  .pitch-subject {{
    font-size: 0.85rem;
    margin-bottom: 0.5rem;
    color: #444;
  }}
  .pitch-article-ref a {{
    color: var(--pacific-blue);
    text-decoration: none;
    border-bottom: 1px solid rgba(107,143,158,0.3);
  }}
  .pitch-role {{
    color: #888;
    font-style: italic;
  }}
  .pitch-subject-text {{
    font-weight: 600;
    color: var(--espresso);
  }}
  .pitch-body-wrap {{ margin: 0.8rem 0; }}
  .pitch-body-editor {{
    width: 100%;
    min-height: 180px;
    font-family: var(--sans);
    font-size: 0.86rem;
    color: #333;
    background: var(--sand-white);
    border: 1px solid var(--warm-sand);
    border-radius: 6px;
    padding: 0.8rem 1rem;
    line-height: 1.6;
    resize: vertical;
  }}
  .pitch-body-editor:focus {{
    outline: none;
    border-color: var(--pacific-blue);
    box-shadow: 0 0 0 3px rgba(107,143,158,0.18);
  }}
  /* ── Extra attachments (user-added files) ─────────────── */
  .pitch-extra-attachments {{
    margin: 0.8rem 0;
    padding: 0.6rem 0.8rem;
    background: #fafaf7;
    border: 1px dashed var(--warm-sand);
    border-radius: 6px;
  }}
  .pitch-attach-head {{
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.6rem;
    margin-bottom: 0.5rem;
  }}
  .pitch-attach-hint {{
    font-size: 0.72rem;
    color: #8a7a62;
    font-style: italic;
  }}
  .btn-attach-file {{
    display: inline-block;
    cursor: pointer;
    background: #fff;
    border: 1px solid var(--warm-sand);
    border-radius: 5px;
    padding: 0.35rem 0.8rem;
    font-size: 0.78rem;
    color: var(--espresso);
    transition: background 0.15s ease, border-color 0.15s ease;
  }}
  .btn-attach-file:hover {{
    background: var(--sand-white);
    border-color: var(--pacific-blue);
  }}
  .pitch-attach-input {{
    display: none;
  }}
  .extra-attach-list {{
    list-style: none;
    margin: 0.5rem 0 0;
    padding: 0;
  }}
  .extra-attach-item {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.6rem;
    padding: 0.3rem 0.5rem;
    margin-top: 0.25rem;
    background: #fff;
    border: 1px solid #eee2cf;
    border-radius: 4px;
    font-size: 0.78rem;
  }}
  .extra-attach-item .fname {{
    flex: 1;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--espresso);
  }}
  .extra-attach-item .fsize {{
    color: #999;
    font-size: 0.72rem;
    white-space: nowrap;
  }}
  .extra-attach-item .fremove {{
    background: transparent;
    border: none;
    cursor: pointer;
    color: #b0463a;
    font-size: 1rem;
    padding: 0 0.3rem;
    line-height: 1;
  }}
  .extra-attach-item .fremove:hover {{
    color: #800;
  }}
  .btn-send-email {{
    background: #2d7a52;
    color: #fff;
    border: none;
    padding: 0.5rem 1.1rem;
    border-radius: 6px;
    font-size: 0.78rem;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s, box-shadow 0.15s, transform 0.1s;
  }}
  .btn-send-email:hover {{
    background: #236040;
    box-shadow: 0 2px 8px rgba(45,122,82,0.3);
  }}
  .btn-send-email:disabled,
  .btn-send-email.sending {{
    background: #8db39d;
    cursor: wait;
  }}
  .btn-send-email.sent {{
    background: #4a9670;
    cursor: default;
  }}
  .btn-send-email.failed {{
    background: #b34e4e;
    cursor: pointer;
  }}
  .btn-mailto {{
    background: var(--pacific-blue);
    color: #fff;
  }}
  .btn-mailto:hover {{ background: #547a89; box-shadow: 0 2px 8px rgba(107,143,158,0.3); }}
  .btn-copy-pitch,
  .btn-copy-addr {{
    background: var(--sand-white);
    color: var(--espresso);
    border: 1px solid var(--warm-sand);
  }}
  .btn-copy-pitch:hover,
  .btn-copy-addr:hover {{ background: var(--warm-sand); }}
  .btn-disabled {{
    background: #eee;
    color: #bbb;
    cursor: not-allowed;
    padding: 0.5rem 1.1rem;
    border-radius: 6px;
    font-size: 0.78rem;
    font-weight: 600;
  }}

  /* ── Reach badge (shows audience size) ──────────────── */
  .badge-reach {{
    background: rgba(107,143,158,0.12);
    color: var(--pacific-blue);
    border: 1px solid rgba(107,143,158,0.3);
    font-weight: 500;
    font-family: ui-monospace, monospace;
    letter-spacing: 0;
  }}
  .badge-reach-estimated {{
    border-style: dashed;
    opacity: 0.85;
    cursor: help;
  }}

  /* ── Early Signals (collapsible, compact cards) ──────── */
  .count-pill.count-early strong {{ color: #7a9470; }}
  .section-collapsed .section-body {{ display: none; }}
  .section-collapsed .collapse-toggle::before {{ content: "▸ "; }}
  .section:not(.section-collapsed) .collapse-toggle {{ display: none; }}
  .collapse-toggle {{
    float: right;
    font-family: var(--sans);
    font-size: 0.72rem;
    font-weight: 400;
    color: var(--stone-grey, #888);
    cursor: pointer;
    text-transform: none;
    letter-spacing: 0;
    margin-top: 0.3rem;
  }}
  .section-heading {{ cursor: pointer; user-select: none; }}
  .early-card {{ background: #fafaf7; }}
  .early-card .card-body {{ padding: 0.8rem 1rem; }}
  .early-card .card-header {{ margin-bottom: 0.4rem; }}
  .early-meta {{
    font-size: 0.72rem;
    font-family: ui-monospace, monospace;
    color: #666;
    letter-spacing: 0;
  }}
  .early-content {{
    font-size: 0.82rem;
    color: #555;
    line-height: 1.5;
    margin: 0.4rem 0 0.6rem;
    font-style: italic;
  }}
  .early-actions {{
    display: flex;
    align-items: center;
    gap: 0.8rem;
  }}
  .early-hint {{
    font-size: 0.72rem;
    color: #999;
    font-style: italic;
  }}
  /* Actionable insight block inside an early-signal card */
  .early-insight {{
    margin: 0.5rem 0 0.7rem;
    padding: 0.6rem 0.8rem;
    background: rgba(196,162,101,0.08);
    border-left: 2px solid var(--tuscan-gold);
    border-radius: 0 4px 4px 0;
    font-size: 0.8rem;
    line-height: 1.45;
  }}
  .early-insight-row {{
    display: flex;
    gap: 0.55rem;
    align-items: flex-start;
  }}
  .early-insight-row + .early-insight-row {{
    margin-top: 0.35rem;
  }}
  .early-insight-label {{
    font-size: 0.65rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 600;
    color: var(--tuscan-gold);
    flex-shrink: 0;
    min-width: 3.8rem;
    padding-top: 0.12rem;
  }}
  .early-insight-text {{
    color: #4a4a4a;
    font-style: normal;
  }}

  /* ── Inline pitch (email ready under each radar news) ── */
  .inline-pitch {{
    margin-top: 1rem;
    padding: 1rem 1.1rem;
    background: linear-gradient(to bottom, rgba(107,143,158,0.08), rgba(107,143,158,0.03));
    border-left: 3px solid var(--pacific-blue);
    border-radius: 0 8px 8px 0;
  }}
  .inline-pitch-header {{
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.6rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px dashed rgba(107,143,158,0.25);
  }}
  .inline-pitch-header .pitch-label {{
    color: var(--pacific-blue);
    font-size: 0.72rem;
    letter-spacing: 0.08em;
    font-weight: 600;
  }}
  .pitch-subject-editor {{
    flex: 1;
    width: 100%;
    font-family: var(--sans);
    font-size: 0.88rem;
    font-weight: 600;
    color: var(--espresso);
    background: #fff;
    border: 1px solid var(--warm-sand);
    border-radius: 4px;
    padding: 0.35rem 0.6rem;
    margin-left: 0.4rem;
  }}
  .pitch-subject-editor:focus {{
    outline: none;
    border-color: var(--pacific-blue);
    box-shadow: 0 0 0 2px rgba(107,143,158,0.15);
  }}
  .count-pill.count-pitch strong {{ color: var(--pacific-blue); }}

  /* ── Reply cards (Sprint 2 — follow-up on outreach) ── */
  .count-pill.count-reply strong {{ color: var(--terracotta); }}
  .count-pill.count-scan {{
    cursor: pointer;
    background: rgba(255,255,255,0.06);
    color: var(--sand-white);
    border: 1px solid rgba(255,255,255,0.2);
    font-family: var(--sans);
    font-size: 0.78rem;
    letter-spacing: 0.04em;
    transition: background 0.15s ease, border-color 0.15s ease;
  }}
  .count-pill.count-scan:hover {{
    background: rgba(255,255,255,0.12);
    border-color: rgba(255,255,255,0.35);
  }}
  .count-pill.count-scan.scanning {{
    opacity: 0.6;
    cursor: wait;
  }}

  .section-replies .section-heading {{ color: var(--terracotta); }}

  /* ── Editorial IG section ───────────────────────────── */
  .section-editorial .section-heading {{ color: var(--olive); }}
  .editorial-toolbar {{
    display: flex;
    gap: 0.5rem;
    align-items: center;
    flex-wrap: wrap;
    margin: 0.5rem 0 1.5rem;
    padding: 0.75rem 1rem;
    background: var(--sand-white);
    border-radius: 6px;
    border-left: 3px solid var(--olive);
  }}
  .editorial-toolbar-hint {{
    font-size: 0.78rem;
    color: #888;
    margin-left: auto;
  }}
  .editorial-toolbar-hint code {{
    background: rgba(0,0,0,0.04);
    padding: 1px 5px;
    border-radius: 3px;
    font-size: 0.72rem;
  }}
  .editorial-week-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin: 1.5rem 0 0.75rem;
    padding: 0.4rem 0.6rem;
    border-bottom: 1px solid var(--warm-sand);
    color: var(--espresso);
  }}
  .editorial-week-label {{
    font-family: var(--serif);
    font-weight: 600;
    font-size: 1.05rem;
    letter-spacing: 0.02em;
  }}
  .editorial-week-count {{
    font-size: 0.7rem;
    color: #999;
    text-transform: uppercase;
    letter-spacing: 0.08em;
  }}
  .editorial-card {{
    border-left: 4px solid var(--olive);
  }}
  .editorial-card .card-status-stripe {{
    background: var(--olive);
  }}
  .editorial-header {{
    flex-wrap: wrap;
    gap: 0.4rem;
    align-items: center;
  }}
  .editorial-when {{
    font-size: 0.72rem;
    color: var(--espresso);
    margin-left: auto;
    font-variant-numeric: tabular-nums;
    letter-spacing: 0.04em;
  }}
  .editorial-subtopic {{
    font-family: var(--serif);
    font-size: 1.15rem;
    font-weight: 600;
    color: var(--espresso);
    margin: 0.5rem 0 0.3rem;
    letter-spacing: 0.01em;
  }}
  .editorial-img-wrap {{
    position: relative;
    margin: 0.75rem 0;
    border-radius: 4px;
    overflow: hidden;
    background: var(--sand-white);
  }}
  .editorial-img {{
    width: 100%;
    max-height: 280px;
    object-fit: cover;
    display: block;
  }}
  .editorial-img-name {{
    position: absolute;
    bottom: 8px;
    right: 8px;
    background: rgba(62,47,43,0.85);
    color: #fff;
    padding: 3px 8px;
    border-radius: 3px;
    font-size: 0.68rem;
    font-family: ui-monospace, monospace;
  }}
  .editorial-partner-link {{
    display: inline-block;
    font-size: 0.78rem;
    font-weight: 500;
    color: var(--pacific-blue);
    text-decoration: none;
    padding: 4px 10px;
    border: 1px solid var(--pacific-blue);
    border-radius: 14px;
    margin: 0.4rem 0;
    transition: all .2s;
  }}
  .editorial-partner-link:hover {{
    background: var(--pacific-blue);
    color: #fff;
  }}

  /* ── Editorial image picker modal ─────────────────────────────── */
  .editorial-img-modal-backdrop {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.6);
    z-index: 800;
    align-items: center;
    justify-content: center;
  }}
  .editorial-img-modal-backdrop.show {{ display: flex; }}
  .editorial-img-modal {{
    background: var(--cream);
    border-radius: 8px;
    width: 92vw;
    max-width: 980px;
    max-height: 88vh;
    overflow-y: auto;
    padding: 28px;
    position: relative;
  }}
  .editorial-img-modal h3 {{
    font-family: var(--serif);
    font-size: 1.4rem;
    color: var(--espresso);
    margin-bottom: 0.4rem;
  }}
  .editorial-img-modal .modal-sub {{
    color: #888;
    font-size: 0.82rem;
    margin-bottom: 1rem;
  }}
  .editorial-img-section {{ margin-bottom: 1.4rem; }}
  .editorial-img-section-label {{
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--olive);
    margin-bottom: 0.5rem;
    padding-bottom: 4px;
    border-bottom: 1px solid var(--warm-sand);
  }}
  .editorial-img-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
    gap: 0.6rem;
  }}
  .editorial-img-tile {{
    cursor: pointer;
    border: 3px solid transparent;
    border-radius: 4px;
    overflow: hidden;
    background: #fff;
    transition: all .2s;
    position: relative;
  }}
  .editorial-img-tile:hover {{ border-color: var(--warm-sand); }}
  .editorial-img-tile.selected {{ border-color: var(--terracotta); }}
  .editorial-img-tile.current::after {{
    content: 'CURRENT';
    position: absolute;
    top: 6px;
    left: 6px;
    background: var(--olive);
    color: #fff;
    font-size: 0.62rem;
    font-weight: 700;
    letter-spacing: 0.06em;
    padding: 2px 6px;
    border-radius: 3px;
  }}
  .editorial-img-tile img {{
    width: 100%;
    height: 130px;
    object-fit: cover;
    display: block;
  }}
  .editorial-img-tile-name {{
    padding: 4px 6px;
    font-size: 0.7rem;
    color: #666;
    font-family: ui-monospace, monospace;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .editorial-img-modal-actions {{
    display: flex;
    justify-content: flex-end;
    gap: 0.6rem;
    margin-top: 1.2rem;
    padding-top: 1rem;
    border-top: 1px solid var(--warm-sand);
  }}
  .post-cover-wrap {{
    position: relative;
  }}
  .post-cover-wrap img {{ cursor: pointer; }}
  .post-expand-btn {{
    position: absolute;
    top: 6px;
    right: 6px;
    background: rgba(62,47,43,0.9);
    color: #fff;
    border: none;
    border-radius: 12px;
    padding: 3px 9px;
    font-size: 0.72rem;
    font-weight: 700;
    cursor: pointer;
    letter-spacing: 0.02em;
    transition: all .2s;
  }}
  .post-expand-btn:hover {{
    background: var(--terracotta);
    transform: scale(1.05);
  }}
  .post-slides-strip {{
    grid-column: 1 / -1;
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
    gap: 0.5rem;
    padding: 0.6rem;
    margin: 0.2rem 0 0.4rem;
    background: rgba(196,162,101,0.08);
    border-left: 3px solid var(--tuscan-gold);
    border-radius: 4px;
  }}
  .post-slides-strip .editorial-img-tile {{
    background: #fff;
  }}
  .post-slides-strip .editorial-img-tile img {{
    height: 100px;
  }}
  .editorial-caption {{
    min-height: 90px;
    font-size: 0.92rem;
    line-height: 1.5;
  }}
  .editorial-hashtags {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin: 0.5rem 0;
  }}
  .editorial-htag {{
    background: var(--sand-white);
    color: var(--olive);
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.72rem;
    font-weight: 500;
  }}
  .editorial-slides {{
    margin: 0.5rem 0;
    padding: 0.5rem 0.75rem;
    background: var(--cream);
    border-radius: 4px;
    border: 1px solid var(--warm-sand);
  }}
  .editorial-slides summary {{
    cursor: pointer;
    font-size: 0.78rem;
    font-weight: 600;
    color: var(--espresso);
    letter-spacing: 0.04em;
  }}
  .editorial-slide {{
    display: flex;
    gap: 0.6rem;
    padding: 0.5rem 0;
    border-bottom: 1px dashed var(--warm-sand);
    align-items: flex-start;
  }}
  .editorial-slide:last-child {{ border-bottom: none; }}
  .editorial-slide-num {{
    flex: 0 0 28px;
    height: 28px;
    border-radius: 50%;
    background: var(--olive);
    color: #fff;
    font-size: 0.78rem;
    font-weight: 700;
    display: flex;
    align-items: center;
    justify-content: center;
  }}
  .editorial-slide-content strong {{
    display: block;
    font-size: 0.78rem;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--espresso);
    margin-bottom: 0.2rem;
  }}
  .editorial-slide-content p {{
    font-size: 0.85rem;
    color: #555;
    line-height: 1.45;
  }}
  .editorial-voice-check {{
    margin-top: 0.5rem;
    padding: 0.5rem 0.75rem;
    background: rgba(108,143,158,0.08);
    border-left: 2px solid var(--pacific-blue);
    font-size: 0.78rem;
    color: #666;
    line-height: 1.4;
    border-radius: 0 3px 3px 0;
  }}
  .editorial-voice-label {{
    font-weight: 600;
    color: var(--pacific-blue);
    text-transform: uppercase;
    font-size: 0.7rem;
    letter-spacing: 0.06em;
    margin-right: 0.3rem;
  }}
  .editorial-warn {{
    background: #fff3cd;
    color: #856404;
    padding: 0.5rem 0.75rem;
    border-radius: 4px;
    border-left: 3px solid #f39c12;
    font-size: 0.82rem;
    margin: 0.5rem 0;
    font-weight: 500;
  }}
  .badge-pillar {{
    background: var(--olive);
    color: #fff;
  }}
  .badge-partner {{
    background: var(--pacific-blue);
    color: #fff;
    font-family: ui-monospace, monospace;
  }}
  .status-draft {{
    background: #ddd;
    color: #555;
  }}
  .status-approved {{
    background: var(--tuscan-gold);
    color: var(--espresso);
  }}
  .status-scheduled {{
    background: var(--pacific-blue);
    color: #fff;
  }}
  .status-published {{
    background: var(--olive);
    color: #fff;
  }}

  .reply-card {{
    border-left: 4px solid var(--terracotta);
  }}
  .reply-card[data-classification="needs_human"] {{
    border-left-color: #c0392b;
    background: linear-gradient(to bottom, rgba(192,57,43,0.04), transparent 120px);
  }}
  .reply-card[data-classification="polite_decline"] {{
    border-left-color: #888;
  }}
  .reply-card .card-body {{ padding: 1.2rem 1.4rem; }}

  .badge-reply-material {{
    background: rgba(92,107,79,0.15);
    color: var(--olive);
    border: 1px solid var(--olive);
  }}
  .badge-reply-call {{
    background: rgba(107,143,158,0.15);
    color: var(--pacific-blue);
    border: 1px solid var(--pacific-blue);
  }}
  .badge-reply-both {{
    background: linear-gradient(135deg, rgba(92,107,79,0.18), rgba(107,143,158,0.18));
    color: var(--espresso);
    border: 1px solid var(--olive);
  }}
  .badge-reply-decline {{
    background: #eee;
    color: #555;
    border: 1px solid #aaa;
  }}
  .badge-reply-human {{
    background: rgba(192,57,43,0.12);
    color: #c0392b;
    border: 1px solid #c0392b;
  }}
  .badge-reply-conf {{
    background: var(--sand-white);
    color: var(--espresso);
    border: 1px solid var(--warm-sand);
    font-family: ui-monospace, monospace;
    letter-spacing: 0;
    text-transform: lowercase;
  }}
  .reply-hint {{
    margin-left: auto;
    font-size: 0.78rem;
    color: #777;
    font-style: italic;
  }}

  .reply-recipient {{
    margin: 0.6rem 0 0.4rem;
    font-size: 0.92rem;
    color: var(--charcoal);
  }}
  .reply-recipient .pitch-label {{ margin-right: 0.35rem; }}
  .reply-outreach-meta {{
    font-size: 0.8rem;
    color: #777;
    margin-bottom: 0.6rem;
  }}
  .reply-outreach-meta em {{ color: var(--espresso); font-style: italic; }}

  .reply-journalist {{
    margin: 0.6rem 0 0.8rem;
    padding: 0.7rem 0.9rem;
    background: rgba(194,113,79,0.05);
    border-left: 3px solid var(--terracotta);
    border-radius: 0 6px 6px 0;
  }}
  .reply-journalist-empty {{
    opacity: 0.65;
    font-style: italic;
  }}
  .reply-journalist summary {{
    cursor: pointer;
    list-style: none;
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
  }}
  .reply-journalist summary::-webkit-details-marker {{ display: none; }}
  .reply-journalist summary::before {{
    content: "▸";
    font-size: 0.7rem;
    color: var(--terracotta);
    transition: transform 0.15s ease;
    display: inline-block;
  }}
  .reply-journalist[open] summary::before {{ transform: rotate(90deg); }}
  .reply-journalist-preview {{
    font-size: 0.85rem;
    color: #555;
    font-style: italic;
    flex: 1 1 280px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .reply-received-chip {{
    font-size: 0.7rem;
    color: #999;
    font-family: ui-monospace, monospace;
    letter-spacing: 0;
  }}
  .reply-journalist-body {{
    margin-top: 0.6rem;
    padding: 0.6rem 0.8rem;
    background: #fff;
    border-radius: 4px;
    border: 1px solid rgba(194,113,79,0.2);
    font-family: var(--sans);
    font-size: 0.85rem;
    color: var(--charcoal);
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 360px;
    overflow-y: auto;
  }}

  .reply-reasoning {{
    font-size: 0.8rem;
    color: #666;
    margin-bottom: 0.7rem;
    padding: 0.4rem 0.7rem;
    background: rgba(0,0,0,0.02);
    border-radius: 4px;
  }}
  .reply-needs-human {{
    margin: 0.6rem 0 0.7rem;
    padding: 0.6rem 0.9rem;
    background: rgba(192,57,43,0.08);
    color: #c0392b;
    border: 1px solid #c0392b;
    border-radius: 6px;
    font-size: 0.88rem;
    font-weight: 500;
  }}
  .reply-needs-human em {{
    display: block;
    margin-top: 0.25rem;
    font-size: 0.82rem;
    color: #8a2a1f;
    font-style: italic;
  }}

  .reply-subject-editor {{
    flex: 1;
    width: 100%;
    font-family: var(--sans);
    font-size: 0.88rem;
    font-weight: 600;
    color: var(--espresso);
    background: #fff;
    border: 1px solid var(--warm-sand);
    border-radius: 4px;
    padding: 0.35rem 0.6rem;
    margin-left: 0.4rem;
  }}
  .reply-subject-editor:focus {{
    outline: none;
    border-color: var(--terracotta);
    box-shadow: 0 0 0 2px rgba(194,113,79,0.15);
  }}
  .reply-body-editor {{
    width: 100%;
    min-height: 170px;
    font-family: var(--sans);
    font-size: 0.9rem;
    color: var(--charcoal);
    background: #fff;
    border: 1px solid var(--warm-sand);
    border-radius: 6px;
    padding: 0.7rem 0.85rem;
    resize: vertical;
    margin-top: 0.4rem;
    line-height: 1.55;
  }}
  .reply-body-editor:focus {{
    outline: none;
    border-color: var(--terracotta);
    box-shadow: 0 0 0 2px rgba(194,113,79,0.15);
  }}

  .reply-attachments {{
    margin-top: 0.8rem;
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 0.7rem;
    font-size: 0.84rem;
  }}
  .reply-attachments .pitch-label {{ margin-right: 0.25rem; }}
  .reply-attach-item {{
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    padding: 0.25rem 0.55rem;
    background: var(--sand-white);
    border: 1px solid var(--warm-sand);
    border-radius: 4px;
    cursor: pointer;
    user-select: none;
  }}
  .reply-attach-item input[type="checkbox"] {{ margin: 0; }}
  .reply-attach-path {{
    font-family: ui-monospace, monospace;
    font-size: 0.7rem;
    color: #888;
    letter-spacing: 0;
  }}

  .btn.btn-send-reply {{
    background: var(--terracotta);
    color: #fff;
    border: 1px solid var(--terracotta);
  }}
  .btn.btn-send-reply:hover {{
    background: #a5593a;
    border-color: #a5593a;
  }}
  .btn.btn-send-reply.sending {{ opacity: 0.7; cursor: wait; }}
  .btn.btn-send-reply.sent {{
    background: #2e7d32;
    border-color: #2e7d32;
  }}
  .btn.btn-send-reply.failed {{
    background: #c0392b;
    border-color: #c0392b;
  }}
  .btn.btn-redraft-reply {{
    background: #fff;
    color: var(--pacific-blue);
    border: 1px solid var(--pacific-blue);
  }}
  .btn.btn-redraft-reply:hover {{
    background: rgba(107,143,158,0.1);
  }}
  .btn.btn-redraft-reply.loading {{ opacity: 0.7; cursor: wait; }}

  /* ── Viral Opportunity cards ─────────────────────── */
  .count-pill.count-viral strong {{ color: #ff4500; }}
  .viral-card .card-body {{ padding: 1.2rem 1.4rem; }}
  .badge-virality {{
    font-family: ui-monospace, monospace;
    letter-spacing: 0;
  }}
  .virality-hot {{
    background: linear-gradient(135deg, #ff4500, #ff8f0e);
    color: #fff;
  }}
  .virality-rising {{
    background: var(--tuscan-gold);
    color: var(--espresso);
  }}
  .virality-warm {{
    background: var(--sand-white);
    color: var(--stone-grey, #666);
    border: 1px solid var(--warm-sand);
  }}

  .viral-metrics {{
    display: flex;
    flex-wrap: wrap;
    gap: 0.8rem;
    margin: 0.7rem 0 0.8rem;
    font-size: 0.78rem;
  }}
  .viral-metric {{
    display: inline-flex;
    align-items: center;
    gap: 0.3rem;
    color: #555;
    background: var(--sand-white);
    padding: 0.25rem 0.6rem;
    border-radius: 100px;
    border: 1px solid var(--warm-sand);
  }}
  .viral-metric-icon {{
    font-size: 0.85rem;
    opacity: 0.7;
  }}
  .viral-metric em {{
    font-style: normal;
    color: #888;
    font-size: 0.72rem;
    margin-left: 0.15rem;
  }}
  .viral-followers {{
    background: rgba(107,143,158,0.12) !important;
    border-color: rgba(107,143,158,0.3) !important;
    color: var(--pacific-blue) !important;
  }}

  .viral-content {{
    font-size: 0.9rem;
    color: #333;
    line-height: 1.55;
    background: #fefdfb;
    padding: 0.9rem 1.1rem;
    border-radius: 6px;
    border-left: 3px solid var(--warm-sand);
    margin: 0.6rem 0 1rem;
  }}

  .viral-reply-wrap {{ margin: 0.8rem 0; }}
  .viral-reply-label {{
    display: flex;
    align-items: center;
    gap: 0.6rem;
    margin-bottom: 0.4rem;
  }}
  .reply-tone {{
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    color: var(--pacific-blue);
    background: rgba(107,143,158,0.1);
    padding: 0.15rem 0.5rem;
    border-radius: 100px;
  }}
  .reply-char-count {{
    margin-left: auto;
    font-size: 0.72rem;
    color: #999;
    font-family: ui-monospace, monospace;
  }}
  .reply-char-count.over-limit {{ color: var(--terracotta); font-weight: 600; }}

  .viral-reply-editor {{
    width: 100%;
    min-height: 70px;
    font-family: var(--sans);
    font-size: 0.9rem;
    color: #222;
    background: #fff8f0;
    border: 1px solid var(--warm-sand);
    border-radius: 6px;
    padding: 0.7rem 0.9rem;
    line-height: 1.5;
    resize: vertical;
  }}
  .viral-reply-editor:focus {{
    outline: none;
    border-color: #ff4500;
    box-shadow: 0 0 0 3px rgba(255,69,0,0.15);
  }}

  .viral-skip {{
    margin: 0.8rem 0;
    padding: 0.7rem 1rem;
    background: rgba(153,153,153,0.08);
    border-radius: 6px;
    color: #666;
    font-size: 0.85rem;
  }}
  .viral-skip strong {{ color: #888; }}

  .btn-viral-reply {{
    background: linear-gradient(135deg, #ff4500, #ff8f0e);
    color: #fff;
  }}
  .btn-viral-reply:hover {{
    box-shadow: 0 2px 10px rgba(255,69,0,0.4);
  }}

  .card-excerpt {{
    font-size: 0.88rem;
    color: #666;
    margin-bottom: 0.8rem;
    line-height: 1.55;
  }}

  .card-post-preview {{
    font-size: 0.88rem;
    color: #444;
    background: var(--sand-white);
    border-radius: 8px;
    padding: 0.8rem 1rem;
    margin-bottom: 0.8rem;
    line-height: 1.55;
    max-height: 120px;
    overflow: hidden;
  }}

  .card-meta {{
    display: flex;
    gap: 1.2rem;
    font-size: 0.75rem;
    color: #999;
    margin-bottom: 1rem;
  }}

  /* ── Buttons ──────────────────────────────────────── */
  .card-actions {{
    display: flex;
    gap: 0.6rem;
    flex-wrap: wrap;
  }}

  .btn {{
    display: inline-block;
    font-family: var(--sans);
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 0.5rem 1.1rem;
    border-radius: 6px;
    border: none;
    cursor: pointer;
    text-decoration: none;
    text-align: center;
    transition: background 0.2s, transform 0.15s, box-shadow 0.2s;
  }}
  .btn:active {{ transform: scale(0.97); }}
  .btn:disabled {{
    opacity: 0.4;
    cursor: not-allowed;
    transform: none !important;
  }}

  .btn-preview {{
    background: var(--sand-white);
    color: var(--espresso);
    border: 1px solid var(--warm-sand);
  }}
  .btn-preview:hover {{ background: var(--warm-sand); }}

  .btn-approve {{
    background: var(--olive);
    color: #fff;
  }}
  .btn-approve:hover {{ background: #4e5d42; box-shadow: 0 2px 8px rgba(92,107,79,0.3); }}

  /* Pubblica IG — gemello di btn-approve ma in rosa-magenta (coerente
     col blocco IG companion). Resta disabilitato finché l'account
     Instagram non è collegato; il :disabled state lo rende grigiastro
     e non-cliccabile, e il tooltip spiega perché. */
  .btn-approve-ig {{
    background: #b03060;
    color: #fff;
  }}
  .btn-approve-ig:hover {{ background: #8e2650; box-shadow: 0 2px 8px rgba(176,48,96,0.3); }}
  .btn-approve-ig:disabled,
  .btn-approve-ig.btn-disabled {{
    background: #d8c8d0;
    color: #fff;
    cursor: not-allowed;
    opacity: 0.7;
    box-shadow: none;
  }}
  .btn-approve-ig:disabled:hover,
  .btn-approve-ig.btn-disabled:hover {{
    background: #d8c8d0;
    box-shadow: none;
  }}

  .btn-reject {{
    background: var(--terracotta);
    color: #fff;
  }}
  .btn-reject:hover {{ background: #a85d3f; box-shadow: 0 2px 8px rgba(194,113,79,0.3); }}

  .channel-sub {{ font-size: 1.0rem; margin: 1.1rem 0 0.5rem; color: #1a1a2e; border-bottom: 1px solid #eee3d2; padding-bottom: 4px; }}
  .more-cards {{ margin: 0.6rem 0 1rem; }}
  .more-cards summary {{ cursor: pointer; color: #8a7a5f; font-size: 0.85rem; padding: 6px 2px; user-select: none; }}
  .more-cards summary:hover {{ color: #1a1a2e; }}
  .social-img-preview img {{ max-width: 280px; max-height: 180px; border-radius: 6px; margin: 6px 0; object-fit: cover; border: 1px solid #e3d9c8; }}
  .social-img-missing {{ font-size: 0.78rem; color: #a85d3f; background: #fdf3ee; border: 1px dashed #d9a08a; border-radius: 5px; padding: 5px 9px; margin: 6px 0; }}
  .btn-publish-social {{
    background: #1a1a2e;
    color: #fff;
  }}
  .btn-publish-social:hover {{ background: #16213e; box-shadow: 0 2px 8px rgba(26,26,46,0.35); }}
  .btn-publish-social[data-platform="instagram"] {{
    background: linear-gradient(135deg, #833AB4, #E1306C, #F77737);
    color: #fff;
  }}
  .btn-publish-social[data-platform="instagram"]:hover {{
    box-shadow: 0 2px 10px rgba(225,48,108,0.4);
  }}

  .btn-copy-open {{
    background: var(--sand-white);
    color: var(--espresso);
    border: 1px solid var(--warm-sand);
  }}
  .btn-copy-open:hover {{ background: var(--warm-sand); }}

  /* ── Publish result banner ────────────────────────── */
  .publish-result {{
    margin-top: 0.6rem;
    padding: 0.5rem 0.8rem;
    border-radius: 6px;
    font-size: 0.78rem;
    display: none;
  }}
  .publish-result.success {{
    display: block;
    background: rgba(92,107,79,0.12);
    color: var(--olive);
  }}
  .publish-result.error {{
    display: block;
    background: rgba(194,113,79,0.12);
    color: var(--terracotta);
  }}
  .publish-result a {{
    color: inherit;
    font-weight: 600;
    text-decoration: underline;
  }}

  /* ── Setup instructions modal ────────────────────── */
  .setup-instructions {{
    white-space: pre-wrap;
    font-size: 0.82rem;
    color: #555;
    line-height: 1.6;
    background: var(--sand-white);
    padding: 1rem;
    border-radius: 8px;
    margin-top: 0.8rem;
    font-family: monospace;
  }}

  /* ── Social platform status bar ──────────────────── */
  .social-platforms-status {{
    display: flex;
    gap: 1rem;
    margin-bottom: 1rem;
    flex-wrap: wrap;
  }}
  .platform-pill {{
    display: inline-flex;
    align-items: center;
    gap: 0.4rem;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.05em;
    text-transform: uppercase;
    padding: 0.35rem 0.85rem;
    border-radius: 100px;
    background: var(--sand-white);
    border: 1px solid var(--warm-sand);
    color: var(--stone-grey);
  }}
  .platform-pill .dot {{
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
  }}
  .platform-pill .dot.green {{ background: var(--olive); }}
  .platform-pill .dot.grey {{ background: #ccc; }}

  /* ── Status label after action ───────────────────── */
  .status-label {{
    display: inline-block;
    font-size: 0.78rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 0.5rem 1.1rem;
    border-radius: 6px;
  }}
  .status-label.approved {{ background: rgba(92,107,79,0.12); color: var(--olive); }}
  .status-label.rejected {{ background: rgba(194,113,79,0.12); color: var(--terracotta); }}

  /* ── Empty state ──────────────────────────────────── */
  .empty-state {{
    text-align: center;
    padding: 4rem 2rem;
  }}
  .empty-icon {{ font-size: 3rem; margin-bottom: 1rem; }}
  .empty-state h2 {{
    font-family: var(--serif);
    font-size: 1.5rem;
    color: var(--espresso);
    margin-bottom: 0.5rem;
  }}
  .empty-state p {{ color: #888; font-size: 0.9rem; }}

  /* ── Footer ───────────────────────────────────────── */
  .footer {{
    text-align: center;
    padding: 2rem;
    font-size: 0.72rem;
    color: #aaa;
    letter-spacing: 0.04em;
  }}

  /* ── Toast notifications ──────────────────────────── */
  .toast {{
    position: fixed;
    bottom: 2rem;
    right: 2rem;
    background: var(--espresso);
    color: var(--sand-white);
    padding: 0.8rem 1.4rem;
    border-radius: 8px;
    font-size: 0.85rem;
    font-weight: 500;
    box-shadow: 0 4px 16px rgba(0,0,0,0.2);
    transform: translateY(120%);
    opacity: 0;
    transition: all 0.3s ease;
    z-index: 1000;
  }}
  .toast.show {{
    transform: translateY(0);
    opacity: 1;
  }}

  /* ── New buttons (edit / opus) ───────────────────── */
  .btn-edit {{
    background: var(--pacific-blue);
    color: #fff;
  }}
  .btn-edit:hover {{ background: #587a88; box-shadow: 0 2px 8px rgba(107,143,158,0.3); }}
  .btn-opus {{
    background: var(--tuscan-gold);
    color: var(--espresso);
  }}
  .btn-opus:hover {{ background: #b08e4e; color:#fff; box-shadow: 0 2px 8px rgba(196,162,101,0.3); }}
  .btn-image {{
    background: #6b5846;
    color: #fff;
  }}
  .btn-image:hover {{ background: #8b7356; box-shadow: 0 2px 8px rgba(107,88,70,0.3); }}

  /* ── Image picker modal ───────────────────── */
  .image-picker-backdrop {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(45, 30, 25, 0.72);
    z-index: 2000;
    align-items: center;
    justify-content: center;
    padding: 2rem;
  }}
  .image-picker-backdrop.show {{ display: flex; }}
  .image-picker-modal {{
    background: #fff;
    border-radius: 10px;
    max-width: 1200px;
    width: 100%;
    max-height: 90vh;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    box-shadow: 0 30px 80px rgba(0,0,0,0.4);
  }}
  .image-picker-header {{
    padding: 1.2rem 1.6rem;
    border-bottom: 1px solid var(--warm-sand);
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
  }}
  .image-picker-header h3 {{
    margin: 0;
    font-family: var(--serif);
    color: var(--espresso);
    font-size: 1.3rem;
  }}
  .image-picker-query {{
    flex: 1;
    margin: 0 1rem;
    padding: 0.6rem 0.9rem;
    border: 1px solid var(--warm-sand);
    border-radius: 6px;
    font-family: var(--sans);
    font-size: 0.9rem;
  }}
  .image-picker-body {{
    padding: 1.2rem 1.6rem;
    overflow-y: auto;
    flex: 1;
  }}
  .image-picker-status {{
    font-size: 0.85rem;
    color: var(--stone-grey);
    margin-bottom: 1rem;
  }}
  .image-picker-grid {{
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 0.9rem;
  }}
  @media (max-width: 900px) {{
    .image-picker-grid {{ grid-template-columns: repeat(2, 1fr); }}
  }}
  .image-candidate {{
    position: relative;
    border: 2px solid transparent;
    border-radius: 6px;
    overflow: hidden;
    cursor: pointer;
    background: var(--sand-white);
    transition: transform 0.18s, border-color 0.18s, box-shadow 0.18s;
  }}
  .image-candidate:hover {{
    transform: translateY(-2px);
    border-color: var(--tuscan-gold);
    box-shadow: 0 8px 20px rgba(0,0,0,0.15);
  }}
  .image-candidate.selected {{
    border-color: var(--pacific-blue);
    box-shadow: 0 0 0 3px rgba(107,143,158,0.25);
  }}
  .image-candidate img {{
    display: block;
    width: 100%;
    height: 160px;
    object-fit: cover;
    background: #eee;
  }}
  .image-candidate-caption {{
    padding: 0.5rem 0.7rem;
    font-size: 0.72rem;
    color: var(--stone-grey);
    letter-spacing: 0.03em;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .image-candidate-badge {{
    position: absolute;
    top: 8px;
    left: 8px;
    padding: 3px 9px;
    border-radius: 100px;
    font-family: var(--sans);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
    text-transform: uppercase;
    color: #fff;
    box-shadow: 0 2px 6px rgba(0,0,0,0.25);
    z-index: 2;
  }}
  .image-candidate-badge.badge-source {{
    background: #b0532b;
  }}
  .image-candidate-badge.badge-unsplash {{
    background: #6b8f9e;
  }}
  .image-picker-section-label {{
    font-family: var(--sans);
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--espresso);
    margin: 1.2rem 0 0.6rem;
    padding-bottom: 0.4rem;
    border-bottom: 1px solid var(--warm-sand);
  }}
  .image-picker-section-label:first-child {{ margin-top: 0; }}
  .image-picker-section-label .hint {{
    font-weight: 400;
    text-transform: none;
    color: var(--stone-grey);
    margin-left: 0.5rem;
  }}
  .image-picker-footer {{
    padding: 1rem 1.6rem;
    border-top: 1px solid var(--warm-sand);
    display: flex;
    justify-content: flex-end;
    gap: 0.8rem;
  }}

  /* ── Editable social textarea ───────────────────── */
  .card-post-editor {{
    width: 100%;
    min-height: 110px;
    font-family: var(--sans);
    font-size: 0.88rem;
    color: #333;
    background: var(--sand-white);
    border: 1px solid var(--warm-sand);
    border-radius: 8px;
    padding: 0.8rem 1rem;
    margin-bottom: 0.8rem;
    line-height: 1.55;
    resize: vertical;
  }}
  .card-post-editor:focus {{
    outline: none;
    border-color: var(--terracotta);
    background: #fff;
  }}

  /* ── Modal ──────────────────────────────────────── */
  .modal-backdrop {{
    position: fixed;
    inset: 0;
    background: rgba(62,47,43,0.55);
    display: none;
    align-items: flex-start;
    justify-content: center;
    padding: 3rem 1.2rem;
    z-index: 2000;
    overflow-y: auto;
  }}
  .modal-backdrop.show {{ display: flex; }}
  .modal {{
    background: var(--cream);
    border-radius: 14px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.35);
    max-width: 560px;
    width: 100%;
    padding: 1.8rem 1.8rem 1.6rem;
    animation: modalIn 0.22s ease;
  }}
  .modal.modal-lg {{ max-width: 820px; }}
  @keyframes modalIn {{
    from {{ transform: translateY(14px); opacity: 0; }}
    to   {{ transform: translateY(0); opacity: 1; }}
  }}
  .modal-title {{
    font-family: var(--serif);
    font-size: 1.55rem;
    color: var(--espresso);
    margin-bottom: 0.3rem;
    font-weight: 600;
  }}
  .modal-subtitle {{
    font-size: 0.8rem;
    color: #888;
    margin-bottom: 1.2rem;
    letter-spacing: 0.02em;
  }}
  .modal label {{
    display: block;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--espresso);
    margin: 0.9rem 0 0.35rem;
  }}
  .modal input[type="text"], .modal textarea {{
    width: 100%;
    font-family: var(--sans);
    font-size: 0.9rem;
    color: #333;
    background: #fff;
    border: 1px solid var(--warm-sand);
    border-radius: 6px;
    padding: 0.6rem 0.8rem;
    line-height: 1.5;
    resize: vertical;
  }}
  .modal textarea {{ min-height: 90px; }}
  .modal input:focus, .modal textarea:focus {{
    outline: none;
    border-color: var(--terracotta);
  }}
  .modal .modal-body-scroll {{
    max-height: 60vh;
    overflow-y: auto;
    padding-right: 0.4rem;
  }}
  .modal-section {{
    border-top: 1px dashed var(--warm-sand);
    margin-top: 1rem;
    padding-top: 0.6rem;
  }}
  .modal-actions {{
    display: flex;
    gap: 0.6rem;
    justify-content: flex-end;
    margin-top: 1.4rem;
    flex-wrap: wrap;
  }}
  .btn-primary {{
    background: var(--terracotta);
    color: #fff;
  }}
  .btn-primary:hover {{ background: #a85d3f; }}
  .btn-secondary {{
    background: transparent;
    color: var(--espresso);
    border: 1px solid var(--warm-sand);
  }}
  .btn-secondary:hover {{ background: var(--sand-white); }}

  /* Revision compare */
  .rev-compare {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-top: 1rem;
  }}
  @media (max-width: 720px) {{ .rev-compare {{ grid-template-columns: 1fr; }} }}
  .rev-pane {{
    background: #fff;
    border: 1px solid var(--warm-sand);
    border-radius: 8px;
    padding: 0.8rem 0.9rem;
    max-height: 320px;
    overflow-y: auto;
    font-size: 0.82rem;
    line-height: 1.5;
    white-space: pre-wrap;
    color: #333;
  }}
  .rev-pane-label {{
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--espresso);
    margin-bottom: 0.35rem;
  }}

  /* ── API Health banner — injected from api_health_banner.CSS_LIGHT ─── */
  {API_HEALTH_CSS}

  /* Spinner */
  .spinner {{
    display: inline-block;
    width: 14px;
    height: 14px;
    border: 2px solid rgba(255,255,255,0.35);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    vertical-align: -2px;
    margin-right: 6px;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>

{api_health_banner}

<div class="header">
  <h1>My <span>Villa</span> &mdash; Content Review</h1>
  <div class="subtitle">{today}</div>
  <div class="counts">
    {f'<div class="count-pill count-reply"><strong>{len(reply_drafts)}</strong>&ensp;↩️ Replies</div>' if reply_drafts else ''}
    {f'<div class="count-pill count-viral"><strong>{len(radar["viral"])}</strong>&ensp;🔥 Viral</div>' if radar and radar.get("viral") else ''}
    {f'<div class="count-pill"><strong>{len(radar["news"])}</strong>&ensp;📰 News</div>' if radar and radar.get("news") else ''}
    {f'<div class="count-pill count-pitch"><strong>{len(radar["emails"])}</strong>&ensp;✉️ Email Ready</div>' if radar and radar.get("emails") else ''}
    {f'<div class="count-pill count-early"><strong>{len(radar["early"])}</strong>&ensp;🌱 Early</div>' if radar and radar.get("early") else ''}
    <div class="count-pill"><strong>{len(journal)}</strong>&ensp;📝 Articles</div>
    <div class="count-pill"><strong>{len(social)}</strong>&ensp;📱 Social</div>
    <button class="count-pill count-scan" onclick="scanRepliesNow(this)" title="Poll Gmail for new journalist replies and re-draft">🔄 Scan replies</button>
  </div>
  {f'<div class="radar-meta">Latest radar: <strong>{radar["date"]}</strong> · <a href="/radar">Open full dashboard</a></div>' if radar else ''}
</div>

<div class="main">
  {empty_msg}

  {f'''
  <div class="section">
    <h2 class="section-heading">📱 Social — da approvare<span class="section-count">{len(social)}</span></h2>
    <div class="social-platforms-status" id="social-platforms-status"></div>
    <p class="section-subtitle"><strong>Il flusso quotidiano:</strong> "✅ Approva → coda" mette il post in coda di pubblicazione automatica (Instagram: 1 al giorno; X: 1 al giorno — cap configurabili). "🚀 Pubblica subito" salta la coda e posta ora via API. I companion degli articoli hanno già la hero; i post reattivi Instagram richiedono una immagine.</p>
    {social_cards}
  </div>
  ''' if social else '''
  <div class="section">
    <h2 class="section-heading">📱 Social — da approvare<span class="section-count">0</span></h2>
    <p class="section-subtitle">Nessuna proposta in attesa. I generatori (radar reattivo, companion articoli, partner) ne produrranno con la prossima pipeline.</p>
  </div>
  '''}

  {ig_queue_strip}

  {f'''
  <div class="section">
    <h2 class="section-heading">🔥 Commenti ai post virali<span class="section-count">{len(radar["viral"])}</span></h2>
    <p class="section-subtitle">Post ad alto engagement dove un commento My Villa aggiunge valore. <strong>Flusso assistito</strong> (le API Meta non permettono di commentare post altrui): 📋 copia il testo → apri il post → incolla → poi "Scarta" per segnarlo come fatto.</p>
    {viral_cards}
  </div>
  ''' if radar and radar.get("viral") else ''}

  {f'''
  <div class="section section-replies">
    <h2 class="section-heading">↩️ Risposte giornalisti<span class="section-count">{len(reply_drafts)}</span></h2>
    <p class="section-subtitle">Giornalisti che hanno risposto — UNICA parte email che resta manuale (il resto è automatico). Rivedi e invia: press kit o call con Paolo.</p>
    {reply_cards}
  </div>
  ''' if reply_drafts else ''}

  {f'''
  <div class="section section-collapsed" data-section="radar-news">
    <h2 class="section-heading" onclick="toggleSection(this)">
      📰 Radar News (informativo)<span class="section-count">{len(radar["news"])}</span>
      <span class="collapse-toggle">▸ expand</span>
    </h2>
    <p class="section-subtitle">Le opportunità qualificate di oggi. <strong>I pitch email partono in automatico</strong> (cap 20/giorno) — questa sezione serve solo per consultazione o invii manuali extra.</p>
    <div class="section-body">
      {news_cards}
    </div>
  </div>
  ''' if radar and radar.get("news") else ''}

  {f'''
  <div class="section section-collapsed" data-section="early">
    <h2 class="section-heading" onclick="toggleSection(this)">
      🌱 Early Signals<span class="section-count">{len(radar["early"])}</span>
      <span class="collapse-toggle">▸ expand</span>
    </h2>
    <p class="section-subtitle">On-topic posts with low engagement — worth watching, not worth replying yet.</p>
    <div class="section-body">
      {early_cards}
    </div>
  </div>
  ''' if radar and radar.get("early") else ''}

  {f'''
  <div class="section section-editorial section-collapsed" data-section="partner">
    <h2 class="section-heading" onclick="toggleSection(this)">🤝 Partner reposts (Instagram) <span class="section-count">{len(editorial)}</span> <span class="collapse-toggle">▸ expand</span></h2>
    <p class="section-subtitle">
      Event-driven model (since 2026-05-04): we no longer plan an editorial calendar
      for IG. Instead we react to two triggers:
      <strong>(1)</strong> when we publish a journal article, an Instagram companion
      is generated inline on the article card (above) and approved with the same
      "Pubblica" click;
      <strong>(2)</strong> when a relevant partner publishes something, we draft a
      repost — those are the cards listed here. Institutional content
      (vision / archetype / system) is now handled by the human editorial team.
    </p>
    <div class="editorial-toolbar">
      <button class="btn btn-edit" onclick="planEditorialMonth(this)">📝 Plan next month</button>
      <button class="btn btn-edit" onclick="generateEditorialBatch(this)">⚙ Generate next 14 days</button>
      <span class="editorial-toolbar-hint">CLI alternatives: <code>editorial_planner.py --month YYYY-MM</code> · <code>editorial_generator.py --month YYYY-MM</code></span>
    </div>
    {editorial_cards}
  </div>
  ''' if editorial else f'''
  <div class="section section-editorial section-collapsed" data-section="editorial-empty">
    <h2 class="section-heading" onclick="toggleSection(this)">
      📆 Editorial Calendar (Instagram)<span class="section-count">0</span>
      <span class="collapse-toggle">▸ expand</span>
    </h2>
    <div class="section-body">
      <p class="section-subtitle">
        No editorial drafts yet. Generate a calendar with:
        <code>python3 _system/scripts/editorial_planner.py --month $(date -v+1m +%Y-%m)</code>,
        then drafts with <code>editorial_generator.py --month YYYY-MM</code>.
      </p>
      <div class="editorial-toolbar">
        <button class="btn btn-edit" onclick="planEditorialMonth(this)">📝 Plan next month</button>
        <button class="btn btn-edit" onclick="generateEditorialBatch(this)">⚙ Generate next 14 days</button>
      </div>
    </div>
  </div>
  '''}

  {"" if total == 0 else f'''
  <div class="section">
    <h2 class="section-heading">📝 Journal Articles<span class="section-count">{len(journal)}</span></h2>
    {journal_cards if journal else '<p style="color:#999; font-size:0.9rem;">No journal drafts pending.</p>'}
  </div>

  <div class="section">
    <h2 class="section-heading" onclick="togglePublishedSection(this)" style="cursor:pointer; user-select:none;">📚 Published Articles<span class="section-count">{len(published)}</span><span id="published-toggle-icon" style="float:right; font-size:0.7em; color:#999;">▾ click per espandere</span></h2>
    <div id="published-list" style="display:none;">
      <p class="section-subtitle" style="margin-bottom: 1rem;">Articoli già live su myvilla.la. Click "↗ Live" per vedere la pagina, "✏ Modifica" per editare inline, "🗑 Rimuovi" per togliere dal sito (sposta in <code>_archive/blog/journal/</code>).</p>
      {published_rows if published else '<p style="color:#999; font-size:0.9rem;">No published articles yet.</p>'}
    </div>
  </div>

  '''}
</div>

<div class="footer">
  Generated {ts} &middot; My Villa Review Server
</div>

<!-- ── Modals ─────────────────────────────────────── -->

<!-- Editorial image picker modal — for swapping a draft's image -->
<div class="editorial-img-modal-backdrop" id="editorial-img-modal">
  <div class="editorial-img-modal">
    <h3>Choose image for this post</h3>
    <div class="modal-sub" id="editorial-img-modal-sub"></div>
    <div id="editorial-img-modal-body">
      <!-- sections injected here -->
    </div>
    <div class="editorial-img-modal-actions">
      <button class="btn btn-reject" onclick="closeEditorialImagePicker()">Cancel</button>
      <button class="btn btn-edit" id="editorial-img-modal-confirm"
              onclick="confirmEditorialImagePick()" disabled>Use selected image</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="edit-modal">
  <div class="modal modal-lg">
    <h3 class="modal-title">Modifica articolo</h3>
    <div class="modal-subtitle" id="edit-modal-file"></div>
    <div class="modal-body-scroll" id="edit-modal-body">
      <!-- fields injected here -->
    </div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="closeModal('edit-modal')">Annulla</button>
      <button class="btn btn-primary" id="edit-save-btn" onclick="saveJournalEdit()">Salva</button>
    </div>
  </div>
</div>

<div class="modal-backdrop" id="revise-modal">
  <div class="modal">
    <h3 class="modal-title">Chiedi a Opus di revisionare</h3>
    <div class="modal-subtitle" id="revise-modal-file"></div>
    <label for="revise-feedback">Cosa vuoi cambiare?</label>
    <textarea id="revise-feedback" rows="4" placeholder="Es: più corto, tono meno formale, aggiungi un esempio concreto..."></textarea>
    <div id="revise-compare-wrap" style="display:none;">
      <div class="rev-compare">
        <div>
          <div class="rev-pane-label">Versione attuale</div>
          <div class="rev-pane" id="rev-current"></div>
        </div>
        <div>
          <div class="rev-pane-label">Proposta di Opus</div>
          <div class="rev-pane" id="rev-proposed"></div>
        </div>
      </div>
    </div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="closeModal('revise-modal')">Annulla</button>
      <button class="btn btn-secondary" id="revise-discard-btn" onclick="discardRevision()" style="display:none;">Scarta revisione</button>
      <button class="btn btn-primary" id="revise-accept-btn" onclick="acceptRevision()" style="display:none;">Accetta e chiudi</button>
      <button class="btn btn-primary" id="revise-send-btn" onclick="sendRevise()">Invia a Opus</button>
    </div>
  </div>
</div>

<!-- Image Picker Modal -->
<div class="image-picker-backdrop" id="image-picker-modal">
  <div class="image-picker-modal">
    <div class="image-picker-header">
      <h3>Scegli immagine hero</h3>
      <input type="text" class="image-picker-query" id="image-picker-query" placeholder="Query Unsplash (es. Los Angeles luxury villa concrete)">
      <button class="btn btn-primary" id="image-picker-search-btn" onclick="searchImages()">Cerca</button>
    </div>
    <div class="image-picker-body">
      <div class="image-picker-status" id="image-picker-status">Scrivi una query e clicca Cerca.</div>
      <div id="image-picker-grid"></div>
    </div>
    <div class="image-picker-footer">
      <button class="btn btn-secondary" onclick="closeModal('image-picker-modal')">Annulla</button>
      <button class="btn btn-primary" id="image-picker-confirm-btn" onclick="confirmImageSelection()" disabled>Usa questa immagine</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
function showToast(msg) {{
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), 3000);
}}

function doAction(action, file, type, btn) {{
  const card = btn.closest('.card');
  const buttons = card.querySelectorAll('.btn');
  buttons.forEach(b => b.disabled = true);

  fetch('/' + action, {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{file: file, type: type}})
  }})
  .then(r => r.json())
  .then(data => {{
    if (data.ok) {{
      const cls = action === 'approve' ? 'approved' : 'rejected';
      card.classList.add(cls);

      const actionsDiv = card.querySelector('.card-actions');
      const label = document.createElement('span');
      label.className = 'status-label ' + cls;
      label.textContent = action === 'approve' ? 'Approved \u2713' : 'Discarded';
      actionsDiv.innerHTML = '';
      actionsDiv.appendChild(label);

      showToast(data.message);
    }} else {{
      buttons.forEach(b => b.disabled = false);
      showToast('Error: ' + (data.error || 'Unknown error'));
    }}
  }})
  .catch(err => {{
    buttons.forEach(b => b.disabled = false);
    showToast('Network error: ' + err.message);
  }});
}}

/* ── Social: publish directly to X/Instagram ───── */
function publishSocial(file, platform, btn) {{
  const card = btn.closest('.card');
  const textarea = card.querySelector('.card-post-editor');
  const text = textarea.value.trim();
  if (!text) {{ showToast('Post is empty'); return; }}

  const origHTML = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Publishing...';

  // Remove existing result banners
  card.querySelectorAll('.publish-result').forEach(el => el.remove());

  fetch('/api/publish_social', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      file: file,
      type: 'social',
      platform: platform,
      text: text,
    }})
  }})
  .then(r => r.json())
  .then(data => {{
    btn.disabled = false;
    btn.innerHTML = origHTML;

    const resultDiv = document.createElement('div');
    resultDiv.className = 'publish-result';

    if (data.ok) {{
      resultDiv.className += ' success';
      let msg = data.message || 'Published successfully!';
      if (data.url) msg += ' <a href="' + data.url + '" target="_blank">View post &rarr;</a>';
      resultDiv.innerHTML = msg;
      // Mark card as published
      card.classList.add('approved');
      const actionsDiv = card.querySelector('.card-actions');
      const label = document.createElement('span');
      label.className = 'status-label approved';
      label.textContent = 'Published \u2713';
      actionsDiv.innerHTML = '';
      actionsDiv.appendChild(label);
    }} else if (data.needs_setup) {{
      resultDiv.className += ' error';
      resultDiv.innerHTML = data.error +
        '<div class="setup-instructions">' + (data.setup_instructions || '') + '</div>';
    }} else {{
      resultDiv.className += ' error';
      resultDiv.textContent = data.error || 'Unknown error';
    }}

    card.querySelector('.card-body').appendChild(resultDiv);
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.innerHTML = origHTML;
    showToast('Network error: ' + err.message);
  }});
}}

/* ── Social: copy text + open platform ─────────── */
function copyAndOpen(platform, btn) {{
  const card = btn.closest('.card');
  const textarea = card.querySelector('.card-post-editor');
  const text = textarea.value.trim();

  navigator.clipboard.writeText(text).then(() => {{
    showToast('Copied to clipboard!');

    if (platform === 'x' || platform === 'twitter') {{
      // Open X compose with text pre-filled (URL encoded)
      const encoded = encodeURIComponent(text);
      window.open('https://x.com/intent/tweet?text=' + encoded, '_blank');
    }} else if (platform === 'instagram' || platform === 'ig') {{
      // Instagram doesn't support pre-filled compose, just open it
      window.open('https://www.instagram.com/', '_blank');
      showToast('Caption copied! Paste into Instagram.');
    }}
  }}).catch(() => {{
    // Fallback: select the textarea
    textarea.select();
    document.execCommand('copy');
    showToast('Copied! Open ' + platform + ' to paste.');
  }});
}}

/* ── IG companion (inside journal cards) ──────────
   New event-driven model: when we publish a journal article we also
   announce it on Instagram. The caption is generated on demand from
   the journal sidecar (article title + excerpt + sources) and
   editable inline before "Pubblica" approves both at once.
*/
function _findIGCompanionEditor(file) {{
  return document.querySelector(`.ig-companion-editor[data-companion-for="${{file}}"]`);
}}

function _replaceIGEmptyWithEditor(file, body, btnEmptyBlock) {{
  // Swap the "📷 Genera Instagram companion" empty state for the editor
  // so a freshly-generated caption is immediately editable + persisted
  // via the auto-save on input.
  const card = btnEmptyBlock.closest('.card');
  const oldBlock = btnEmptyBlock.closest('.ig-companion-block');
  if (!card || !oldBlock) return;
  const html =
    '<div class="ig-companion-block">' +
      '<div class="ig-companion-head">' +
        '<span class="ig-companion-label">📷 Instagram companion (sotto l\\'articolo, approvato qui)</span>' +
        '<button class="btn btn-mini" onclick="regenerateIGCompanion(\\'' + file + '\\', this)" ' +
                'title="Re-genera con Anthropic (sovrascrive il testo qui sotto)">↻ Rigenera</button>' +
      '</div>' +
      '<textarea class="ig-companion-editor" data-companion-for="' + file + '" oninput="updateIGCompanionCount(this)"></textarea>' +
      '<div class="ig-companion-foot">' +
        '<span class="ig-companion-charcount">0 chars</span>' +
        '<span class="ig-companion-hint">Pubblica: il caption viene salvato e l\\'IG va in <code>_system/social/posts/approved/</code></span>' +
      '</div>' +
    '</div>';
  oldBlock.outerHTML = html;
  const ta = _findIGCompanionEditor(file);
  if (ta) {{
    ta.value = body;
    updateIGCompanionCount(ta);
  }}
}}

function generateIGCompanion(file, btn) {{
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>Generating…';
  fetch('/api/generate-ig-companion', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{file: file}})
  }})
  .then(r => r.json())
  .then(resp => {{
    if (resp.ok) {{
      _replaceIGEmptyWithEditor(file, resp.body || '', btn);
      showToast('IG companion generated. Edit inline before "Pubblica".');
    }} else {{
      btn.disabled = false;
      btn.innerHTML = orig;
      showToast('IG generate failed: ' + (resp.error || 'unknown'));
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.innerHTML = orig;
    showToast('IG generate network error: ' + err.message);
  }});
}}

function regenerateIGCompanion(file, btn) {{
  const ta = _findIGCompanionEditor(file);
  if (ta && ta.value.trim() && !confirm('Sovrascrivere il testo attuale con una nuova generazione AI?')) {{
    return;
  }}
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>↻';
  fetch('/api/generate-ig-companion', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{file: file, force: true}})
  }})
  .then(r => r.json())
  .then(resp => {{
    btn.disabled = false;
    btn.innerHTML = orig;
    if (resp.ok) {{
      if (ta) {{
        ta.value = resp.body || '';
        updateIGCompanionCount(ta);
      }}
      showToast('IG companion rigenerato.');
    }} else {{
      showToast('Rigenerazione fallita: ' + (resp.error || 'unknown'));
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.innerHTML = orig;
    showToast('IG rigenerazione network error: ' + err.message);
  }});
}}

function updateIGCompanionCount(ta) {{
  const block = ta.closest('.ig-companion-block');
  if (!block) return;
  const count = ta.value.length;
  const el = block.querySelector('.ig-companion-charcount');
  if (el) {{
    el.textContent = count + ' chars';
    el.classList.toggle('over-limit', count > 2200);  /* IG hard cap */
  }}
  /* Debounced save: 700ms after the last keystroke we persist the edit
     to the .ig.md file so a refresh keeps it. */
  clearTimeout(ta._igSaveTimer);
  ta._igSaveTimer = setTimeout(() => {{
    const file = ta.getAttribute('data-companion-for');
    fetch('/api/save-ig-companion', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{file: file, body: ta.value}})
    }}).catch(() => {{}});  /* silent — visible save state is the textarea itself */
  }}, 700);
}}

/* ── Section: expand/collapse (for Early Signals) ─ */
function toggleSection(heading) {{
  const section = heading.closest('.section');
  if (!section) return;
  section.classList.toggle('section-collapsed');
  const toggle = section.querySelector('.collapse-toggle');
  if (toggle) {{
    toggle.textContent = section.classList.contains('section-collapsed') ? '▸ expand' : '▾ collapse';
  }}
}}

/* ── Viral Opportunity: live char counter ─────── */
function updateViralCharCount(textarea) {{
  const card = textarea.closest('.card');
  const counter = card.querySelector('.reply-char-count');
  if (!counter) return;
  const len = textarea.value.length;
  counter.textContent = len + ' chars';
  const overLimit = len > 280;
  counter.classList.toggle('over-limit', overLimit);
}}

/* ── Viral: reply on X (opens compose window) ──── */
function openViralReplyX(btn) {{
  const card = btn.closest('.card');
  const textarea = card.querySelector('.viral-reply-editor');
  if (!textarea) return;
  const text = textarea.value.trim();
  if (!text) {{ showToast('Reply is empty'); return; }}

  // Look up the original tweet URL (to extract the reply target id)
  const openLink = card.querySelector('.btn-copy-open');
  if (!openLink || !openLink.href) {{
    showToast('Original tweet URL missing');
    return;
  }}
  const m = openLink.href.match(/\\/status\\/(\\d+)/);
  if (!m) {{
    showToast('Could not parse tweet id from URL');
    window.open(openLink.href, '_blank');
    return;
  }}
  const tweetId = m[1];
  const url = 'https://x.com/intent/tweet?in_reply_to=' + tweetId +
              '&text=' + encodeURIComponent(text);
  window.open(url, '_blank');
  showToast('Opening X reply compose...');
}}

/* ── Viral: copy reply text ────────────────────── */
function publishRedditComment(btn) {{
  const card = btn.closest('.card');
  const textarea = card.querySelector('.viral-reply-editor');
  if (!textarea || !textarea.value.trim()) {{ showToast('Commento vuoto'); return; }}
  if (!confirm('Pubblicare questo commento su Reddit con l\'account My Villa?')) return;
  const buttons = card.querySelectorAll('.btn');
  buttons.forEach(b => b.disabled = true);
  btn.textContent = '⏳ Pubblico…';
  fetch('/api/reddit-comment', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{url: btn.dataset.url, text: textarea.value}})
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      btn.textContent = '✓ Pubblicato';
      showToast(d.message || 'Commento live su Reddit');
      if (d.comment_url) {{
        const link = document.createElement('a');
        link.href = d.comment_url; link.target = '_blank';
        link.textContent = '↗ vedi commento';
        link.className = 'btn btn-copy-open';
        btn.after(link);
      }}
      card.style.opacity = '0.55';
    }} else {{
      buttons.forEach(b => b.disabled = false);
      btn.textContent = '🚀 Commenta su Reddit';
      alert(d.needs_setup
        ? 'Credenziali Reddit non configurate.\n\nSetup (3 min): reddit.com/prefs/apps → create app (script) → poi in .env:\nREDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USERNAME, REDDIT_PASSWORD\n\nGuida completa: _system/docs/reddit_setup.md'
        : 'Errore: ' + (d.error || 'sconosciuto'));
    }}
  }})
  .catch(e => {{
    buttons.forEach(b => b.disabled = false);
    btn.textContent = '🚀 Commenta su Reddit';
    alert('Errore di rete: ' + e);
  }});
}}

function copyViralReply(btn) {{
  const card = btn.closest('.card');
  const textarea = card.querySelector('.viral-reply-editor');
  if (!textarea) return;
  navigator.clipboard.writeText(textarea.value).then(() => {{
    const orig = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => {{ btn.textContent = orig; }}, 1600);
    showToast('Reply copied to clipboard');
  }}).catch(() => {{
    textarea.select();
    document.execCommand('copy');
    showToast('Copied!');
  }});
}}

/* ── Published Articles: toggle the collapsable section ─── */
function togglePublishedSection(heading) {{
  const list = document.getElementById('published-list');
  const icon = document.getElementById('published-toggle-icon');
  if (!list) return;
  const isHidden = list.style.display === 'none';
  list.style.display = isHidden ? '' : 'none';
  if (icon) icon.textContent = isHidden ? '▴ click per chiudere' : '▾ click per espandere';
}}

/* ── Published Articles: remove from the live site ─────────
   Two-click confirmation gate (irreversible-feeling action even
   though the file is just moved to _archive/blog/journal/).
   Server rebuilds the indices and auto-pushes; in ~60s the page
   is gone from myvilla.la.
*/
function unpublishArticle(file, btn) {{
  if (!confirm('Rimuovere QUESTO articolo dal sito myvilla.la?\\n\\nIl file viene spostato in _archive/blog/journal/ e il sito si aggiorna in ~60 secondi.\\n\\nFile: ' + file)) {{
    return;
  }}
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>Rimuovendo...';
  fetch('/api/unpublish', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{file: file, type: 'journal'}})
  }})
  .then(r => r.json())
  .then(resp => {{
    if (resp.ok) {{
      const row = document.getElementById('published-' + file);
      if (row) {{
        row.style.transition = 'opacity 0.3s, max-height 0.3s';
        row.style.opacity = '0';
        row.style.maxHeight = '0';
        setTimeout(() => {{ if (row.parentNode) row.parentNode.removeChild(row); }}, 320);
      }}
      showToast('Rimosso dal sito. Sito aggiornato tra ~60s.');
    }} else {{
      btn.disabled = false;
      btn.innerHTML = orig;
      showToast('Rimozione fallita: ' + (resp.error || 'unknown'));
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.innerHTML = orig;
    showToast('Errore di rete: ' + err.message);
  }});
}}

/* ── Viral: skip (hide card locally) ───────────── */
function skipViral(btn) {{
  const card = btn.closest('.card');
  card.style.opacity = '0.35';
  card.style.transition = 'opacity 0.3s';
  const actions = card.querySelector('.card-actions');
  if (actions) {{
    actions.innerHTML = '<span class="status-label rejected">Skipped</span>';
  }}
  showToast('Skipped — reload page to restore');
}}

/* ── Radar News: permanent dismiss ──────────────
   Tells the backend to add the URL to previously_reported.json so this
   item never appears again on any future radar scan. The card fades
   out and is removed from the DOM, and a refresh confirms the
   dismissal (the server filters dismissed URLs at render time).
*/
function dismissRadarItem(url, btn) {{
  if (!url) return;
  if (!confirm('Discard this item? It will no longer appear in this radar nor in any future radar scan.')) {{
    return;
  }}
  btn.disabled = true;
  const orig = btn.innerHTML;
  btn.innerHTML = '<span class="spinner"></span>Discarding...';
  fetch('/api/dismiss-radar-item', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{url: url}})
  }})
  .then(r => r.json())
  .then(resp => {{
    if (resp.ok) {{
      const card = btn.closest('.news-card');
      if (card) {{
        card.style.transition = 'opacity 0.3s, transform 0.3s';
        card.style.opacity = '0';
        card.style.transform = 'scale(0.98)';
        setTimeout(() => {{ if (card.parentNode) card.parentNode.removeChild(card); }}, 320);
      }}
      showToast('Discarded — will not reappear.');
    }} else {{
      btn.disabled = false;
      btn.innerHTML = orig;
      showToast('Discard failed: ' + (resp.error || 'unknown'));
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.innerHTML = orig;
    showToast('Network error: ' + err.message);
  }});
}}

/* ── Journalist Pitch: copy email body (edited) ── */
function copyPitch(btn) {{
  const card = btn.closest('.card');
  const textarea = card.querySelector('.pitch-body-editor');
  if (!textarea) return;
  const text = textarea.value;
  navigator.clipboard.writeText(text).then(() => {{
    showToast('Email body copied to clipboard');
    const orig = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => {{ btn.textContent = orig; }}, 1600);
  }}).catch(() => {{
    textarea.select();
    document.execCommand('copy');
    showToast('Copied!');
  }});
}}

/* ── Journalist Pitch: copy just the email address ── */
function copyAddr(email, btn) {{
  navigator.clipboard.writeText(email).then(() => {{
    showToast('Email address copied: ' + email);
    const orig = btn.textContent;
    btn.textContent = 'Copied ✓';
    setTimeout(() => {{ btn.textContent = orig; }}, 1600);
  }});
}}

/* ── Extra attachments state ─────────────────────────
   Files added by the user via the `+ Add file` picker are kept in a
   WeakMap keyed by the card element, so we don't have to stuff megabytes
   of base64 into the DOM. The sendOutreachEmail / sendReply functions
   read from this map when building the request body.

   Limits mirror the server side (see _persist_uploaded_attachments in
   approve.py): 10 MB per file, 18 MB total per send. */
const EXTRA_ATTACHMENTS = new WeakMap();
const MAX_ATTACH_BYTES_PER_FILE = 10 * 1024 * 1024;
const MAX_ATTACH_BYTES_TOTAL = 18 * 1024 * 1024;

function _getCardAttachments(card) {{
  let arr = EXTRA_ATTACHMENTS.get(card);
  if (!arr) {{ arr = []; EXTRA_ATTACHMENTS.set(card, arr); }}
  return arr;
}}

function _fmtSize(bytes) {{
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / 1024 / 1024).toFixed(1) + ' MB';
}}

function _renderAttachList(card) {{
  const list = card.querySelector('.extra-attach-list');
  if (!list) return;
  const items = _getCardAttachments(card);
  list.innerHTML = '';
  items.forEach((att, idx) => {{
    const li = document.createElement('li');
    li.className = 'extra-attach-item';
    const name = document.createElement('span');
    name.className = 'fname';
    name.textContent = att.filename;
    const size = document.createElement('span');
    size.className = 'fsize';
    size.textContent = _fmtSize(att.size);
    const rm = document.createElement('button');
    rm.className = 'fremove';
    rm.type = 'button';
    rm.title = 'Remove attachment';
    rm.textContent = '✕';
    rm.onclick = () => {{ items.splice(idx, 1); _renderAttachList(card); }};
    li.appendChild(name); li.appendChild(size); li.appendChild(rm);
    list.appendChild(li);
  }});
}}

/* ── + Add file: read files, base64-encode, push into card's list ── */
function addExtraAttachment(inputEl) {{
  const card = inputEl.closest('.card');
  if (!card) return;
  const items = _getCardAttachments(card);
  const files = Array.from(inputEl.files || []);
  if (!files.length) return;

  /* Pre-check total size incl. already-attached to stop early and
     give a useful message (instead of letting the server reject). */
  let totalBytes = items.reduce((s, a) => s + a.size, 0);
  const rejected = [];
  const accepted = [];

  files.forEach(f => {{
    if (f.size > MAX_ATTACH_BYTES_PER_FILE) {{
      rejected.push(f.name + ' (>10MB)');
      return;
    }}
    if (totalBytes + f.size > MAX_ATTACH_BYTES_TOTAL) {{
      rejected.push(f.name + ' (would exceed 18MB total)');
      return;
    }}
    totalBytes += f.size;
    accepted.push(f);
  }});

  if (rejected.length) {{
    showToast('Skipped: ' + rejected.join(', '));
  }}

  let pending = accepted.length;
  if (!pending) {{ inputEl.value = ''; return; }}

  accepted.forEach(f => {{
    const reader = new FileReader();
    reader.onload = () => {{
      /* readAsDataURL returns "data:<mime>;base64,<payload>" — strip the
         prefix so the server gets just the base64 payload. */
      const raw = String(reader.result || '');
      const comma = raw.indexOf(',');
      const b64 = comma >= 0 ? raw.slice(comma + 1) : raw;
      items.push({{
        filename: f.name,
        mime: f.type || 'application/octet-stream',
        size: f.size,
        content_base64: b64,
      }});
      pending -= 1;
      if (pending === 0) {{
        _renderAttachList(card);
        inputEl.value = '';  /* allow re-adding same file after a remove */
      }}
    }};
    reader.onerror = () => {{
      showToast('Could not read file: ' + f.name);
      pending -= 1;
      if (pending === 0) {{ _renderAttachList(card); inputEl.value = ''; }}
    }};
    reader.readAsDataURL(f);
  }});
}}

/* ── Journalist Pitch: send email via Gmail API ───── */
/* Wired to the "Send now" button. Reads the current (possibly edited)
   subject + body from the inline editors, then POSTs to /api/send-email.
   Respects config.yml `confirm_before_send` (client-side) and `dry_run`
   (server-side — the server returns reason='dry_run' if active). */
function sendOutreachEmail(btn, email, mode) {{
  const card = btn.closest('.card');
  const subjectEl = card.querySelector('.pitch-subject-editor');
  const bodyEl = card.querySelector('.pitch-body-editor');
  if (!subjectEl || !bodyEl) {{
    showToast('Could not find subject/body in this card');
    return;
  }}
  const subject = subjectEl.value.trim();
  const body = bodyEl.value.trim();
  if (!subject || !body) {{
    showToast('Subject and body cannot be empty');
    return;
  }}
  /* Two-click confirmation gate — the config.yml flag only changes copy,
     the UI always asks once to avoid accidental sends.
     `mode` can be 'apollo_likely' (extra explicit confirmation that the
     address is not fully verified) or undefined (normal flow). */
  let confirmMsg;
  if (mode === 'apollo_likely') {{
    confirmMsg = (
      '🟡 Apollo likely match — confirm before send.\\n\\n' +
      'Apollo returned this address but marked it as LIKELY, not verified.\\n' +
      'Double-check the name + publication match the journalist you intended.\\n\\n' +
      'To: ' + email + '\\n' +
      'Subject: ' + subject
    );
  }} else {{
    confirmMsg = (
      'Send this email now?\\n\\n' +
      'To: ' + email + '\\n' +
      'Subject: ' + subject + '\\n\\n' +
      'Body preview: ' + body.slice(0, 160) + (body.length > 160 ? '...' : '')
    );
  }}
  if (!confirm(confirmMsg)) return;

  btn.classList.remove('sent', 'failed');
  btn.classList.add('sending');
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.innerHTML = '<span class="spinner"></span>Sending...';

  /* Include any user-added attachments. The confirm above already
     mentioned "To/Subject/Body" — if attachments exist, show a second
     confirm specifically about them so the user knows files are going
     out (and which ones). This is the "policy: niente allegati nella
     prima mail" nudge, surfaced at send-time. */
  const attachments = _getCardAttachments(card);
  if (attachments.length > 0) {{
    const list = attachments.map(a => '  • ' + a.filename + ' (' + _fmtSize(a.size) + ')').join('\\n');
    if (!confirm('⚠️ Attaching ' + attachments.length + ' file(s):\\n\\n' + list + '\\n\\nPolicy: first-touch emails should usually have NO attachments. Continue?')) {{
      btn.classList.remove('sending');
      btn.disabled = false;
      btn.textContent = origLabel;
      return;
    }}
  }}

  /* Pull the article URL out of the card header (.news-title a) so the
     backend can persist it as "user_dismissed" in previously_reported.json.
     This prevents future radar scans from re-suggesting the same article
     after we've already sent a pitch on it — guarding against
     double-sends to journalists. Missing URL is OK; backend treats it
     as optional. */
  let sourceUrl = '';
  try {{
    const link = card.querySelector('.news-title a, .card-title a');
    sourceUrl = link ? link.getAttribute('href') || '' : '';
  }} catch (e) {{ /* ignore — extraction is best-effort */ }}

  fetch('/api/send-email', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      to: email,
      subject: subject,
      body: body,
      uploaded_attachments: attachments,
      source_url: sourceUrl,
    }})
  }})
  .then(r => r.json().then(data => ({{status: r.status, data: data}})))
  .then(({{status, data}}) => {{
    btn.classList.remove('sending');
    btn.disabled = false;
    if (data.ok) {{
      btn.classList.add('sent');
      if (data.reason === 'dry_run') {{
        btn.textContent = 'Dry-run ✓';
        showToast('Dry-run OK — no real send. Flip config.yml dry_run:false to send for real.');
      }} else {{
        btn.textContent = 'Sent ✓';
        const dismissedNote = (data.dismissed_source_url
          ? ' — article will not reappear in future radar scans'
          : '');
        showToast('Email sent to ' + email + ' (msg ' + (data.message_id || '?') + ')' + dismissedNote);
        /* Fade the card out after a real send (not dry-run). The
           backend has already persisted the source_url so a refresh
           confirms the dismissal anyway, but the visual feedback is
           important: the operator sees the card disappear and knows
           the radar won't propose it again. */
        if (data.dismissed_source_url) {{
          setTimeout(() => {{
            card.style.transition = 'opacity 0.4s, transform 0.4s';
            card.style.opacity = '0';
            card.style.transform = 'scale(0.98)';
            setTimeout(() => {{ if (card.parentNode) card.parentNode.removeChild(card); }}, 420);
          }}, 1200);
        }}
      }}
      setTimeout(() => {{
        btn.classList.remove('sent');
        btn.textContent = origLabel;
      }}, 4000);
    }} else {{
      btn.classList.add('failed');
      btn.textContent = 'Failed';
      const err = data.error || data.reason || ('HTTP ' + status);
      showToast('Send failed: ' + err);
      setTimeout(() => {{
        btn.classList.remove('failed');
        btn.textContent = origLabel;
      }}, 6000);
    }}
  }})
  .catch(err => {{
    btn.classList.remove('sending');
    btn.classList.add('failed');
    btn.disabled = false;
    btn.textContent = 'Failed';
    showToast('Send failed: ' + err.message);
    setTimeout(() => {{
      btn.classList.remove('failed');
      btn.textContent = origLabel;
    }}, 6000);
  }});
}}

/* ── Risky-send override ──────────────────────────
   Called from the "⚠️ Send anyway" button on cards whose email_source
   is `opus` or `pattern_guess` (both of which have ~50-60% bounce rates
   based on historical data). Forces a two-step confirmation that cites
   the risk, then delegates to sendOutreachEmail for the actual POST.
   The idea is: make the user pause and think before sending to an
   unverified address, but don't stop them if they're sure. */
function sendRiskyOutreach(btn, email, source) {{
  const srcLabel = source === 'pattern_guess'
    ? 'pattern guess (firstname.lastname@domain)'
    : source === 'opus'
      ? 'LLM-inferred (Claude guessed it)'
      : source || 'unknown';
  const msg = (
    '⚠️ UNVERIFIED EMAIL ADDRESS ⚠️\\n\\n' +
    'To: ' + email + '\\n' +
    'Source: ' + srcLabel + '\\n\\n' +
    'Historical data shows ~50-60% of these bounce. Bouncing hurts your\\n' +
    'domain reputation (future real emails may land in spam).\\n\\n' +
    'Proceed anyway?'
  );
  if (!confirm(msg)) return;
  /* Second step: degrade the button visually, then call the normal
     send flow which will do its own confirm. */
  btn.classList.add('btn-send-email');
  btn.classList.remove('btn-send-risky');
  sendOutreachEmail(btn, email);
}}

/* ── Reply follow-up (Sprint 2) ─────────────────── */
/* Helpers that read the current (edited) state of a reply card and
   drive the /api/send-reply, /api/redraft-reply, /api/dismiss-reply,
   /api/scan-replies endpoints. Kept parallel to sendOutreachEmail so
   the mental model is the same for the operator. */

function _collectReplyCard(card) {{
  const subjectEl = card.querySelector('.reply-subject-editor');
  const bodyEl = card.querySelector('.reply-body-editor');
  const subject = subjectEl ? subjectEl.value.trim() : '';
  const body = bodyEl ? bodyEl.value.trim() : '';
  const attachments = Array.from(
    card.querySelectorAll('.reply-attach-toggle:checked')
  ).map(i => i.getAttribute('data-path'));
  const uploadedAttachments = _getCardAttachments(card);
  return {{
    thread_id: card.getAttribute('data-thread-id') || '',
    to: card.getAttribute('data-to') || '',
    in_reply_to: card.getAttribute('data-in-reply-to') || '',
    references: card.getAttribute('data-references') || '',
    subject: subject,
    body: body,
    attachments: attachments,
    uploaded_attachments: uploadedAttachments,
  }};
}}

function sendReplyEmail(btn) {{
  const card = btn.closest('.reply-card');
  if (!card) return;
  const p = _collectReplyCard(card);
  if (!p.thread_id || !p.to) {{
    showToast('Missing thread or recipient on this card');
    return;
  }}
  if (!p.subject || !p.body) {{
    showToast('Subject and body cannot be empty');
    return;
  }}
  const totalAttach = (p.attachments?.length || 0) + (p.uploaded_attachments?.length || 0);
  const attachParts = [];
  if (p.attachments?.length) attachParts.push(p.attachments.length + ' canonical');
  if (p.uploaded_attachments?.length) {{
    attachParts.push(
      p.uploaded_attachments.length + ' uploaded (' +
      p.uploaded_attachments.map(a => a.filename).join(', ') + ')'
    );
  }}
  const attachSummary = totalAttach
    ? ' (' + attachParts.join(', ') + ')'
    : ' (no attachments)';
  const confirmMsg = (
    'Send this reply now?\\n\\n' +
    'To: ' + p.to + '\\n' +
    'Subject: ' + p.subject + '\\n' +
    attachSummary + '\\n\\n' +
    'Body preview: ' + p.body.slice(0, 160) +
    (p.body.length > 160 ? '...' : '')
  );
  if (!confirm(confirmMsg)) return;

  btn.classList.remove('sent', 'failed');
  btn.classList.add('sending');
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.innerHTML = '<span class="spinner"></span>Sending...';

  fetch('/api/send-reply', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(p)
  }})
  .then(r => r.json().then(data => ({{status: r.status, data: data}})))
  .then(({{status, data}}) => {{
    btn.classList.remove('sending');
    btn.disabled = false;
    if (data.ok) {{
      btn.classList.add('sent');
      if (data.reason === 'dry_run') {{
        btn.textContent = 'Dry-run ✓';
        showToast('Dry-run OK — flip config.yml dry_run:false to send for real.');
      }} else {{
        btn.textContent = 'Sent ✓';
        showToast('Reply sent to ' + p.to + ' (msg ' + (data.message_id || '?') + ')');
        /* Fade the card out — the backend archived the draft. */
        setTimeout(() => {{
          card.style.transition = 'opacity 0.4s ease';
          card.style.opacity = '0.35';
        }}, 600);
      }}
      setTimeout(() => {{
        btn.classList.remove('sent');
        btn.textContent = origLabel;
      }}, 4000);
    }} else {{
      btn.classList.add('failed');
      btn.textContent = 'Failed';
      const err = data.error || data.reason || ('HTTP ' + status);
      showToast('Send failed: ' + err);
      setTimeout(() => {{
        btn.classList.remove('failed');
        btn.textContent = origLabel;
      }}, 6000);
    }}
  }})
  .catch(err => {{
    btn.classList.remove('sending');
    btn.classList.add('failed');
    btn.disabled = false;
    btn.textContent = 'Failed';
    showToast('Send failed: ' + err.message);
    setTimeout(() => {{
      btn.classList.remove('failed');
      btn.textContent = origLabel;
    }}, 6000);
  }});
}}

function redraftReply(btn) {{
  const card = btn.closest('.reply-card');
  if (!card) return;
  const threadId = card.getAttribute('data-thread-id') || '';
  if (!threadId) {{
    showToast('Missing thread id on this card');
    return;
  }}
  if (!confirm('Re-run the drafter for this thread? Your edits will be overwritten.')) {{
    return;
  }}
  const origLabel = btn.textContent;
  btn.classList.add('loading');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Drafting...';

  fetch('/api/redraft-reply', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{thread_id: threadId}})
  }})
  .then(r => r.json().then(data => ({{status: r.status, data: data}})))
  .then(({{status, data}}) => {{
    btn.classList.remove('loading');
    btn.disabled = false;
    btn.textContent = origLabel;
    if (data.ok) {{
      showToast('New draft generated — reloading page');
      setTimeout(() => window.location.reload(), 700);
    }} else {{
      const err = data.error || data.stderr || ('HTTP ' + status);
      showToast('Re-draft failed: ' + err);
    }}
  }})
  .catch(err => {{
    btn.classList.remove('loading');
    btn.disabled = false;
    btn.textContent = origLabel;
    showToast('Re-draft failed: ' + err.message);
  }});
}}

function dismissReply(btn) {{
  const card = btn.closest('.reply-card');
  if (!card) return;
  const threadId = card.getAttribute('data-thread-id') || '';
  if (!threadId) {{
    showToast('Missing thread id on this card');
    return;
  }}
  if (!confirm('Dismiss this draft? It will be moved to _dismissed/ (reversible).')) {{
    return;
  }}
  const origLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Dismissing...';

  fetch('/api/dismiss-reply', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{thread_id: threadId}})
  }})
  .then(r => r.json().then(data => ({{status: r.status, data: data}})))
  .then(({{status, data}}) => {{
    btn.disabled = false;
    btn.textContent = origLabel;
    if (data.ok) {{
      card.style.transition = 'opacity 0.3s ease';
      card.style.opacity = '0';
      setTimeout(() => {{ card.style.display = 'none'; }}, 350);
      showToast('Draft dismissed');
    }} else {{
      const err = data.error || ('HTTP ' + status);
      showToast('Dismiss failed: ' + err);
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = origLabel;
    showToast('Dismiss failed: ' + err.message);
  }});
}}

function scanRepliesNow(btn) {{
  if (btn.classList.contains('scanning')) return;
  btn.classList.add('scanning');
  const origLabel = btn.textContent;
  btn.textContent = '🔄 Scanning...';

  fetch('/api/scan-replies', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{with_drafts: true}})
  }})
  .then(r => r.json().then(data => ({{status: r.status, data: data}})))
  .then(({{status, data}}) => {{
    btn.classList.remove('scanning');
    btn.textContent = origLabel;
    if (data.ok) {{
      showToast('Scan complete — reloading');
      setTimeout(() => window.location.reload(), 600);
    }} else {{
      const err = data.error || data.stderr || ('HTTP ' + status);
      showToast('Scan failed: ' + err);
    }}
  }})
  .catch(err => {{
    btn.classList.remove('scanning');
    btn.textContent = origLabel;
    showToast('Scan failed: ' + err.message);
  }});
}}

/* ── Modal helpers ─────────────────────────────── */
function openModal(id) {{
  document.getElementById(id).classList.add('show');
}}
function closeModal(id) {{
  document.getElementById(id).classList.remove('show');
}}

/* ── Social live char count ────────────────────── */
function updateSocialCharCount(textarea) {{
  const card = textarea.closest('.card');
  const counter = card.querySelector('.char-count-live');
  if (counter) counter.textContent = textarea.value.length;
}}

/* ── Social: save edit ─────────────────────────── */
function saveSocialDraft(file, btn) {{
  const card = btn.closest('.card');
  const textarea = card.querySelector('.card-post-editor');
  const newBody = textarea.value;
  const origLabel = btn.textContent;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Saving...';

  fetch('/api/save_draft', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{file: file, type: 'social', data: {{body: newBody}}}})
  }})
  .then(r => r.json())
  .then(data => {{
    btn.disabled = false;
    btn.textContent = origLabel;
    if (data.ok) {{
      showToast('Changes saved');
      const counter = card.querySelector('.char-count-live');
      if (counter) counter.textContent = newBody.length;
    }} else {{
      showToast('Error: ' + (data.error || 'Unknown'));
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = origLabel;
    showToast('Network error: ' + err.message);
  }});
}}

/* ── Journal: edit modal ───────────────────────── */
let currentJournalEdit = {{file: null, data: null, card: null}};

function openJournalEdit(file, btn) {{
  const card = btn.closest('.card');
  currentJournalEdit.file = file;
  currentJournalEdit.card = card;
  document.getElementById('edit-modal-file').textContent = file;
  const body = document.getElementById('edit-modal-body');
  body.innerHTML = '<p style="color:#888; font-size:0.85rem;">Caricamento...</p>';
  openModal('edit-modal');

  fetch('/api/get_draft', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{file: file, type: 'journal'}})
  }})
  .then(r => r.json())
  .then(resp => {{
    if (!resp.ok) {{
      body.innerHTML = '<p style="color:#c2714f;">Errore: ' + (resp.error || 'Unknown') + '</p>';
      return;
    }}
    currentJournalEdit.data = resp.data;
    renderJournalEditForm(resp.data);
  }})
  .catch(err => {{
    body.innerHTML = '<p style="color:#c2714f;">Errore di rete: ' + err.message + '</p>';
  }});
}}

function renderJournalEditForm(data) {{
  const body = document.getElementById('edit-modal-body');
  let html = '';
  html += '<label>Titolo</label><input type="text" id="edit-title" value="' + escAttr(data.title || '') + '">';
  html += '<label>Excerpt</label><textarea id="edit-excerpt" rows="2">' + escText(data.excerpt || '') + '</textarea>';
  html += '<label>Meta description</label><textarea id="edit-meta-desc" rows="2">' + escText(data.meta_description || '') + '</textarea>';
  html += '<label>Paragrafo di apertura</label><textarea id="edit-opening" rows="4">' + escText(data.opening_paragraph || '') + '</textarea>';
  const sections = data.sections || [];
  sections.forEach((s, i) => {{
    html += '<div class="modal-section">';
    html += '<label>Sezione ' + (i+1) + ' - titolo</label>';
    html += '<input type="text" class="edit-sec-heading" data-idx="' + i + '" value="' + escAttr(s.heading || '') + '">';
    html += '<label>Sezione ' + (i+1) + ' - paragrafi (uno per riga vuota)</label>';
    const paras = (s.paragraphs || []).join('\\n\\n');
    html += '<textarea class="edit-sec-paras" data-idx="' + i + '" rows="5">' + escText(paras) + '</textarea>';
    html += '</div>';
  }});
  html += '<div class="modal-section">';
  html += '<label>La nostra prospettiva</label>';
  html += '<textarea id="edit-perspective" rows="4">' + escText(data.our_perspective || '') + '</textarea>';
  html += '</div>';
  body.innerHTML = html;
}}

function collectJournalEdit() {{
  const d = Object.assign({{}}, currentJournalEdit.data);
  d.title = document.getElementById('edit-title').value;
  d.excerpt = document.getElementById('edit-excerpt').value;
  d.meta_description = document.getElementById('edit-meta-desc').value;
  d.opening_paragraph = document.getElementById('edit-opening').value;
  d.our_perspective = document.getElementById('edit-perspective').value;
  const sections = (d.sections || []).map((s, i) => Object.assign({{}}, s));
  document.querySelectorAll('.edit-sec-heading').forEach(inp => {{
    const i = parseInt(inp.dataset.idx);
    if (sections[i]) sections[i].heading = inp.value;
  }});
  document.querySelectorAll('.edit-sec-paras').forEach(ta => {{
    const i = parseInt(ta.dataset.idx);
    if (sections[i]) {{
      sections[i].paragraphs = ta.value.split(/\\n\\s*\\n/).map(p => p.trim()).filter(Boolean);
    }}
  }});
  d.sections = sections;
  return d;
}}

function saveJournalEdit() {{
  const data = collectJournalEdit();
  const btn = document.getElementById('edit-save-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Salvo...';

  fetch('/api/save_draft', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{file: currentJournalEdit.file, type: 'journal', data: data}})
  }})
  .then(r => r.json())
  .then(resp => {{
    btn.disabled = false;
    btn.textContent = 'Salva';
    if (resp.ok) {{
      showToast('Articolo salvato e ri-renderizzato');
      // Update card preview
      const card = currentJournalEdit.card;
      if (card) {{
        const titleEl = card.querySelector('[data-field="title"]');
        if (titleEl) titleEl.textContent = data.title || '';
        const excerptEl = card.querySelector('[data-field="excerpt"]');
        if (excerptEl) excerptEl.textContent = data.excerpt || '';
      }}
      currentJournalEdit.data = data;
      closeModal('edit-modal');
    }} else {{
      showToast('Errore: ' + (resp.error || 'Unknown'));
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Salva';
    showToast('Errore di rete: ' + err.message);
  }});
}}

/* ── Revise modal (journal + social) ───────────── */
let currentRevise = {{
  file: null, type: null, card: null,
  originalContent: null, currentContent: null, proposedContent: null,
  history: []
}};

function openReviseModal(file, type, btn) {{
  const card = btn.closest('.card');
  currentRevise.file = file;
  currentRevise.type = type;
  currentRevise.card = card;
  currentRevise.history = [];
  currentRevise.proposedContent = null;

  document.getElementById('revise-modal-file').textContent = file;
  document.getElementById('revise-feedback').value = '';
  document.getElementById('revise-compare-wrap').style.display = 'none';
  document.getElementById('revise-accept-btn').style.display = 'none';
  document.getElementById('revise-discard-btn').style.display = 'none';
  document.getElementById('revise-send-btn').textContent = 'Invia a Opus';

  // Load current content
  if (type === 'social') {{
    const textarea = card.querySelector('.card-post-editor');
    currentRevise.originalContent = textarea ? textarea.value : '';
    currentRevise.currentContent = currentRevise.originalContent;
    openModal('revise-modal');
  }} else {{
    // journal: fetch json
    fetch('/api/get_draft', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{file: file, type: 'journal'}})
    }})
    .then(r => r.json())
    .then(resp => {{
      if (!resp.ok) {{
        showToast('Errore: ' + (resp.error || 'Unknown'));
        return;
      }}
      currentRevise.originalContent = resp.data;
      currentRevise.currentContent = resp.data;
      openModal('revise-modal');
    }})
    .catch(err => showToast('Errore di rete: ' + err.message));
  }}
}}

function reviseSummary(content, type) {{
  if (type === 'social') return content || '';
  // journal: show title + opening + first section heading
  if (!content) return '';
  let out = '';
  if (content.title) out += content.title + '\\n\\n';
  if (content.excerpt) out += content.excerpt + '\\n\\n';
  if (content.opening_paragraph) out += content.opening_paragraph + '\\n\\n';
  (content.sections || []).forEach(s => {{
    if (s.heading) out += '## ' + s.heading + '\\n';
    (s.paragraphs || []).forEach(p => out += p + '\\n\\n');
  }});
  if (content.our_perspective) out += '---\\n' + content.our_perspective;
  return out;
}}

function sendRevise() {{
  const feedback = document.getElementById('revise-feedback').value.trim();
  if (!feedback) {{
    showToast('Scrivi prima il feedback');
    return;
  }}
  const btn = document.getElementById('revise-send-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Opus sta revisionando...';

  fetch('/api/revise', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      file: currentRevise.file,
      type: currentRevise.type,
      feedback: feedback,
      content: currentRevise.currentContent
    }})
  }})
  .then(r => r.json())
  .then(resp => {{
    btn.disabled = false;
    if (!resp.ok) {{
      btn.textContent = 'Invia a Opus';
      showToast('Errore: ' + (resp.error || 'Unknown'));
      return;
    }}
    currentRevise.proposedContent = resp.revised;
    document.getElementById('rev-current').textContent = reviseSummary(currentRevise.currentContent, currentRevise.type);
    document.getElementById('rev-proposed').textContent = reviseSummary(resp.revised, currentRevise.type);
    document.getElementById('revise-compare-wrap').style.display = 'block';
    document.getElementById('revise-accept-btn').style.display = 'inline-block';
    document.getElementById('revise-discard-btn').style.display = 'inline-block';
    btn.textContent = 'Revisiona ancora';
    document.getElementById('revise-feedback').value = '';
    document.getElementById('revise-feedback').placeholder = 'Altre modifiche? Scrivi qui e clicca Revisiona ancora...';
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Invia a Opus';
    showToast('Errore di rete: ' + err.message);
  }});
}}

function discardRevision() {{
  // Roll back to previous content in history (if any)
  if (currentRevise.history.length > 0) {{
    currentRevise.currentContent = currentRevise.history.pop();
  }} else {{
    currentRevise.currentContent = currentRevise.originalContent;
  }}
  currentRevise.proposedContent = null;
  document.getElementById('revise-compare-wrap').style.display = 'none';
  document.getElementById('revise-accept-btn').style.display = 'none';
  document.getElementById('revise-discard-btn').style.display = 'none';
  document.getElementById('revise-send-btn').textContent = 'Invia a Opus';
  showToast('Revisione scartata');
}}

function acceptRevision() {{
  if (!currentRevise.proposedContent) {{
    closeModal('revise-modal');
    return;
  }}
  // push current to history, promote proposed
  currentRevise.history.push(currentRevise.currentContent);
  currentRevise.currentContent = currentRevise.proposedContent;
  currentRevise.proposedContent = null;

  // Save to disk
  const btn = document.getElementById('revise-accept-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Salvo...';

  let payload;
  if (currentRevise.type === 'social') {{
    payload = {{file: currentRevise.file, type: 'social', data: {{body: currentRevise.currentContent}}}};
  }} else {{
    payload = {{file: currentRevise.file, type: 'journal', data: currentRevise.currentContent}};
  }}

  fetch('/api/save_draft', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify(payload)
  }})
  .then(r => r.json())
  .then(resp => {{
    btn.disabled = false;
    btn.textContent = 'Accetta e chiudi';
    if (resp.ok) {{
      showToast('Revisione salvata');
      // Update card preview
      const card = currentRevise.card;
      if (currentRevise.type === 'social') {{
        const ta = card.querySelector('.card-post-editor');
        if (ta) {{
          ta.value = currentRevise.currentContent;
          updateSocialCharCount(ta);
        }}
      }} else {{
        const d = currentRevise.currentContent;
        const titleEl = card.querySelector('[data-field="title"]');
        if (titleEl && d.title) titleEl.textContent = d.title;
        const excerptEl = card.querySelector('[data-field="excerpt"]');
        if (excerptEl && d.excerpt) excerptEl.textContent = d.excerpt;
      }}
      closeModal('revise-modal');
    }} else {{
      showToast('Errore nel salvare: ' + (resp.error || 'Unknown'));
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Accetta e chiudi';
    showToast('Errore di rete: ' + err.message);
  }});
}}

/* ── small escape helpers for form rendering ───── */
function escAttr(s) {{
  return String(s || '').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}
function escText(s) {{
  return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}}

/* Close modals on backdrop click */
document.querySelectorAll('.modal-backdrop').forEach(bd => {{
  bd.addEventListener('click', (e) => {{
    if (e.target === bd) bd.classList.remove('show');
  }});
}});
document.querySelectorAll('.image-picker-backdrop').forEach(bd => {{
  bd.addEventListener('click', (e) => {{
    if (e.target === bd) bd.classList.remove('show');
  }});
}});

/* ── Image picker ─────────────────────────────── */
let currentImagePicker = {{
  file: null,
  card: null,
  candidates: [],
  selectedIndex: -1,
}};

function openImagePicker(file, btn) {{
  const card = btn.closest('.card');
  currentImagePicker.file = file;
  currentImagePicker.card = card;
  currentImagePicker.candidates = [];
  currentImagePicker.selectedIndex = -1;
  document.getElementById('image-picker-query').value = '';
  document.getElementById('image-picker-grid').innerHTML = '';
  document.getElementById('image-picker-status').textContent =
    'Scrivi una query (o lascia vuoto per usare image_prompt) e clicca Cerca.';
  document.getElementById('image-picker-confirm-btn').disabled = true;
  document.getElementById('image-picker-modal').classList.add('show');
  // Kick off a search immediately using the article's image_prompt
  setTimeout(() => searchImages(), 100);
}}

function searchImages() {{
  if (!currentImagePicker.file) return;
  const q = document.getElementById('image-picker-query').value || '';
  const statusEl = document.getElementById('image-picker-status');
  const gridEl = document.getElementById('image-picker-grid');
  const btn = document.getElementById('image-picker-search-btn');
  statusEl.textContent = 'Ricerca in corso...';
  gridEl.innerHTML = '';
  btn.disabled = true;
  fetch('/api/fetch_images', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      file: currentImagePicker.file,
      type: 'journal',
      query: q,
    }})
  }})
  .then(r => r.json())
  .then(resp => {{
    btn.disabled = false;
    if (!resp.ok) {{
      statusEl.textContent = 'Errore: ' + (resp.error || 'unknown');
      return;
    }}
    currentImagePicker.candidates = resp.candidates || [];
    currentImagePicker.selectedIndex = -1;
    document.getElementById('image-picker-confirm-btn').disabled = true;
    if (currentImagePicker.candidates.length === 0) {{
      statusEl.textContent = 'Nessun risultato. Prova una query diversa.';
      return;
    }}
    statusEl.textContent =
      currentImagePicker.candidates.length + ' risultati per "' + (resp.query_used || q) + '". Clicca per selezionare.';
    renderImageGrid();
  }})
  .catch(err => {{
    btn.disabled = false;
    statusEl.textContent = 'Errore rete: ' + err.message;
  }});
}}

function renderImageGrid() {{
  const container = document.getElementById('image-picker-grid');
  container.innerHTML = '';

  // Split candidates by origin so sources appear first in their own section
  const sources = [];
  const unsplash = [];
  currentImagePicker.candidates.forEach((c, i) => {{
    c.__origIndex = i;
    if ((c.origin || 'unsplash') === 'source') sources.push(c);
    else unsplash.push(c);
  }});

  function buildCard(c) {{
    const div = document.createElement('div');
    div.className = 'image-candidate';
    if (c.__origIndex === currentImagePicker.selectedIndex) div.classList.add('selected');
    div.onclick = () => selectImageCandidate(c.__origIndex);

    const badge = document.createElement('span');
    const isSrc = (c.origin || 'unsplash') === 'source';
    badge.className = 'image-candidate-badge ' + (isSrc ? 'badge-source' : 'badge-unsplash');
    badge.textContent = isSrc ? 'Da fonte' : 'Unsplash';
    div.appendChild(badge);

    const img = document.createElement('img');
    img.src = c.thumb_url || c.full_url || '';
    img.alt = c.alt_description || '';
    img.loading = 'lazy';
    img.referrerPolicy = 'no-referrer';
    img.onerror = () => {{
      img.style.background = '#f5ecdc';
      img.alt = 'Immagine non caricabile (bloccata dalla fonte)';
    }};
    div.appendChild(img);

    const cap = document.createElement('div');
    cap.className = 'image-candidate-caption';
    cap.textContent = isSrc
      ? ((c.source_publication || 'Fonte') + ' — ' + (c.source_title || '').slice(0, 60))
      : ('by ' + (c.author_name || 'Unknown'));
    div.appendChild(cap);
    return div;
  }}

  if (sources.length > 0) {{
    const header = document.createElement('div');
    header.className = 'image-picker-section-label';
    header.innerHTML = 'Dalle fonti citate <span class="hint">(hotlink, pertinenza massima)</span>';
    // Span across the grid by putting it outside, then re-adding the grid wrapper
    container.style.display = 'block';
    container.appendChild(header);
    const subGrid = document.createElement('div');
    subGrid.className = 'image-picker-grid';
    sources.forEach(c => subGrid.appendChild(buildCard(c)));
    container.appendChild(subGrid);
  }}

  if (unsplash.length > 0) {{
    const header = document.createElement('div');
    header.className = 'image-picker-section-label';
    header.innerHTML = 'Da Unsplash <span class="hint">(download + credit)</span>';
    container.appendChild(header);
    const subGrid = document.createElement('div');
    subGrid.className = 'image-picker-grid';
    unsplash.forEach(c => subGrid.appendChild(buildCard(c)));
    container.appendChild(subGrid);
  }}

  if (sources.length === 0 && unsplash.length === 0) {{
    const p = document.createElement('div');
    p.className = 'image-picker-status';
    p.textContent = 'Nessun candidato disponibile.';
    container.appendChild(p);
  }}
}}

function selectImageCandidate(i) {{
  currentImagePicker.selectedIndex = i;
  renderImageGrid();
  document.getElementById('image-picker-confirm-btn').disabled = false;
}}

function confirmImageSelection() {{
  if (currentImagePicker.selectedIndex < 0) return;
  const candidate = currentImagePicker.candidates[currentImagePicker.selectedIndex];
  const btn = document.getElementById('image-picker-confirm-btn');
  const statusEl = document.getElementById('image-picker-status');
  btn.disabled = true;
  statusEl.textContent = 'Download in corso...';
  fetch('/api/select_image', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{
      file: currentImagePicker.file,
      type: 'journal',
      candidate: candidate,
    }})
  }})
  .then(r => r.json())
  .then(resp => {{
    if (!resp.ok) {{
      statusEl.textContent = 'Errore: ' + (resp.error || 'unknown');
      btn.disabled = false;
      return;
    }}
    showToast('Immagine salvata: by ' + (resp.hero_image && resp.hero_image.author_name));
    closeModal('image-picker-modal');
  }})
  .catch(err => {{
    statusEl.textContent = 'Errore rete: ' + err.message;
    btn.disabled = false;
  }});
}}

/* ── Check social credentials on load ──────────── */
(function checkSocialCreds() {{
  const statusEl = document.getElementById('social-platforms-status');
  if (!statusEl) return;
  fetch('/api/social_credentials', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: '{{}}'
  }})
  .then(r => r.json())
  .then(data => {{
    let html = '';
    if (data.x) {{
      const dot = data.x.configured ? 'green' : 'grey';
      const label = data.x.configured ? 'X API Connected' : 'X API Not configured';
      html += '<span class="platform-pill"><span class="dot ' + dot + '"></span>' + label + '</span>';
    }}
    if (data.instagram) {{
      const dot = data.instagram.configured ? 'green' : 'grey';
      const label = data.instagram.configured ? 'Instagram API Connected' : 'Instagram API Not configured';
      html += '<span class="platform-pill"><span class="dot ' + dot + '"></span>' + label + '</span>';
    }}
    statusEl.innerHTML = html;
  }})
  .catch(() => {{}});
}})();

/* ════════════════════════════════════════════════════════════════════
   Editorial IG — handlers
   ════════════════════════════════════════════════════════════════════ */

function updateEditorialCharCount(textarea) {{
  const card = textarea.closest('.editorial-card');
  if (!card) return;
  // Strip the trailing hashtag line (separated by blank line) to count
  // caption-only chars, matching what we store in frontmatter.
  const fullText = textarea.value || '';
  const parts = fullText.trimEnd().split(/\\n\\s*\\n/);
  let captionText = fullText;
  if (parts.length >= 2 && parts[parts.length - 1].trim().startsWith('#')) {{
    captionText = parts.slice(0, -1).join('\\n\\n');
  }}
  const live = card.querySelector('.char-count-live');
  if (live) live.textContent = captionText.length;
}}

function saveEditorialDraft(file, btn) {{
  const card = document.getElementById('card-editorial-' + file);
  if (!card) return;
  const ta = card.querySelector('textarea[data-field="body"]');
  const newBody = ta.value;

  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';

  fetch('/api/editorial/save', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ file: file, body: newBody }})
  }})
  .then(r => r.json())
  .then(d => {{
    btn.disabled = false;
    btn.textContent = d.ok ? '✓ Saved' : 'Failed';
    setTimeout(() => {{ btn.textContent = orig; }}, 1800);
  }})
  .catch(() => {{ btn.disabled = false; btn.textContent = 'Error'; setTimeout(() => {{ btn.textContent = orig; }}, 2400); }});
}}

function regenerateEditorial(file, btn) {{
  if (!confirm('Regenerate caption + hashtags for this slot? Your manual edits will be lost.')) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Generating…';

  fetch('/api/editorial/regenerate', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ file: file }})
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      btn.textContent = '✓ Regenerated';
      setTimeout(() => location.reload(), 800);
    }} else {{
      btn.disabled = false;
      btn.textContent = 'Failed';
      alert('Regenerate failed: ' + (d.error || 'unknown'));
      setTimeout(() => {{ btn.textContent = orig; }}, 2400);
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Error';
    alert('Regenerate error: ' + err.message);
  }});
}}

function buildEditorialPackage(file, btn) {{
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Building…';

  fetch('/api/editorial/build-package', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ file: file }})
  }})
  .then(r => r.json())
  .then(d => {{
    btn.disabled = false;
    if (d.ok) {{
      btn.textContent = '✓ Package built';
      // Try to reveal the package folder in Finder via a server-side hook.
      // Fall back to showing path if unsupported.
      if (d.package_path) {{
        navigator.clipboard.writeText(d.package_path).catch(() => {{}});
        showEditorialToast('Package ready · path copied: ' + d.package_path);
      }}
    }} else {{
      btn.textContent = 'Failed';
      alert('Build failed: ' + (d.error || 'unknown'));
    }}
    setTimeout(() => {{ btn.textContent = orig; }}, 3500);
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Error';
    alert('Build error: ' + err.message);
  }});
}}

function markEditorialPublished(file, btn) {{
  const url = prompt('Instagram post URL (optional, paste from app):', '');
  if (url === null) return;  // cancelled
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Marking…';

  fetch('/api/editorial/mark-published', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ file: file, ig_post_url: url }})
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      btn.textContent = '✓ Published';
      setTimeout(() => location.reload(), 600);
    }} else {{
      btn.disabled = false;
      btn.textContent = 'Failed';
      alert('Mark-published failed: ' + (d.error || 'unknown'));
      setTimeout(() => {{ btn.textContent = orig; }}, 2400);
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Error';
    alert('Mark-published error: ' + err.message);
  }});
}}

function discardEditorial(file, btn) {{
  if (!confirm('Discard this editorial draft? The .md file will be deleted (calendar slot remains).')) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Discarding…';

  fetch('/api/editorial/discard', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ file: file }})
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      const card = document.getElementById('card-editorial-' + file);
      if (card) card.style.opacity = '0.3';
      btn.textContent = '✓ Discarded';
      setTimeout(() => location.reload(), 500);
    }} else {{
      btn.disabled = false;
      btn.textContent = 'Failed';
      setTimeout(() => {{ btn.textContent = orig; }}, 2400);
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Error';
    alert('Discard error: ' + err.message);
  }});
}}

function planEditorialMonth(btn) {{
  const month = prompt('Month to plan (YYYY-MM):', new Date(Date.now() + 30*864e5).toISOString().slice(0, 7));
  if (!month) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Planning…';

  fetch('/api/editorial/plan-month', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ month: month }})
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      btn.textContent = '✓ Planned ' + (d.slot_count || '?') + ' slots';
      setTimeout(() => location.reload(), 1200);
    }} else {{
      btn.disabled = false;
      btn.textContent = 'Failed';
      alert('Plan failed: ' + (d.error || 'unknown'));
      setTimeout(() => {{ btn.textContent = orig; }}, 2400);
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Error';
    alert('Plan error: ' + err.message);
  }});
}}

function generateEditorialBatch(btn) {{
  const month = prompt('Month to generate from (YYYY-MM):', new Date().toISOString().slice(0, 7));
  if (!month) return;
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Generating…';

  fetch('/api/editorial/generate', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{ month: month, days_ahead: 14 }})
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      btn.textContent = '✓ Generated ' + (d.generated || '?');
      setTimeout(() => location.reload(), 1200);
    }} else {{
      btn.disabled = false;
      btn.textContent = 'Failed';
      alert('Generate failed: ' + (d.error || 'unknown'));
      setTimeout(() => {{ btn.textContent = orig; }}, 2400);
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = 'Error';
    alert('Generate error: ' + err.message);
  }});
}}

function showEditorialToast(msg) {{
  let toast = document.getElementById('editorial-toast');
  if (!toast) {{
    toast = document.createElement('div');
    toast.id = 'editorial-toast';
    toast.style.cssText = 'position:fixed;bottom:24px;right:24px;background:#3E2F2B;color:#fff;padding:14px 20px;border-radius:6px;z-index:9999;font-size:.85rem;max-width:480px;box-shadow:0 4px 24px rgba(0,0,0,.3);';
    document.body.appendChild(toast);
  }}
  toast.textContent = msg;
  toast.style.opacity = '1';
  clearTimeout(toast._t);
  toast._t = setTimeout(() => {{ toast.style.opacity = '0'; toast.style.transition = 'opacity .4s'; }}, 4500);
}}

/* ════════════════════════════════════════════════════════════════════
   Editorial image picker modal
   Lists candidates from 3 sources:
     1. Partner-cache thumbnails of the current partner_post (if any)
     2. Whitelisted /img/ assets (real photos / renders, no diagrams)
     3. Recent partner-cache thumbnails across all partners (cross-pollination)
   ════════════════════════════════════════════════════════════════════ */

let _editorialImgPickerState = {{
  file: null,
  selectedWebPath: null,
  selectedFilename: null,
  currentWebPath: null,
}};

function openEditorialImagePicker(file, btn) {{
  _editorialImgPickerState = {{
    file: file, selectedWebPath: null, selectedFilename: null, currentWebPath: null,
  }};
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Loading…';

  fetch('/api/editorial/list-image-candidates?file=' + encodeURIComponent(file))
    .then(r => r.json())
    .then(d => {{
      btn.disabled = false;
      btn.textContent = orig;
      if (!d.ok) {{
        alert('Failed to load candidates: ' + (d.error || 'unknown'));
        return;
      }}
      _editorialImgPickerState.currentWebPath = d.current_web_path || null;
      const body = document.getElementById('editorial-img-modal-body');
      const sub = document.getElementById('editorial-img-modal-sub');
      sub.textContent = file;
      body.innerHTML = '';

      const selectTile = (tile, webPath, filename) => {{
        document.querySelectorAll('.editorial-img-tile.selected')
          .forEach(t => t.classList.remove('selected'));
        tile.classList.add('selected');
        _editorialImgPickerState.selectedWebPath = webPath;
        _editorialImgPickerState.selectedFilename = filename;
        document.getElementById('editorial-img-modal-confirm').disabled = false;
      }};

      const makeTile = (it) => {{
        const tile = document.createElement('div');
        tile.className = 'editorial-img-tile';
        if (it.web_path === d.current_web_path) tile.classList.add('current');
        tile.dataset.webPath = it.web_path;
        tile.dataset.filename = it.filename;
        tile.innerHTML =
          '<img src="' + it.web_path + '" loading="lazy" alt="">' +
          '<div class="editorial-img-tile-name">' + it.filename + '</div>';
        tile.onclick = () => selectTile(tile, it.web_path, it.filename);
        return tile;
      }};

      // Flat sections (Partner post slides + Local assets)
      const renderFlatSection = (label, items, sublabel) => {{
        if (!items || items.length === 0) return;
        const sec = document.createElement('div');
        sec.className = 'editorial-img-section';
        sec.innerHTML =
          '<div class="editorial-img-section-label">' + label +
          ' <span style="color:#999;font-weight:400;letter-spacing:.04em;text-transform:none;font-size:.68rem;">(' +
          items.length + (sublabel ? ' ' + sublabel : '') + ')</span></div>' +
          '<div class="editorial-img-grid"></div>';
        const grid = sec.querySelector('.editorial-img-grid');
        items.forEach(it => grid.appendChild(makeTile(it)));
        body.appendChild(sec);
      }};

      // Grouped section (Other partner posts) — cover with +N expand
      const renderGroupedSection = (label, postGroups) => {{
        if (!postGroups || postGroups.length === 0) return;
        const totalImgs = postGroups.reduce((a, p) =>
          a + 1 + (p.slides ? p.slides.length : 0), 0);
        const sec = document.createElement('div');
        sec.className = 'editorial-img-section';
        sec.innerHTML =
          '<div class="editorial-img-section-label">' + label +
          ' <span style="color:#999;font-weight:400;letter-spacing:.04em;text-transform:none;font-size:.68rem;">(' +
          postGroups.length + ' posts · ' + totalImgs + ' images)</span></div>' +
          '<div class="editorial-img-grid"></div>';
        const grid = sec.querySelector('.editorial-img-grid');

        postGroups.forEach((post, postIdx) => {{
          // Cover tile with "+N" expand badge if post has slides
          const coverWrap = document.createElement('div');
          coverWrap.className = 'editorial-img-tile post-cover-wrap';
          coverWrap.dataset.postIdx = postIdx;
          if (post.cover.web_path === d.current_web_path) coverWrap.classList.add('current');
          coverWrap.dataset.webPath = post.cover.web_path;
          coverWrap.dataset.filename = post.cover.filename;
          const handleLabel = post.handle.replace('_klimaengineering','').replace('__','');
          let badgeHtml = '';
          if (post.slides && post.slides.length > 0) {{
            badgeHtml = '<button class="post-expand-btn" data-post-idx="' + postIdx +
              '" title="Show ' + post.slides.length + ' more slides">+' +
              post.slides.length + '</button>';
          }}
          coverWrap.innerHTML =
            '<img src="' + post.cover.web_path + '" loading="lazy" alt="">' +
            '<div class="editorial-img-tile-name">@' + handleLabel + '</div>' +
            badgeHtml;
          // Cover image click → select cover
          coverWrap.querySelector('img').onclick = (e) => {{
            e.stopPropagation();
            selectTile(coverWrap, post.cover.web_path, post.cover.filename);
          }};
          // Click on the +N button → toggle slide expansion
          const expandBtn = coverWrap.querySelector('.post-expand-btn');
          if (expandBtn) {{
            expandBtn.onclick = (e) => {{
              e.stopPropagation();
              const stripId = 'slide-strip-' + postIdx;
              let strip = document.getElementById(stripId);
              if (strip) {{
                strip.remove();
                expandBtn.textContent = '+' + post.slides.length;
                return;
              }}
              strip = document.createElement('div');
              strip.id = stripId;
              strip.className = 'post-slides-strip';
              post.slides.forEach(s => {{
                const tile = makeTile(s);
                strip.appendChild(tile);
              }});
              // Insert strip RIGHT AFTER the cover (so it spans grid)
              coverWrap.insertAdjacentElement('afterend', strip);
              expandBtn.textContent = '−';
            }};
          }}
          grid.appendChild(coverWrap);
        }});

        body.appendChild(sec);
      }};

      renderFlatSection('Partner post slides',
                        d.partner_post_thumbnails || [],
                        "this post's carousel");
      renderGroupedSection('Other partner posts (cross-pollination)',
                           d.partner_other_posts || []);
      renderFlatSection('My Villa renders / photos', d.local_assets || []);

      document.getElementById('editorial-img-modal').classList.add('show');
    }})
    .catch(err => {{
      btn.disabled = false;
      btn.textContent = orig;
      alert('Picker error: ' + err.message);
    }});
}}

function closeEditorialImagePicker() {{
  document.getElementById('editorial-img-modal').classList.remove('show');
  document.getElementById('editorial-img-modal-confirm').disabled = true;
}}

function confirmEditorialImagePick() {{
  const s = _editorialImgPickerState;
  if (!s.file || !s.selectedWebPath) return;
  const btn = document.getElementById('editorial-img-modal-confirm');
  const orig = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'Saving…';

  fetch('/api/editorial/swap-image', {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json' }},
    body: JSON.stringify({{
      file: s.file,
      image_web_path: s.selectedWebPath,
      image_filename: s.selectedFilename,
    }}),
  }})
  .then(r => r.json())
  .then(d => {{
    if (d.ok) {{
      closeEditorialImagePicker();
      showEditorialToast('Image swapped — reloading…');
      setTimeout(() => location.reload(), 600);
    }} else {{
      btn.disabled = false;
      btn.textContent = orig;
      alert('Swap failed: ' + (d.error || 'unknown'));
    }}
  }})
  .catch(err => {{
    btn.disabled = false;
    btn.textContent = orig;
    alert('Swap error: ' + err.message);
  }});
}}

</script>

</body>
</html>"""
    return html


def _esc(text):
    """HTML-escape a string."""
    if not text:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _esc_js(text):
    """Escape for safe inclusion inside a JS single-quoted string in an HTML attribute."""
    if not text:
        return ""
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# ── Git auto-push (after every Pubblica) ─────────────────────────────
#
# Best-effort wrapper that runs `git add -A && git commit && git push`
# after a successful publish, so the live site picks up the new article
# without manual operator intervention.
#
# All failure modes (no remote, no network, auth issue, merge conflict,
# nothing to commit) are caught and logged but **never** roll back the
# publish — the article is already in blog/ and the indexes are already
# rewritten; pushing is a separate, asynchronous concern.
#
# Disable for a single session by exporting MYVILLA_AUTOPUSH=0 before
# launching review.command.

_AUTOPUSH_TIMEOUT_S = 30


def _autopush_commit_message(filename: str, content_type: str) -> str:
    """Build a human-readable commit message for the auto-push.

    For journal articles, tries to pull the title from the published
    HTML <title> tag; falls back to the filename. Social approvals get a
    generic message because their content lives outside the tracked tree
    (the autopush will usually no-op for social, but the message is
    still set in case future state changes cause something to commit).
    """
    if content_type == "journal":
        published = BLOG_DIR / filename
        if published.exists():
            try:
                content = published.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"<title>(.*?)</title>", content, re.IGNORECASE | re.DOTALL)
                if m:
                    title = html.unescape(m.group(1).strip())
                    title = re.sub(r"\s*[—-]\s*My Villa Journal\s*$", "", title)
                    if title:
                        return f"Publish journal: {title}"
            except Exception:
                pass
        return f"Publish journal: {filename}"
    if content_type == "social":
        return f"Approve social post: {filename}"
    return f"Update from approve.py: {filename}"


def _run_git(args: list[str], timeout: int = _AUTOPUSH_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Thin wrapper: always run from ROOT_DIR, capture output, never raise."""
    return subprocess.run(
        ["git", *args],
        cwd=str(ROOT_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _git_autopush(commit_msg: str) -> bool:
    """Stage all → commit → push origin/main. Best-effort; returns success.

    Returns True only when a push actually happened (i.e. there was
    something to commit AND it landed on origin). Returns False for
    every other case (disabled by env var, no remote, nothing to commit,
    auth failure, network failure). Never raises.
    """
    if os.environ.get("MYVILLA_AUTOPUSH", "1").strip() in ("0", "false", "no", ""):
        print("  [autopush] disabled via MYVILLA_AUTOPUSH=0 — skipping")
        return False

    if not (ROOT_DIR / ".git").exists():
        print("  [autopush] no .git directory in project root — skipping")
        return False

    try:
        # 1. Verify there is a remote configured. Without one we can't push.
        remote_check = _run_git(["remote"], timeout=5)
        if remote_check.returncode != 0 or not remote_check.stdout.strip():
            print("  [autopush] no git remote configured — skipping "
                  "(set up with: git remote add origin <url>)")
            return False

        # 2. Stage everything (gitignore-protected: .env, drafts, archives,
        # OAuth tokens are all excluded by .gitignore).
        add = _run_git(["add", "-A"], timeout=15)
        if add.returncode != 0:
            print(f"  [autopush] git add failed: {add.stderr.strip()[:200]}")
            return False

        # 3. Skip if nothing changed (e.g. social approve that only touches
        # _system/social/posts/, which is in .gitignore).
        diff_quiet = _run_git(["diff", "--cached", "--quiet"], timeout=5)
        if diff_quiet.returncode == 0:
            print("  [autopush] nothing to commit — skipping push")
            return False

        # 4. Commit.
        commit = _run_git(["commit", "-m", commit_msg], timeout=15)
        if commit.returncode != 0:
            print(f"  [autopush] commit failed: {commit.stderr.strip()[:200]}")
            return False

        # 5. Push. The slow step. If it fails (network, auth, conflict)
        # the local commit is still made — operator can run `git push`
        # later to recover.
        push = _run_git(["push", "origin", "main"], timeout=_AUTOPUSH_TIMEOUT_S)
        if push.returncode != 0:
            print(f"  [autopush] push failed: {push.stderr.strip()[:200]}")
            print("  [autopush] commit is local-only; run 'git push' "
                  "manually when the issue is resolved")
            return False

        # 6. Report the new commit hash for log clarity.
        sha = _run_git(["rev-parse", "--short", "HEAD"], timeout=5)
        sha_str = sha.stdout.strip() if sha.returncode == 0 else "(unknown)"
        print(f"  [autopush] ✓ pushed {sha_str} to origin/main "
              f"({commit_msg!r})")
        return True

    except subprocess.TimeoutExpired:
        print(f"  [autopush] timeout after {_AUTOPUSH_TIMEOUT_S}s — push abandoned. "
              "Run 'git push' manually.")
        return False
    except Exception as e:  # noqa: BLE001 — never crash the publish
        print(f"  [autopush] unexpected error: {type(e).__name__}: {e}")
        return False


# ── HTTP Request Handler ─────────────────────────────────────────────

class ReviewHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        """Cleaner log output."""
        print(f"  [{self.log_date_time_string()}] {fmt % args}")

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self._cors_headers()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._send_html(build_dashboard())

        elif parsed.path == "/radar":
            # Serve the latest radar dashboard HTML (same-origin for Safari)
            params = parse_qs(parsed.query)
            requested = params.get("file", [None])[0]
            radar_dir = SYSTEM_DIR / "radar" / "reports"
            if requested:
                safe_name = Path(requested).name
                target = radar_dir / safe_name
                if not target.exists() or target.suffix != ".html":
                    self._send_html(f"<h1>Radar file not found: {_esc(safe_name)}</h1>", 404)
                    return
            else:
                # Pick most recent radar_*.html
                html_files = sorted(
                    radar_dir.glob("radar_*.html"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if not html_files:
                    self._send_html("<h1>No radar dashboards found in _system/radar/reports/</h1>", 404)
                    return
                target = html_files[0]
            self._send_html(target.read_text(encoding="utf-8", errors="replace"))

        elif parsed.path == "/radar-md":
            # Serve the most recent radar .md file as plain HTML-wrapped text.
            #
            # NOTE (2026-04-29): No longer linked from the home dashboard —
            # the /radar HTML view supersedes it for everyday use. Kept
            # active because the endpoint is still useful for:
            #   - copy-pasting the digest into an email
            #   - reading offline / from a terminal browser
            #   - direct-URL access by external scripts
            # If nothing references it for a few weeks, delete the route
            # and the surrounding helpers.
            radar_dir = SYSTEM_DIR / "radar" / "reports"
            md_files = sorted(
                [f for f in radar_dir.glob("radar_*.md")
                 if re.match(r"^radar_\d{4}-\d{2}-\d{2}\.md$", f.name)],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not md_files:
                self._send_html("<h1>No radar .md digest found. Run generate_radar_report.py with --markdown.</h1>", 404)
                return
            md_content = md_files[0].read_text(encoding="utf-8", errors="replace")
            escaped = _esc(md_content)
            html_wrapper = (
                "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
                "<title>Radar Digest · " + md_files[0].stem + "</title>"
                "<style>body{font-family:ui-monospace,monospace;max-width:820px;margin:2rem auto;padding:2rem;"
                "background:#FAF8F5;color:#2C2C2C;line-height:1.55;white-space:pre-wrap;font-size:14px;}"
                "a{color:#6B8F9E;}</style></head><body>" + escaped + "</body></html>"
            )
            self._send_html(html_wrapper)

        elif parsed.path == "/preview":
            params = parse_qs(parsed.query)
            filename = params.get("file", [None])[0]
            if not filename:
                self._send_html("<h1>Missing ?file= parameter</h1>", 400)
                return

            # Sanitize: only allow simple filenames, no path traversal
            safe_name = Path(filename).name
            # Look in _drafts/journal/ first (queue), then in blog/
            # (already-published). This lets the same /preview URL serve
            # both draft review AND post-publish editing without the
            # caller needing to know which folder the file lives in.
            filepath = DRAFTS_DIR / "journal" / safe_name
            in_blog = False
            if not filepath.exists():
                blog_path = BLOG_DIR / safe_name
                if blog_path.exists():
                    filepath = blog_path
                    in_blog = True
            if not filepath.exists() or filepath.suffix != ".html":
                self._send_html(f"<h1>File not found: {_esc(safe_name)}</h1>", 404)
                return

            content = filepath.read_text(encoding="utf-8", errors="replace")
            # Inject WYSIWYG inline editor — the editor JS handles both
            # draft and live articles; the difference is the save target
            # (the existing /api/save_journal route already detects the
            # current location).
            wysiwyg = _build_wysiwyg_injection(safe_name, in_blog=in_blog)
            content = content.replace("</body>", wysiwyg + "\n</body>")
            self._send_html(content)

        elif parsed.path == "/api/editorial/list-image-candidates":
            params = parse_qs(parsed.query)
            self._handle_editorial_list_image_candidates(
                params.get("file", [""])[0]
            )

        elif (parsed.path.startswith("/blog/assets/") or parsed.path.startswith("/assets/")
              or parsed.path.startswith("/img/")
              or parsed.path.startswith("/_system/social/partner_cache/")):
            # Static asset passthrough so previews can load hero images etc.
            rel = parsed.path.lstrip("/")
            root = (SYSTEM_DIR.parent).resolve()
            # Draft articles live at _drafts/journal/ but their <img src="assets/img/...">
            # is relative, so browsers requesting /assets/img/X.jpg must find the file
            # at blog/assets/img/X.jpg (where it's actually stored). Try both locations.
            candidates = [(SYSTEM_DIR.parent / rel).resolve()]
            if rel.startswith("assets/"):
                candidates.append((SYSTEM_DIR.parent / "blog" / rel).resolve())
            candidate = None
            for c in candidates:
                # Prevent path traversal
                try:
                    c.relative_to(root)
                except ValueError:
                    continue
                if c.exists() and c.is_file():
                    candidate = c
                    break
            if candidate is None:
                self._send_html(f"<h1>Asset not found: {_esc(parsed.path)}</h1>", 404)
                return
            ext = candidate.suffix.lower()
            ctype = {
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".png": "image/png",
                ".webp": "image/webp",
                ".gif": "image/gif",
                ".svg": "image/svg+xml",
                ".css": "text/css",
                ".js": "application/javascript",
            }.get(ext, "application/octet-stream")
            body = candidate.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self._cors_headers()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        else:
            self._send_html("<h1>404 Not Found</h1>", 404)

    def do_POST(self):
        parsed = urlparse(self.path)

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            data = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send_json({"ok": False, "error": "Invalid JSON"}, 400)
            return

        filename = data.get("file", "")
        content_type = data.get("type", "")

        # Special case: social_credentials doesn't need file/type
        if parsed.path == "/api/social_credentials":
            self._handle_social_credentials()
            return

        # Special case: send-email is a direct Gmail send (no file persistence)
        if parsed.path == "/api/send-email":
            self._handle_send_email(
                data.get("to", ""),
                data.get("subject", ""),
                data.get("body", ""),
                uploaded_attachments=data.get("uploaded_attachments") or [],
                source_url=(data.get("source_url") or "").strip(),
            )
            return

        # ── Reply follow-up endpoints (Sprint 2) ─────────────────────────
        if parsed.path == "/api/send-reply":
            self._handle_send_reply(
                thread_id=data.get("thread_id", ""),
                to=data.get("to", ""),
                subject=data.get("subject", ""),
                body=data.get("body", ""),
                in_reply_to=data.get("in_reply_to"),
                references=data.get("references"),
                attachments=data.get("attachments") or [],
                uploaded_attachments=data.get("uploaded_attachments") or [],
            )
            return
        if parsed.path == "/api/scan-replies":
            self._handle_scan_replies(
                with_drafts=bool(data.get("with_drafts", True)),
            )
            return
        if parsed.path == "/api/redraft-reply":
            self._handle_redraft_reply(data.get("thread_id", ""))
            return
        if parsed.path == "/api/dismiss-reply":
            self._handle_dismiss_reply(data.get("thread_id", ""))
            return
        if parsed.path == "/api/dismiss-radar-item":
            self._handle_dismiss_radar_item(data.get("url", ""))
            return
        if parsed.path == "/api/reddit-comment":
            self._handle_reddit_comment(
                data.get("url", ""), data.get("text", ""))
            return
        if parsed.path == "/api/generate-ig-companion":
            self._handle_generate_ig_companion(
                data.get("file", ""),
                force=bool(data.get("force", False)),
            )
            return
        if parsed.path == "/api/save-ig-companion":
            self._handle_save_ig_companion(
                data.get("file", ""),
                data.get("body", ""),
            )
            return

        # ── Editorial IG endpoints (Phase 1 — draft-only) ─────────────
        # These intentionally bypass the journal/social file/type
        # validation below: editorial drafts live in their own folder
        # and have a different shape (no `type` field).
        if parsed.path == "/api/editorial/save":
            self._handle_editorial_save(data.get("file", ""), data.get("body", ""))
            return
        if parsed.path == "/api/editorial/regenerate":
            self._handle_editorial_regenerate(data.get("file", ""))
            return
        if parsed.path == "/api/editorial/build-package":
            self._handle_editorial_build_package(data.get("file", ""))
            return
        if parsed.path == "/api/editorial/mark-published":
            self._handle_editorial_mark_published(
                data.get("file", ""),
                data.get("ig_post_url", ""),
            )
            return
        if parsed.path == "/api/editorial/discard":
            self._handle_editorial_discard(data.get("file", ""))
            return
        if parsed.path == "/api/editorial/plan-month":
            self._handle_editorial_plan(data.get("month", ""))
            return
        if parsed.path == "/api/editorial/generate":
            self._handle_editorial_generate(
                data.get("month", ""),
                int(data.get("days_ahead") or 14),
            )
            return
        if parsed.path == "/api/editorial/swap-image":
            self._handle_editorial_swap_image(
                data.get("file", ""),
                data.get("image_web_path", ""),
                data.get("image_filename", ""),
            )
            return

        # Special case: radar_draft is an in-memory revision (no file persistence)
        # Only valid on /api/revise endpoint.
        if content_type == "radar_draft":
            if parsed.path != "/api/revise":
                self._send_json(
                    {"ok": False, "error": "radar_draft only supported on /api/revise"},
                    400,
                )
                return
            self._handle_revise_radar(
                data.get("draft_type", ""),
                data.get("content", ""),
                data.get("feedback", ""),
                data.get("context") or {},
            )
            return

        # Validate
        if not filename or content_type not in ("journal", "social"):
            self._send_json(
                {"ok": False, "error": "Missing 'file' or invalid 'type'"},
                400,
            )
            return

        # Sanitize filename
        safe_name = Path(filename).name

        if parsed.path == "/approve":
            self._handle_approve(safe_name, content_type)
        elif parsed.path == "/reject":
            self._handle_reject(safe_name, content_type)
        elif parsed.path == "/api/unpublish":
            self._handle_unpublish(safe_name, content_type)
        elif parsed.path == "/api/get_draft":
            self._handle_get_draft(safe_name, content_type)
        elif parsed.path == "/api/save_draft":
            self._handle_save_draft(safe_name, content_type, data.get("data") or {})
        elif parsed.path == "/api/revise":
            self._handle_revise(
                safe_name,
                content_type,
                data.get("feedback", ""),
                data.get("content"),
            )
        elif parsed.path == "/api/fetch_images":
            self._handle_fetch_images(
                safe_name,
                content_type,
                data.get("query", ""),
            )
        elif parsed.path == "/api/select_image":
            self._handle_select_image(
                safe_name,
                content_type,
                data.get("candidate") or {},
            )
        elif parsed.path == "/api/publish_social":
            self._handle_publish_social(
                safe_name,
                content_type,
                data.get("platform", ""),
                data.get("text", ""),
                data.get("image_url", ""),
            )
        elif parsed.path == "/api/social_credentials":
            self._handle_social_credentials()
        else:
            self._send_json({"ok": False, "error": "Unknown endpoint"}, 404)

    def _handle_approve(self, filename, content_type):
        if content_type == "journal":
            src = DRAFTS_DIR / "journal" / filename
            dst = BLOG_DIR / filename
            if not src.exists():
                self._send_json({"ok": False, "error": f"File not found: {filename}"}, 404)
                return

            # ── Pre-publish link-check gate ───────────────────────────────
            # Block publishing any draft with broken external links. This is
            # the backstop against LLM-fabricated source URLs that slipped past
            # generation-time verification (verify_sources_live in
            # generate_journal.py). Callers can pass force=true to override
            # after a human review.
            force = False
            try:
                import json as _json
                content_length = int(self.headers.get("Content-Length", 0))
                # Body already consumed in do_POST; re-peek via the request
                # data is not possible here. Use a query string fallback.
                parsed = urlparse(self.path)
                qs = parse_qs(parsed.query or "")
                force = (qs.get("force", ["0"])[0] or "0").lower() in ("1", "true", "yes")
            except Exception:
                force = False

            if VALIDATE_LINKS_OK and not force:
                try:
                    report = validate_links_file(src, fix=False)
                    broken = report.get("broken") or []
                    if broken:
                        print(f"  [Approve] BLOCKED: {filename} has {len(broken)} broken link(s)")
                        for b in broken:
                            print(f"    ✗ {b.get('status','---')}  {b.get('url','?')}  {b.get('reason','')}")
                        self._send_json({
                            "ok": False,
                            "error": (
                                f"Cannot publish: {len(broken)} broken external link(s) "
                                f"detected. Fix or re-verify the draft before approving."
                            ),
                            "broken_links": [
                                {
                                    "url": b.get("url", ""),
                                    "status": b.get("status", 0),
                                    "reason": b.get("reason", ""),
                                }
                                for b in broken
                            ],
                        }, 400)
                        return
                    print(f"  [Approve] Link check OK ({report.get('ok',0)}/{report.get('total',0)} links verified)")
                except Exception as e:
                    print(f"  [Approve] Warning: link validation raised {e} — publishing anyway")

            # ── Before moving: dismiss the source URLs in the sidecar ──
            # so the radar won't re-suggest the same story on the next
            # scan. Same mechanism used by Cancella and by the Discard
            # button on Radar News cards. Done BEFORE the move so the
            # sidecar is still findable at the draft path.
            dismissed_source_urls = self._dismiss_journal_sources(
                src.with_suffix(".json"),
                reason_note=f"published journal: {filename}",
            )

            BLOG_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            print(f"  Published: {filename} -> blog/")

            # Also move the sidecar .json (if present) so it doesn't orphan in _drafts/
            sidecar_src = src.with_suffix(".json")
            if sidecar_src.exists():
                sidecar_dst = dst.with_suffix(".json")
                shutil.move(str(sidecar_src), str(sidecar_dst))
                print(f"  Moved sidecar JSON: {sidecar_src.name} -> blog/")

            # ── IG companion: decoupled from journal publish ────────
            # 2026-05-12: the operator wants journal-publish and IG-publish
            # to be separate clicks (the IG button on the card is the
            # other "Pubblica" — currently disabled until the IG account
            # is wired up).
            #
            # So we leave the .ig.md companion in place in _drafts/journal/
            # when the journal article is published. It becomes orphaned
            # (no sibling .html anymore) but the file stays there for the
            # operator to either:
            #   - approve it later via the (future) "Pubblica IG" button
            #   - let auto-archive sweep it after 14 days
            # When the IG account goes live, we'll add an
            # /approve-ig endpoint that moves the file to
            # _system/social/posts/approved/.
            companion_src = src.with_suffix(".ig.md")
            if companion_src.exists():
                print(f"  IG companion left in place: {companion_src.name} "
                      f"(awaiting separate 'Pubblica IG' action)")

            # Update journal index
            idx_script = SCRIPT_DIR / "update_journal_index.py"
            if idx_script.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(idx_script)],
                        cwd=str(SCRIPT_DIR),
                        timeout=30,
                    )
                    print("  Rebuilt journal index.")
                except Exception as e:
                    print(f"  Warning: update_journal_index.py failed: {e}")

            # Update sitemap
            sm_script = SCRIPT_DIR / "update_sitemap.py"
            if sm_script.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(sm_script)],
                        cwd=str(SCRIPT_DIR),
                        timeout=30,
                    )
                    print("  Rebuilt sitemap.")
                except Exception as e:
                    print(f"  Warning: update_sitemap.py failed: {e}")

            # Refresh the "From the Journal" block on index.html with the
            # latest 3 published articles.
            hp_script = SCRIPT_DIR / "update_homepage_journal.py"
            if hp_script.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(hp_script)],
                        cwd=str(SCRIPT_DIR),
                        timeout=30,
                    )
                    print("  Refreshed homepage journal preview.")
                except Exception as e:
                    print(f"  Warning: update_homepage_journal.py failed: {e}")

            # Auto-push to GitHub so the live site picks up the new article
            # without a manual `git push`. Best-effort: any failure is
            # logged but does not roll back the publish.
            _git_autopush(_autopush_commit_message(filename, "journal"))

            pub_msg = f"Published: {filename}"
            if dismissed_source_urls:
                pub_msg += (
                    f" (and dismissed {len(dismissed_source_urls)} source "
                    f"URL(s) so the radar won't re-suggest)"
                )
            self._send_json({
                "ok": True,
                "message": pub_msg,
                "dismissed_source_urls": dismissed_source_urls,
            })

        elif content_type == "social":
            src = find_social_source(filename)
            approved_dir = SYSTEM_DIR / "social" / "posts" / "approved"
            if src is None:
                self._send_json({"ok": False, "error": f"File not found: {filename}"}, 404)
                return

            approved_dir.mkdir(parents=True, exist_ok=True)
            if filename.endswith(".ig.md"):
                # Companion di un articolo: il file vive in blog/ (fa
                # parte dell'articolo) → si COPIA in coda come post IG
                # e si marca l'originale, senza spostarlo.
                raw = src.read_text(encoding="utf-8", errors="replace")
                m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
                fm_text = m.group(1) if m else ""
                body_txt = (m.group(2).strip() if m else raw.strip())
                if "status:" not in fm_text:
                    fm_text += "\nstatus: approved"
                else:
                    fm_text = re.sub(r"status:.*", "status: approved", fm_text)
                if "channel:" not in fm_text:
                    fm_text = "channel: ig\n" + fm_text
                dst = approved_dir / (Path(filename).stem.replace(".ig", "")
                                      + "-companion.md")
                dst.write_text(f"---\n{fm_text}\n---\n\n{body_txt}\n",
                               encoding="utf-8")
                # marca l'originale così non viene riproposto
                src.write_text(raw.replace("---\n", "---\nstatus: approved\n", 1)
                               if "status:" not in raw.split("---")[1]
                               else re.sub(r"status:.*", "status: approved",
                                           raw, count=1),
                               encoding="utf-8")
            else:
                dst = approved_dir / filename
                shutil.move(str(src), str(dst))
            print(f"  Approved social: {filename} -> _system/social/posts/approved/")

            # Best-effort autopush. _system/social/posts/ is gitignored so
            # the call usually no-ops, but we keep the hook for symmetry
            # and in case future state changes touch tracked files.
            _git_autopush(_autopush_commit_message(filename, "social"))

            self._send_json({"ok": True, "message": f"Approved: {filename}"})

    def _handle_unpublish(self, filename, content_type):
        """Remove an already-published article from the live site.

        Mirror of _handle_approve in reverse: the .html + .json
        (sidecar) move from blog/ → _archive/blog/journal/, the
        indices/sitemap/homepage are rebuilt, and the change is
        auto-pushed so GitHub Pages drops the page within ~60s.

        Only journal content_type is supported (social posts don't
        have a public "unpublish" semantics — they're published
        directly to X/IG, not to myvilla.la).

        The article's source URLs stay in previously_reported.json:
        unpublishing means "we no longer want this on the site", not
        "the underlying story is fair game for re-coverage". If the
        operator wants the radar to re-suggest those URLs, they can
        edit previously_reported.json by hand to remove the entries.
        """
        if content_type != "journal":
            self._send_json(
                {"ok": False, "error": "Only 'journal' is unpublishable."},
                400,
            )
            return

        src_html = BLOG_DIR / Path(filename).name
        if not src_html.exists() or src_html.suffix != ".html":
            self._send_json(
                {"ok": False, "error": f"Not in blog/: {filename}"},
                404,
            )
            return

        archive_sub = ARCHIVE_DIR / "blog" / "journal"
        archive_sub.mkdir(parents=True, exist_ok=True)

        moved = []
        # Move .html
        dst_html = archive_sub / src_html.name
        # Disambiguate if the same name was already unpublished once.
        if dst_html.exists():
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            dst_html = archive_sub / f"{src_html.stem}.{ts}{src_html.suffix}"
        try:
            shutil.move(str(src_html), str(dst_html))
            moved.append(dst_html.name)
            print(f"  Unpublished: {src_html.name} → _archive/blog/journal/")
        except Exception as e:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"Move failed: {type(e).__name__}: {e}"},
                500,
            )
            return

        # Move sidecar .json
        sidecar = src_html.with_suffix(".json")
        if sidecar.exists():
            dst_json = archive_sub / sidecar.name
            if dst_json.exists():
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                dst_json = archive_sub / f"{sidecar.stem}.{ts}{sidecar.suffix}"
            shutil.move(str(sidecar), str(dst_json))
            moved.append(dst_json.name)
            print(f"  Moved sidecar: {sidecar.name}")

        # Rebuild journal index + sitemap + homepage block
        for script_name in (
            "update_journal_index.py",
            "update_sitemap.py",
            "update_homepage_journal.py",
        ):
            script_path = SCRIPT_DIR / script_name
            if script_path.exists():
                try:
                    subprocess.run(
                        [sys.executable, str(script_path)],
                        cwd=str(SCRIPT_DIR),
                        timeout=30,
                    )
                    print(f"  Rebuilt: {script_name}")
                except Exception as e:  # noqa: BLE001
                    print(f"  Warning: {script_name} failed: {e}")

        # Auto-push so the live site drops the page within ~60s.
        _git_autopush(f"Unpublish journal: {Path(filename).stem}")

        self._send_json({
            "ok": True,
            "message": f"Unpublished: {filename}",
            "archived_as": moved,
        })

    def _handle_reject(self, filename, content_type):
        if content_type == "journal":
            src = DRAFTS_DIR / "journal" / filename
            archive_sub = ARCHIVE_DIR / "journal"
        elif content_type == "social":
            src = find_social_source(filename) or (DRAFTS_DIR / "social" / filename)
            archive_sub = ARCHIVE_DIR / "social"
        else:
            self._send_json({"ok": False, "error": "Invalid type"}, 400)
            return

        if not src.exists():
            self._send_json({"ok": False, "error": f"File not found: {filename}"}, 404)
            return

        # ── Journal-only: before archiving, persist the source URLs ──
        # so the radar never re-suggests this article on future scans.
        # Both rejection AND publication should mark the sources as
        # handled — see _dismiss_journal_sources for the shared logic.
        dismissed_source_urls = []
        if content_type == "journal":
            dismissed_source_urls = self._dismiss_journal_sources(
                src.with_suffix(".json"),
                reason_note=f"rejected journal: {filename}",
            )

        archive_sub.mkdir(parents=True, exist_ok=True)
        dst = archive_sub / filename
        shutil.move(str(src), str(dst))
        print(f"  Archived: {filename} -> _archive/{content_type}/")

        # Journal: also archive the sidecar .json if present
        if content_type == "journal":
            sidecar_src = src.with_suffix(".json")
            if sidecar_src.exists():
                sidecar_dst = dst.with_suffix(".json")
                shutil.move(str(sidecar_src), str(sidecar_dst))
                print(f"  Archived sidecar JSON: {sidecar_src.name}")
            # Also archive the IG companion .ig.md if the operator
            # generated one — rejecting the article kills the companion
            # too (it was tied to that specific piece).
            companion_src = src.with_suffix(".ig.md")
            if companion_src.exists():
                companion_dst = dst.with_suffix(".ig.md")
                shutil.move(str(companion_src), str(companion_dst))
                print(f"  Archived IG companion: {companion_src.name}")

        msg = f"Archived: {filename}"
        if dismissed_source_urls:
            msg += f" (also dismissed {len(dismissed_source_urls)} source URL(s) so the radar won't re-suggest)"
        self._send_json({
            "ok": True,
            "message": msg,
            "dismissed_source_urls": dismissed_source_urls,
        })

    def _dismiss_journal_sources(self, sidecar_path, reason_note):
        """Read a journal sidecar JSON, dismiss every sources[].url+title.

        Called from both _handle_approve and _handle_reject so the radar
        never re-suggests an article we've already processed — published
        OR rejected, both are "handled" from the operator's standpoint.

        Returns the list of URLs successfully added (skips duplicates
        already in previously_reported.json). Empty list when sidecar
        missing or has no sources.
        """
        if not sidecar_path.exists():
            return []
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  Warning: could not parse {sidecar_path.name}: {e}")
            return []
        urls, titles = [], []
        seen = set()
        for s in (sidecar.get("sources") or []):
            u = (s.get("url") or "").strip()
            t = (s.get("title") or "").strip()
            if u and u not in seen:
                seen.add(u)
                urls.append(u)
                titles.append(t)
        if not urls:
            return []
        added = self._mark_urls_user_dismissed(
            urls, reason_note=reason_note, titles=titles,
        )
        # Return the list we attempted to add (caller wants to surface
        # them in the API response). `added` is just the count.
        return urls if added else []

    def _mark_urls_user_dismissed(self, urls, reason_note="", titles=None):
        """Append URLs (and optional matching titles) to previously_reported.json.

        Idempotent: URLs already present are skipped. Used by the Radar
        News "Discard" button (single URL, no title) and by
        _dismiss_journal_sources (multiple URL+title pairs).

        `titles`: optional list parallel to `urls`. When provided, each
        dedup entry gets a non-empty title field so radar.py's title
        fuzzy-match catches the same story under a different aggregator
        URL.

        Atomic write protects the index from torn-write corruption.
        """
        if not urls:
            return 0
        # Pad/trim titles to match urls length so zip() never silently
        # truncates the loop.
        if titles is None:
            titles = [""] * len(urls)
        elif len(titles) < len(urls):
            titles = titles + [""] * (len(urls) - len(titles))
        dedup_path = SYSTEM_DIR / "radar" / "previously_reported.json"
        try:
            if dedup_path.exists():
                data = json.loads(dedup_path.read_text(encoding="utf-8"))
            else:
                data = {"reported_articles": []}
            articles = data.setdefault("reported_articles", [])
            existing = {a.get("url") for a in articles if a.get("url")}
            added = 0
            today = datetime.now().strftime("%Y-%m-%d")
            for u, t in zip(urls, titles):
                if u in existing:
                    continue
                articles.append({
                    "date_first_reported": today,
                    "source": "user_dismissed",
                    "title": t,
                    "score": None,
                    "cluster": None,
                    "action_type": "user_dismissed",
                    "url": u,
                    "note": reason_note,
                })
                existing.add(u)
                added += 1
            if added:
                tmp = dedup_path.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                tmp.replace(dedup_path)
                print(f"  [dismiss] {added} URL(s) added to previously_reported.json ({reason_note})")
            return added
        except Exception as e:  # noqa: BLE001
            print(f"  [dismiss] failed to update previously_reported.json: {e}")
            return 0

    # ── /api/get_draft ──────────────────────────────────────────────
    def _handle_get_draft(self, filename, content_type):
        try:
            if content_type == "journal":
                # Journal: read the .json sidecar
                html_path = DRAFTS_DIR / "journal" / filename
                if filename.endswith(".json"):
                    json_path = html_path
                else:
                    json_path = html_path.with_suffix(".json")
                if not json_path.exists():
                    self._send_json(
                        {"ok": False, "error": f"JSON sidecar not found: {json_path.name}"},
                        404,
                    )
                    return
                data = json.loads(json_path.read_text(encoding="utf-8"))
                self._send_json({"ok": True, "data": data})
                return

            # social
            md_path = find_social_source(filename) or (DRAFTS_DIR / "social" / filename)
            if not md_path.exists():
                self._send_json({"ok": False, "error": f"File not found: {filename}"}, 404)
                return
            raw = md_path.read_text(encoding="utf-8")
            frontmatter = {}
            body = raw
            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
            if fm_match:
                fm_text = fm_match.group(1)
                body = fm_match.group(2).strip()
                for line in fm_text.splitlines():
                    kv = line.split(":", 1)
                    if len(kv) == 2:
                        frontmatter[kv[0].strip()] = kv[1].strip().strip('"').strip("'")
            self._send_json({"ok": True, "data": {"frontmatter": frontmatter, "body": body}})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ── /api/save_draft ─────────────────────────────────────────────
    def _handle_save_draft(self, filename, content_type, data):
        try:
            if content_type == "journal":
                # Look in _drafts/journal/ first; if missing, fall back
                # to blog/. This allows the same /api/save_draft route to
                # update both pending drafts AND already-published
                # articles edited in-place via the WYSIWYG editor.
                html_path = DRAFTS_DIR / "journal" / filename
                if filename.endswith(".json"):
                    json_path = html_path
                    html_path = json_path.with_suffix(".html")
                else:
                    json_path = html_path.with_suffix(".json")
                # Fallback to blog/ if the draft path doesn't exist.
                if not html_path.exists() and not json_path.exists():
                    blog_html = BLOG_DIR / Path(filename).name
                    blog_json = blog_html.with_suffix(".json")
                    if blog_html.exists() or blog_json.exists():
                        html_path = blog_html if filename.endswith(".html") else blog_html
                        if filename.endswith(".json"):
                            json_path = blog_json
                        else:
                            json_path = blog_html.with_suffix(".json")

                if not isinstance(data, dict):
                    self._send_json({"ok": False, "error": "data must be an object"}, 400)
                    return

                # Preserve existing keys we weren't sent (e.g., _date, _section_id, etc.)
                if json_path.exists():
                    try:
                        existing = json.loads(json_path.read_text(encoding="utf-8"))
                        if isinstance(existing, dict):
                            merged = dict(existing)
                            merged.update(data)
                            data = merged
                    except Exception:
                        pass

                json_path.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

                # Re-render HTML
                try:
                    from generate_journal import render_article_html
                    date_str = data.get("_date") or datetime.now().strftime("%Y-%m-%d")
                    new_html = render_article_html(data, date_str)
                    html_path.write_text(new_html, encoding="utf-8")
                    print(f"  Saved & re-rendered: {html_path.name}")
                except Exception as render_err:
                    print(f"  Warning: failed to re-render HTML: {render_err}")
                    self._send_json(
                        {
                            "ok": False,
                            "error": f"JSON saved but HTML re-render failed: {render_err}",
                        },
                        500,
                    )
                    return

                self._send_json({"ok": True, "message": "Saved"})
                return

            # social: rewrite .md with updated body and char_count
            md_path = find_social_source(filename) or (DRAFTS_DIR / "social" / filename)
            if not md_path.exists():
                self._send_json({"ok": False, "error": f"File not found: {filename}"}, 404)
                return

            new_body = data.get("body", "") if isinstance(data, dict) else ""
            raw = md_path.read_text(encoding="utf-8")
            fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
            if fm_match:
                fm_text = fm_match.group(1)
                fm_lines = fm_text.splitlines()
                new_fm_lines = []
                saw_char_count = False
                for line in fm_lines:
                    if line.strip().startswith("char_count:"):
                        new_fm_lines.append(f"char_count: {len(new_body)}")
                        saw_char_count = True
                    else:
                        new_fm_lines.append(line)
                if not saw_char_count:
                    new_fm_lines.append(f"char_count: {len(new_body)}")
                new_fm = "\n".join(new_fm_lines)
                out = f"---\n{new_fm}\n---\n{new_body}\n"
            else:
                out = new_body
            md_path.write_text(out, encoding="utf-8")
            print(f"  Saved social: {md_path.name} ({len(new_body)} chars)")
            self._send_json({"ok": True, "message": "Saved"})
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ── /api/fetch_images ───────────────────────────────────────────
    def _handle_fetch_images(self, filename, content_type, query):
        """Return a list of Unsplash candidate images for the given draft."""
        if content_type != "journal":
            self._send_json(
                {"ok": False, "error": "Image picker only supports journal drafts"},
                400,
            )
            return
        try:
            json_path = DRAFTS_DIR / "journal" / filename
            if not json_path.suffix == ".json":
                json_path = json_path.with_suffix(".json")
            if not json_path.exists():
                self._send_json(
                    {"ok": False, "error": f"Sidecar not found: {json_path.name}"},
                    404,
                )
                return
            sidecar = json.loads(json_path.read_text(encoding="utf-8"))

            # Build search query: user-provided → image_prompt → title+section
            q = (query or "").strip()
            fallbacks: list = []
            if not q:
                q = (sidecar.get("image_prompt") or "").strip()
            # Trim overly long prompts to keep Unsplash relevance sharp
            if len(q) > 140:
                q = q[:140]
            # Build fallbacks from keywords/title
            kw = (sidecar.get("meta_keywords") or "").strip()
            if kw:
                fallbacks.append(", ".join(kw.split(",")[:3]))
            title = (sidecar.get("title") or "").strip()
            if title:
                fallbacks.append(title[:120])
            # Last resort: section + generic architecture terms
            section = (sidecar.get("_section_name") or sidecar.get("section") or "").lower()
            if "insurance" in section:
                fallbacks.append("California home insurance documents")
            elif "market" in section:
                fallbacks.append("Los Angeles luxury real estate architecture")
            else:
                fallbacks.append("Los Angeles architecture concrete house")

            try:
                from image_picker import fetch_candidates, fetch_source_images
            except ImportError as e:
                self._send_json(
                    {"ok": False, "error": f"image_picker unavailable: {e}"},
                    500,
                )
                return

            # 1) Try to extract og:image from each cited source (most relevant)
            sources = sidecar.get("sources") or []
            source_candidates = fetch_source_images(sources)

            # 2) Fetch Unsplash candidates as a fallback pool
            unsplash_candidates = fetch_candidates(
                q, count=9, fallback_queries=fallbacks
            )

            # Put source images FIRST so the user sees them at the top
            combined = source_candidates + unsplash_candidates

            # Auto-download the first source image so it's immediately
            # available locally without user intervention.
            auto_hero = None
            if source_candidates:
                try:
                    from image_picker import download_source_image
                    out_dir = BLOG_DIR / "assets" / "img"
                    first = source_candidates[0]
                    hero = download_source_image(first, slug, out_dir)
                    if hero:
                        sidecar["hero_image"] = hero
                        json_path.write_text(
                            json.dumps(sidecar, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        # Re-render HTML with the auto-selected hero
                        try:
                            from generate_journal import render_article_html
                            date_str = sidecar.get("_date") or datetime.now().strftime("%Y-%m-%d")
                            html_path = json_path.with_suffix(".html")
                            html_path.write_text(
                                render_article_html(sidecar, date_str),
                                encoding="utf-8",
                            )
                        except Exception:
                            pass
                        auto_hero = hero
                        print(f"  [auto-image] Saved first source image for {slug}")
                except Exception as e:
                    print(f"  [auto-image] Failed: {e}")

            self._send_json({
                "ok": True,
                "query_used": q,
                "fallbacks": fallbacks,
                "source_count": len(source_candidates),
                "unsplash_count": len(unsplash_candidates),
                "candidates": combined,
                "auto_hero": auto_hero,
            })
        except Exception as e:
            self._send_json({"ok": False, "error": f"fetch_images error: {e}"}, 500)

    # ── /api/select_image ───────────────────────────────────────────
    def _handle_select_image(self, filename, content_type, candidate):
        """Download the chosen candidate and persist it into the sidecar JSON."""
        if content_type != "journal":
            self._send_json(
                {"ok": False, "error": "Image picker only supports journal drafts"},
                400,
            )
            return
        if not isinstance(candidate, dict) or not candidate.get("full_url"):
            self._send_json(
                {"ok": False, "error": "Invalid candidate payload"},
                400,
            )
            return
        try:
            json_path = DRAFTS_DIR / "journal" / filename
            if not json_path.suffix == ".json":
                json_path = json_path.with_suffix(".json")
            if not json_path.exists():
                self._send_json(
                    {"ok": False, "error": f"Sidecar not found: {json_path.name}"},
                    404,
                )
                return

            sidecar = json.loads(json_path.read_text(encoding="utf-8"))
            slug = sidecar.get("slug") or json_path.stem

            origin = (candidate.get("origin") or "unsplash").lower()

            if origin == "source":
                # Download source image locally for reliability and SEO.
                # Credit is preserved with publication name + link to
                # the original article (fair use editorial commentary).
                try:
                    from image_picker import download_source_image
                except ImportError as e:
                    self._send_json(
                        {"ok": False, "error": f"image_picker unavailable: {e}"},
                        500,
                    )
                    return
                out_dir = BLOG_DIR / "assets" / "img"
                hero = download_source_image(candidate, slug, out_dir)
                if not hero:
                    self._send_json(
                        {"ok": False, "error": "Failed to download source image"},
                        500,
                    )
                    return
            else:
                try:
                    from image_picker import download_candidate
                except ImportError as e:
                    self._send_json(
                        {"ok": False, "error": f"image_picker unavailable: {e}"},
                        500,
                    )
                    return
                # Save under the site's blog/assets/img folder so /blog/assets/img/... resolves
                out_dir = BLOG_DIR / "assets" / "img"
                hero = download_candidate(candidate, slug, out_dir)
                if not hero:
                    self._send_json(
                        {"ok": False, "error": "Failed to download image"},
                        500,
                    )
                    return
                hero["origin"] = "unsplash"

            sidecar["hero_image"] = hero
            json_path.write_text(
                json.dumps(sidecar, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # Re-render the draft HTML so the preview shows the new image
            try:
                from generate_journal import render_article_html
                date_str = sidecar.get("_date") or datetime.now().strftime("%Y-%m-%d")
                html_path = json_path.with_suffix(".html")
                html_path.write_text(
                    render_article_html(sidecar, date_str),
                    encoding="utf-8",
                )
                print(f"  Image selected for {slug}: {hero['photo_id']} by {hero['author_name']}")
            except Exception as e:
                self._send_json(
                    {"ok": False, "error": f"Re-render failed: {e}"},
                    500,
                )
                return

            self._send_json({"ok": True, "hero_image": hero})
        except Exception as e:
            self._send_json({"ok": False, "error": f"select_image error: {e}"}, 500)

    # ── /api/revise ─────────────────────────────────────────────────
    def _handle_revise(self, filename, content_type, feedback, current_content):
        if not feedback or not str(feedback).strip():
            self._send_json({"ok": False, "error": "Empty feedback"}, 400)
            return
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._send_json(
                {"ok": False, "error": "ANTHROPIC_API_KEY not set in .env"},
                500,
            )
            return
        try:
            import anthropic
        except ImportError:
            self._send_json(
                {"ok": False, "error": "anthropic package not installed"},
                500,
            )
            return

        try:
            client = anthropic.Anthropic(api_key=api_key)
            model = _WRITER_MODEL

            if content_type == "journal":
                system_prompt = (
                    "You are revising a brand Journal article for My Villa, the Los Angeles "
                    "practice of a European architecture studio (Rome, Paris) designing "
                    "luxury villas in exposed reinforced concrete. "
                    "Voice rules: My Villa in first person (we/our). Never name Paolo Mezzalama "
                    "in Our Perspective. Prefer 'our European studio' over naming IT'S Architecture. "
                    "Use exposed reinforced concrete / cemento a vista as the construction signature. "
                    "Tradition of building for centuries, not decades. "
                    "Return ONLY valid JSON matching the input schema, no markdown code fences, "
                    "no preamble. Keep the same structure (title, slug, excerpt, opening_paragraph, "
                    "sections, our_perspective, key_data, meta_description, meta_keywords, sources) "
                    "but apply the user's requested changes. Keep the same language as the input. "
                    "Preserve any fields starting with underscore."
                )
                user_msg = (
                    "Current article JSON:\n\n"
                    + json.dumps(current_content, ensure_ascii=False, indent=2)
                    + "\n\nUser feedback / revision request:\n"
                    + str(feedback)
                    + "\n\nReturn the full revised JSON now."
                )
                max_tokens = 4000
            else:
                channel = ""
                if isinstance(current_content, str):
                    body_text = current_content
                else:
                    body_text = str(current_content or "")
                # Try to infer channel from filename
                fn_lower = filename.lower()
                if "-x-" in fn_lower or fn_lower.endswith("-x.md"):
                    channel = "X"
                    limits = "X / Twitter: max 280 characters. No hashtags. Punchy, conversational."
                elif "instagram" in fn_lower or "-ig-" in fn_lower:
                    channel = "Instagram"
                    limits = "Instagram: 125-char hook up top, then body, then 5-10 relevant hashtags at the end."
                else:
                    channel = "social"
                    limits = "Keep it short and on-brand."
                system_prompt = (
                    "You are revising a social media post for My Villa, the Los Angeles "
                    "practice of a European architecture studio (Rome, Paris) designing luxury "
                    "villas in exposed reinforced concrete (cemento a vista). Core positioning: "
                    "European construction tradition — solid masonry and reinforced concrete — "
                    "as a direct answer to LA's permits and insurability challenges. "
                    "Channel: " + channel + ". " + limits +
                    " Never use: bunker, fortress, anti-fire, protect your family. "
                    "Return ONLY the revised post text, no preamble, no markdown fences, no explanation."
                )
                user_msg = (
                    "Current post:\n\n" + body_text
                    + "\n\nUser feedback / revision request:\n" + str(feedback)
                    + "\n\nReturn the revised post now."
                )
                max_tokens = 500

            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
            ).strip()

            if content_type == "journal":
                # Strip code fences if present
                t = text
                if t.startswith("```"):
                    t = re.sub(r"^```(?:json)?\s*", "", t)
                    t = re.sub(r"\s*```\s*$", "", t)
                try:
                    revised = json.loads(t)
                except json.JSONDecodeError as je:
                    self._send_json(
                        {"ok": False, "error": f"Opus returned invalid JSON: {je}"},
                        500,
                    )
                    return
                # Preserve underscore-prefixed fields from original
                if isinstance(current_content, dict):
                    for k, v in current_content.items():
                        if k.startswith("_") and k not in revised:
                            revised[k] = v
                self._send_json({"ok": True, "revised": revised})
            else:
                self._send_json({"ok": True, "revised": text})
        except Exception as e:
            self._send_json({"ok": False, "error": f"Opus error: {e}"}, 500)

    # ── /api/revise (radar_draft: email / tweet / reddit) ──────────
    def _handle_revise_radar(self, draft_type, current_content, feedback, context):
        if not feedback or not str(feedback).strip():
            self._send_json({"ok": False, "error": "Empty feedback"}, 400)
            return
        if not current_content or not str(current_content).strip():
            self._send_json({"ok": False, "error": "Empty content"}, 400)
            return
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            self._send_json(
                {"ok": False, "error": "ANTHROPIC_API_KEY not set in .env"},
                500,
            )
            return
        try:
            import anthropic
        except ImportError:
            self._send_json(
                {"ok": False, "error": "anthropic package not installed"},
                500,
            )
            return

        # Build channel-specific prompt
        dtype = (draft_type or "").lower().strip()
        if dtype == "email":
            channel_desc = "an outreach email (journalist/editor pitch)"
            rules = (
                "Format: subject line on first line prefixed with 'Subject: ', then blank line, "
                "then the body. 90-150 words TOTAL (max 180). Warm, human, European in cadence. "
                "Never salesy. Open connecting to THEIR article, middle explain briefly why My Villa's "
                "European reinforced-concrete tradition is relevant to LA's wildfire/insurance crisis, "
                "end by offering 2-3 adjacent story angles we could support on and invite conversation. "
                "Sign off exactly as:\n\n  Best,\n  Lisa Monelli\n  My Villa Media Team\n  myvilla.la\n\n"
                "Do NOT cite specific stats we can't back up. Do NOT claim we've built homes in LA "
                "(we're a European studio with an LA practice opening). No bullet lists in body — "
                "flowing prose. Forbidden: 'bunker', 'fortress', 'industry-leading', 'premier', "
                "'protect your family', 'survive the next fire'."
            )
            max_tokens = 700
        elif dtype == "tweet" or dtype == "x":
            channel_desc = "an X (Twitter) post"
            rules = (
                "Max 280 characters total including any URL. No hashtags. "
                "Punchy, conversational, on-brand. Single paragraph, no emoji spam."
            )
            max_tokens = 300
        elif dtype == "reddit":
            channel_desc = "a Reddit comment/post"
            rules = (
                "Conversational, genuine, helpful. No marketing language. "
                "2-4 short paragraphs. Never link-drop without context. "
                "Write like a real LA homeowner or builder, not a brand."
            )
            max_tokens = 600
        else:
            channel_desc = "an outreach draft"
            rules = "Keep it short, on-brand, and human."
            max_tokens = 500

        # Context about the article being reacted to
        ctx_lines = []
        if isinstance(context, dict):
            if context.get("title"):
                ctx_lines.append(f"Article title: {context['title']}")
            if context.get("publication"):
                ctx_lines.append(f"Publication: {context['publication']}")
            if context.get("url"):
                ctx_lines.append(f"URL: {context['url']}")
        ctx_block = "\n".join(ctx_lines) if ctx_lines else "(no context provided)"

        system_prompt = (
            "You are revising " + channel_desc + " for My Villa, a luxury "
            "reinforced-concrete villa builder in Los Angeles. " + rules + " "
            "Return ONLY the revised text, no preamble, no markdown fences, no explanation."
        )
        user_msg = (
            "Context:\n" + ctx_block +
            "\n\nCurrent draft:\n\n" + str(current_content) +
            "\n\nUser feedback / revision request:\n" + str(feedback) +
            "\n\nReturn the revised draft now."
        )

        try:
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model=_WRITER_MODEL,
                max_tokens=max_tokens,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(
                getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text"
            ).strip()
            # Strip accidental code fences
            if text.startswith("```"):
                text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
                text = re.sub(r"\s*```\s*$", "", text)
            self._send_json({"ok": True, "revised": text})
        except Exception as e:
            self._send_json({"ok": False, "error": f"Opus error: {e}"}, 500)

    # ── /api/publish_social ──────────────────────────────────────────
    def _handle_publish_social(self, filename, content_type, platform, text, image_url):
        """Publish a social post directly to X or Instagram."""
        if content_type != "social":
            self._send_json({"ok": False, "error": "Only social posts can be published"}, 400)
            return
        if not text.strip():
            self._send_json({"ok": False, "error": "Empty post text"}, 400)
            return

        try:
            from publish_social import publish_to_x, publish_to_instagram
        except ImportError as e:
            self._send_json(
                {"ok": False, "error": f"publish_social module not found: {e}"},
                500,
            )
            return

        platform = platform.lower().strip()

        if platform in ("x", "twitter"):
            result = publish_to_x(text)
        elif platform in ("instagram", "ig"):
            result = publish_to_instagram(text, image_url=image_url or None)
        else:
            self._send_json(
                {"ok": False, "error": f"Unknown platform: {platform}"},
                400,
            )
            return

        if result.get("ok"):
            # Move to approved after successful publish
            src = find_social_source(filename) or (DRAFTS_DIR / "social" / filename)
            approved_dir = SYSTEM_DIR / "social" / "posts" / "approved"
            approved_dir.mkdir(parents=True, exist_ok=True)
            if src.exists():
                shutil.move(str(src), str(approved_dir / filename))
                print(f"  Published & approved: {filename} -> {platform}")

        status = 200 if result.get("ok") else (400 if result.get("needs_setup") else 500)
        self._send_json(result, status)

    # ── /api/social_credentials ──────────────────────────────────────
    def _handle_social_credentials(self):
        """Check which social platforms have credentials configured."""
        try:
            from publish_social import check_credentials
            creds = check_credentials()
            self._send_json({"ok": True, **creds})
        except ImportError:
            self._send_json({
                "ok": True,
                "x": {"configured": False, "message": "publish_social module not found"},
                "instagram": {"configured": False, "message": "publish_social module not found"},
            })

    # ── Extra attachments persistence (upload from dashboard) ────────
    # Per-file size cap and total cap: Gmail rejects messages above 25 MB
    # in total (headers + body + base64-encoded attachments). Base64 adds
    # ~33% overhead, so cap raw uploads at 18 MB total to stay safe.
    MAX_UPLOAD_BYTES_PER_FILE = 10 * 1024 * 1024  # 10 MB
    MAX_UPLOAD_BYTES_TOTAL = 18 * 1024 * 1024      # 18 MB combined

    def _persist_uploaded_attachments(self, uploads):
        """
        Decode user-uploaded attachments (base64 from the dashboard) and
        write them to a fresh temp directory. Returns a list of absolute
        paths that can be passed to send_draft / send_reply.

        `uploads` schema: list of {"name": str, "data_b64": str, "mime": str?}

        Raises ValueError on bad input (oversized, empty, missing fields).
        The temp directory is NOT cleaned up automatically — the send
        policy layer reads the files synchronously, so once send_raw
        returns the tempfiles can be garbage-collected on next reboot.
        We rely on /tmp cleanup rather than explicit unlink to keep the
        send path simple and avoid losing evidence if a send fails.
        """
        if not uploads:
            return []
        if not isinstance(uploads, list):
            raise ValueError("uploaded_attachments must be a list")

        total = 0
        decoded = []
        for i, item in enumerate(uploads):
            if not isinstance(item, dict):
                raise ValueError(f"attachment #{i} is not an object")
            name = (item.get("name") or "").strip()
            data_b64 = item.get("data_b64") or ""
            if not name or not data_b64:
                raise ValueError(
                    f"attachment #{i} missing 'name' or 'data_b64'"
                )
            # Sanitize the filename: strip path components, keep just the
            # basename so a hostile upload can't escape the temp dir.
            safe_name = Path(name).name
            try:
                raw = base64.b64decode(data_b64, validate=True)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(
                    f"attachment '{safe_name}' has invalid base64: {exc}"
                )
            size = len(raw)
            if size == 0:
                raise ValueError(f"attachment '{safe_name}' is empty")
            if size > self.MAX_UPLOAD_BYTES_PER_FILE:
                raise ValueError(
                    f"attachment '{safe_name}' is {size/1024/1024:.1f} MB "
                    f"— per-file limit is "
                    f"{self.MAX_UPLOAD_BYTES_PER_FILE/1024/1024:.0f} MB"
                )
            total += size
            if total > self.MAX_UPLOAD_BYTES_TOTAL:
                raise ValueError(
                    f"combined attachments exceed "
                    f"{self.MAX_UPLOAD_BYTES_TOTAL/1024/1024:.0f} MB — "
                    f"Gmail rejects messages over 25 MB in total"
                )
            decoded.append((safe_name, raw))

        # Materialize into a fresh temp subdir. Using a uuid ensures
        # concurrent sends don't collide on identically-named uploads.
        tmp_root = Path(tempfile.gettempdir()) / "myvilla_uploads"
        tmp_root.mkdir(parents=True, exist_ok=True)
        subdir = tmp_root / uuid.uuid4().hex
        subdir.mkdir(parents=True, exist_ok=False)

        paths = []
        for safe_name, raw in decoded:
            dst = subdir / safe_name
            # If two uploads have the same filename, keep both by
            # appending a counter (rare but cheap to guard against).
            if dst.exists():
                stem, suffix = dst.stem, dst.suffix
                n = 1
                while (subdir / f"{stem}_{n}{suffix}").exists():
                    n += 1
                dst = subdir / f"{stem}_{n}{suffix}"
            dst.write_bytes(raw)
            paths.append(str(dst))
        return paths

    # ── /api/send-email ──────────────────────────────────────────────
    def _handle_send_email(self, to, subject, body,
                           uploaded_attachments=None, source_url=""):
        """
        Send an outreach email through the Gmail API.

        The policy layer in `send_email.send_draft` handles:
        - dry_run short-circuit
        - rate limiting
        - signature injection (Lisa Monelli block)
        - JSONL send log

        `uploaded_attachments` is the optional list of base64-encoded files
        the user attached from the dashboard. The canonical voice rule is
        "niente allegati nella prima mail", but the dashboard lets the user
        override this case-by-case when they judge it appropriate.

        `source_url` is the URL of the radar article that produced this
        pitch. When a REAL send succeeds (not dry_run), the URL is
        appended to previously_reported.json as user_dismissed so the
        radar never re-proposes it — guards against accidentally sending
        a second pitch to the same journalist about the same article.

        Returns the SendResult as JSON. When `dry_run: true` in config.yml,
        the response has ok=true and reason='dry_run' but no message_id.
        """
        if not to or not subject or not body:
            self._send_json(
                {"ok": False, "error": "Missing 'to', 'subject', or 'body'"},
                400,
            )
            return
        # Persist user-uploaded attachments to /tmp before passing their
        # paths through to the send policy layer. Validation errors
        # (oversized, empty, malformed base64) are reported as 400 so
        # the dashboard can surface them inline.
        try:
            extra_paths = self._persist_uploaded_attachments(
                uploaded_attachments
            )
        except ValueError as e:
            self._send_json(
                {"ok": False, "error": f"Attachment error: {e}"},
                400,
            )
            return
        try:
            from send_email import send_draft
            result = send_draft(
                to=to, subject=subject, body=body,
                attachments=extra_paths or None,
            )
            if result.get("ok"):
                status = 200
            elif result.get("reason") == "rate_limited":
                status = 429
            elif result.get("reason") == "blacklisted":
                status = 409  # Conflict — address known-bad, don't retry
            else:
                status = 500

            # Anti-double-send guard: on a REAL successful send (not
            # dry_run), mark the source URL as user_dismissed so the
            # radar never re-suggests it. We only do this on real sends
            # so a dry-run rehearsal doesn't permanently hide the item.
            if (result.get("ok")
                    and result.get("reason") != "dry_run"
                    and source_url):
                added = self._mark_urls_user_dismissed(
                    [source_url],
                    reason_note=f"email sent to {to}",
                )
                if added:
                    # Surface the dismissal in the response so the JS
                    # can fade the card out + tell the operator.
                    result = dict(result)
                    result["dismissed_source_url"] = source_url

            self._send_json(result, status)
        except ImportError as e:
            self._send_json(
                {
                    "ok": False,
                    "error": (
                        f"send_email module not available: {e}. "
                        "Run `python3 _system/scripts/gmail_client.py --authorize` "
                        "once to complete OAuth setup."
                    ),
                },
                500,
            )
        except Exception as e:
            self._send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                500,
            )

    # ── /api/send-reply ─────────────────────────────────────────────
    def _handle_send_reply(
        self, *, thread_id, to, subject, body,
        in_reply_to, references, attachments,
        uploaded_attachments=None,
    ):
        """Send a reply inside an existing Gmail thread.

        Wraps `send_email.send_reply` — dry_run / rate_limit / signature
        injection / send log are all enforced by the same policy layer
        used for first-touch outreach.

        `attachments` is the list of canonical attachments (press kit,
        fact sheet) checked in the dashboard. `uploaded_attachments` is
        the list of base64-encoded files the user drag/dropped onto the
        reply card on top of (or instead of) the canonical ones. The
        two lists are concatenated before being handed to send_reply.
        """
        if not thread_id or not to or not subject or not body:
            self._send_json(
                {"ok": False, "error":
                 "Missing 'thread_id', 'to', 'subject', or 'body'"},
                400,
            )
            return
        try:
            extra_paths = self._persist_uploaded_attachments(
                uploaded_attachments
            )
        except ValueError as e:
            self._send_json(
                {"ok": False, "error": f"Attachment error: {e}"},
                400,
            )
            return
        combined_attachments = list(attachments or []) + extra_paths
        try:
            from send_email import send_reply
            result = send_reply(
                to=to,
                subject=subject,
                body=body,
                thread_id=thread_id,
                in_reply_to=in_reply_to,
                references=references,
                attachments=combined_attachments or None,
            )
            if result.get("ok"):
                status = 200
                # On successful real send, move the draft aside so the
                # dashboard stops showing it as pending.
                if not result.get("dry_run"):
                    try:
                        self._archive_reply_draft(thread_id, sent=True)
                    except Exception as arc_e:  # noqa: BLE001
                        print(
                            f"  [_handle_send_reply] archive warning: {arc_e}"
                        )
            elif result.get("reason") == "rate_limited":
                status = 429
            elif result.get("reason") == "missing_attachment":
                status = 400
            elif result.get("reason") == "blacklisted":
                status = 409  # Conflict — address known-bad, don't retry
            else:
                status = 500
            self._send_json(result, status)
        except ImportError as e:
            self._send_json(
                {"ok": False,
                 "error": f"send_email module not available: {e}."},
                500,
            )
        except Exception as e:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                500,
            )

    # ── /api/scan-replies ───────────────────────────────────────────
    def _handle_scan_replies(self, *, with_drafts=True):
        """Invoke `reply_monitor.py` (optionally with --with-drafts).

        Returns a small summary — used by the dashboard's "Scan replies
        now" button so the operator can refresh on demand without
        leaving the UI.
        """
        monitor = SCRIPT_DIR / "reply_monitor.py"
        if not monitor.exists():
            self._send_json(
                {"ok": False, "error": f"reply_monitor.py not found at {monitor}"},
                500,
            )
            return
        cmd = [sys.executable, str(monitor)]
        if with_drafts:
            cmd.append("--with-drafts")
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(SCRIPT_DIR),
                capture_output=True,
                text=True,
                timeout=180,
            )
            self._send_json({
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-4000:],
                "stderr": proc.stderr[-2000:],
            })
        except subprocess.TimeoutExpired:
            self._send_json(
                {"ok": False, "error": "reply_monitor timed out (180s)"},
                504,
            )
        except Exception as e:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                500,
            )

    # ── /api/redraft-reply ──────────────────────────────────────────
    def _handle_redraft_reply(self, thread_id):
        """Re-run `reply_drafter.py --thread <id>` for a single thread."""
        if not thread_id:
            self._send_json({"ok": False, "error": "Missing 'thread_id'"}, 400)
            return
        drafter = SCRIPT_DIR / "reply_drafter.py"
        if not drafter.exists():
            self._send_json(
                {"ok": False, "error": f"reply_drafter.py not found at {drafter}"},
                500,
            )
            return
        try:
            proc = subprocess.run(
                [sys.executable, str(drafter), "--thread", thread_id, "--verbose"],
                cwd=str(SCRIPT_DIR),
                capture_output=True,
                text=True,
                timeout=120,
            )
            self._send_json({
                "ok": proc.returncode == 0,
                "returncode": proc.returncode,
                "stdout": proc.stdout[-2000:],
                "stderr": proc.stderr[-1000:],
            })
        except subprocess.TimeoutExpired:
            self._send_json(
                {"ok": False, "error": "reply_drafter timed out"},
                504,
            )
        except Exception as e:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                500,
            )

    # ── /api/dismiss-reply ──────────────────────────────────────────
    def _handle_dismiss_reply(self, thread_id):
        """Archive a draft into `_drafts/email_replies/_dismissed/` so the
        dashboard stops surfacing it. Reversible — the file is moved,
        not deleted."""
        if not thread_id:
            self._send_json({"ok": False, "error": "Missing 'thread_id'"}, 400)
            return
        try:
            self._archive_reply_draft(thread_id, sent=False)
            self._send_json({"ok": True, "thread_id": thread_id})
        except FileNotFoundError:
            self._send_json(
                {"ok": False, "error": f"No draft for thread {thread_id}"},
                404,
            )
        except Exception as e:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                500,
            )

    # ── /api/dismiss-radar-item ─────────────────────────────────────
    def _handle_reddit_comment(self, url, text):
        """Pubblica un commento approvato sul thread Reddit via API
        ufficiale (reddit_client). Dopo il successo l'URL viene marcato
        dismissed così il radar non lo ripropone."""
        if not url or not text.strip():
            self._send_json({"ok": False, "error": "url o testo mancante"}, 400)
            return
        try:
            sys.path.insert(0, str(SCRIPT_DIR))
            import reddit_client
            result = reddit_client.post_comment(url, text)
        except Exception as e:  # noqa: BLE001
            self._send_json({"ok": False,
                             "error": f"{type(e).__name__}: {e}"}, 500)
            return

        if result.get("ok"):
            # Marca l'URL come gestito (stesso meccanismo del dismiss)
            try:
                self._mark_url_dismissed_quiet(
                    url, note=f"reddit comment: {result.get('comment_url','')}")
            except Exception:  # noqa: BLE001
                pass
            self._send_json(result)
        else:
            status = 400 if result.get("needs_setup") else 502
            self._send_json(result, status)

    def _mark_url_dismissed_quiet(self, url, note=""):
        """Aggiunge l'URL a previously_reported.json (user_dismissed)
        senza passare dall'endpoint HTTP del dismiss."""
        dedup_path = SYSTEM_DIR / "radar" / "previously_reported.json"
        try:
            data = json.loads(dedup_path.read_text(encoding="utf-8")) \
                if dedup_path.exists() else {"reported": []}
        except (OSError, json.JSONDecodeError):
            data = {"reported": []}
        data.setdefault("reported", []).append({
            "date_first_reported": datetime.now().strftime("%Y-%m-%d"),
            "source": "user_dismissed",
            "title": "",
            "score": None,
            "cluster": None,
            "action_type": "user_dismissed",
            "url": url,
            "note": note,
        })
        dedup_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8")

    def _handle_dismiss_radar_item(self, url):
        """Permanently dismiss a Radar News item by URL.

        Appends the URL to `_system/radar/previously_reported.json` with
        action_type='user_dismissed' so:
          1. radar.py excludes it from every future scan (existing dedup
             machinery already honors the URL set in that file).
          2. scan_radar_opportunities() in this server filters it out of
             the current dashboard view immediately, so a page refresh
             confirms the dismissal.

        Reversible — the operator can edit the JSON to remove the entry.
        """
        url = (url or "").strip()
        if not url:
            self._send_json({"ok": False, "error": "missing 'url'"}, 400)
            return

        dedup_path = SYSTEM_DIR / "radar" / "previously_reported.json"
        try:
            if dedup_path.exists():
                data = json.loads(dedup_path.read_text(encoding="utf-8"))
            else:
                data = {"reported_articles": []}

            articles = data.setdefault("reported_articles", [])
            # Skip duplicate entries — idempotent.
            if any(a.get("url") == url for a in articles):
                self._send_json({"ok": True, "message": "already dismissed",
                                 "url": url})
                return

            articles.append({
                "date_first_reported": datetime.now().strftime("%Y-%m-%d"),
                "source": "user_dismissed",
                "title": "",
                "score": None,
                "cluster": None,
                "action_type": "user_dismissed",
                "url": url,
            })

            # Atomic write so a partial failure can't corrupt the dedup file.
            tmp = dedup_path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(dedup_path)

            print(f"  [dismiss-radar] {url}")
            self._send_json({"ok": True, "url": url,
                             "message": "Dismissed (will not reappear)"})
        except Exception as e:  # noqa: BLE001
            print(f"  [dismiss-radar] error: {type(e).__name__}: {e}")
            self._send_json({"ok": False, "error": str(e)}, 500)

    # ── /api/generate-ig-companion ──────────────────────────────────
    def _handle_generate_ig_companion(self, filename, *, force=False):
        """Shell out to generate_ig_companion.py for a journal draft.

        The script reads _drafts/journal/<slug>.json, calls Anthropic,
        writes _drafts/journal/<slug>.ig.md. We then read that file
        back, strip the frontmatter, and return the body so the JS can
        drop it into the inline editor.

        `force=True` overwrites an existing companion; default skips
        regen if the file already exists (used by the on-demand
        "Genera" empty-state button which only fires when there's no
        companion yet).
        """
        if not filename:
            self._send_json({"ok": False, "error": "missing 'file'"}, 400)
            return
        safe = Path(filename).name
        # Strip suffix to find the journal stem the script expects.
        if safe.endswith(".json"):
            stem = safe[:-5]
        elif safe.endswith(".html"):
            stem = safe[:-5]
        else:
            stem = Path(safe).stem
        journal_dir = DRAFTS_DIR / "journal"
        article_json = journal_dir / f"{stem}.json"
        if not article_json.exists():
            self._send_json(
                {"ok": False, "error": f"sidecar not found: {article_json.name}"},
                404,
            )
            return
        companion_path = journal_dir / f"{stem}.ig.md"
        if companion_path.exists() and not force:
            # Idempotent: return what's already there.
            self._send_json({
                "ok": True,
                "body": self._read_ig_companion_body(companion_path),
                "cached": True,
            })
            return
        # Shell out — the script handles dotenv + Anthropic itself.
        try:
            result = subprocess.run(
                [sys.executable, str(SCRIPT_DIR / "generate_ig_companion.py"),
                 "--article", str(article_json),
                 "--output",  str(companion_path)],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                err = (result.stderr or result.stdout or "").strip()[:500]
                self._send_json(
                    {"ok": False, "error": f"generator failed: {err}"},
                    500,
                )
                return
            self._send_json({
                "ok": True,
                "body": self._read_ig_companion_body(companion_path),
            })
        except subprocess.TimeoutExpired:
            self._send_json(
                {"ok": False, "error": "generator timed out (60s)"},
                504,
            )
        except Exception as e:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                500,
            )

    def _read_ig_companion_body(self, path):
        """Read the .ig.md file and return just the body (no frontmatter)."""
        try:
            raw = path.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n.*?\n---\s*\n+(.*)", raw, re.DOTALL)
            return (m.group(1) if m else raw).strip()
        except Exception:
            return ""

    # ── /api/save-ig-companion ──────────────────────────────────────
    def _handle_save_ig_companion(self, filename, body):
        """Persist an edit to the journal article's IG companion .ig.md.

        Preserves the YAML frontmatter at the top of the file (if any)
        and only updates the body below the second `---` separator.
        Char_count in the frontmatter is also refreshed.
        """
        if not filename:
            self._send_json({"ok": False, "error": "missing 'file'"}, 400)
            return
        safe = Path(filename).name
        if safe.endswith(".json") or safe.endswith(".html"):
            stem = safe[:-5]
        else:
            stem = Path(safe).stem
        companion_path = DRAFTS_DIR / "journal" / f"{stem}.ig.md"
        if not companion_path.exists():
            self._send_json(
                {"ok": False, "error": f"companion not found: {companion_path.name}"},
                404,
            )
            return
        try:
            raw = companion_path.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if m:
                frontmatter = m.group(1)
                # Refresh char_count in the frontmatter so downstream
                # readers stay in sync with the edited body.
                new_count = len((body or "").strip())
                frontmatter = re.sub(
                    r"(?m)^char_count:\s*\d+\s*$",
                    f"char_count: {new_count}",
                    frontmatter,
                )
                new_raw = f"---\n{frontmatter}\n---\n\n{(body or '').strip()}\n"
            else:
                # File had no frontmatter — overwrite as plain text.
                new_raw = (body or "").strip() + "\n"
            companion_path.write_text(new_raw, encoding="utf-8")
            self._send_json({"ok": True, "char_count": len((body or "").strip())})
        except Exception as e:  # noqa: BLE001
            self._send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"},
                500,
            )

    def _archive_reply_draft(self, thread_id, *, sent):
        """Move `_drafts/email_replies/<id>.json` aside.

        `sent=True` → `_dismissed/sent/<id>.json` (a record of fulfilled
        threads). `sent=False` → `_dismissed/<id>.json`.
        """
        src = REPLY_DRAFTS_DIR / f"{thread_id}.json"
        if not src.exists():
            raise FileNotFoundError(src)
        dst_dir = REPLIES_DISMISSED_DIR / ("sent" if sent else "")
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst_dir / src.name))

    # ════════════════════════════════════════════════════════════════
    # Editorial IG handlers (Phase 1 — draft-only mode)
    # All shell out to dedicated CLI scripts so the dashboard stays a
    # thin orchestrator. Failures bubble up as JSON errors to the UI.
    # ════════════════════════════════════════════════════════════════

    def _editorial_draft_path(self, filename):
        """Resolve a safe path inside _drafts/social_editorial/."""
        safe = Path(filename).name
        if not safe.endswith(".md"):
            return None
        path = DRAFTS_DIR / "social_editorial" / safe
        if not path.exists() or not path.is_file():
            return None
        return path

    def _handle_editorial_save(self, filename, body):
        """Overwrite the body of a draft .md AND keep frontmatter coherent.

        After saving, we recompute from the new body:
          - char_count   = caption length without the trailing hashtag line
          - hashtags     = parsed from the trailing #tag line, in order
          - warning_forbidden_terms = any hit against the calendar config's
            forbidden_terms list (case-insensitive)
        Other frontmatter fields (date, pillar, image_*, etc.) are preserved.
        """
        path = self._editorial_draft_path(filename)
        if not path:
            self._send_json({"ok": False, "error": "draft not found"}, 404)
            return
        try:
            import yaml as _yaml
            raw = path.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
            if not m:
                self._send_json({"ok": False, "error": "missing frontmatter"}, 400)
                return
            try:
                fm = _yaml.safe_load(m.group(1)) or {}
            except Exception:
                self._send_json({"ok": False, "error": "yaml parse failed"}, 400)
                return

            new_body = body.strip()

            # Split caption from trailing hashtag line. The convention is:
            #   <caption text>
            #   <blank line>
            #   #Tag1 #Tag2 …      ← the hashtag line, optional
            parts = new_body.split("\n\n")
            hashtag_line = ""
            if len(parts) >= 2 and parts[-1].strip().startswith("#"):
                hashtag_line = parts[-1].strip()
                caption_only = "\n\n".join(parts[:-1]).strip()
            else:
                caption_only = new_body

            # Parse hashtags in order, dedup case-insensitively.
            seen = set()
            hashtags = []
            for tag in re.findall(r"#(\w+)", hashtag_line):
                k = tag.lower()
                if k in seen:
                    continue
                seen.add(k)
                hashtags.append(tag)

            # Forbidden-term check (substring match, lowercase)
            forbidden_warnings = []
            try:
                cfg_path = SYSTEM_DIR / "config" / "editorial-calendar.yml"
                cfg = _yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}
                forbidden = (cfg.get("constraints", {}) or {}).get("forbidden_terms", []) or []
                low = caption_only.lower()
                for term in forbidden:
                    if term and term.lower() in low:
                        forbidden_warnings.append(term)
            except Exception:
                pass

            fm["char_count"] = len(caption_only)
            fm["hashtags"] = hashtags
            if forbidden_warnings:
                fm["warning_forbidden_terms"] = forbidden_warnings
            else:
                fm.pop("warning_forbidden_terms", None)
            fm["last_edited_at"] = datetime.now().isoformat(timespec="seconds")

            new_fm = _yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
            new_md = "---\n" + new_fm + "---\n\n" + new_body + "\n"
            path.write_text(new_md, encoding="utf-8")

            self._send_json({
                "ok": True,
                "char_count": fm["char_count"],
                "hashtag_count": len(hashtags),
                "forbidden_warnings": forbidden_warnings,
            })
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_editorial_discard(self, filename):
        path = self._editorial_draft_path(filename)
        if not path:
            self._send_json({"ok": False, "error": "draft not found"}, 404)
            return
        try:
            # Soft-delete: move to _dismissed/ rather than unlink.
            dismissed = path.parent / "_dismissed"
            dismissed.mkdir(exist_ok=True)
            shutil.move(str(path), str(dismissed / path.name))
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_editorial_build_package(self, filename):
        """Shell out to publish_instagram.py to build a publish-ready folder."""
        path = self._editorial_draft_path(filename)
        if not path:
            self._send_json({"ok": False, "error": "draft not found"}, 404)
            return
        script = SCRIPT_DIR / "publish_instagram.py"
        if not script.exists():
            self._send_json({"ok": False, "error": "publish_instagram.py missing"}, 500)
            return
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--draft", path.name],
                capture_output=True, text=True, timeout=60,
                cwd=str(ROOT_DIR),
            )
            if result.returncode != 0:
                self._send_json({
                    "ok": False,
                    "error": result.stderr.strip() or "publish_instagram.py failed",
                    "stdout": result.stdout,
                }, 500)
                return
            # Extract package path from stdout (line starting with "  ✓ Package →")
            pkg_path = ""
            for line in result.stdout.splitlines():
                if "Package →" in line:
                    pkg_path = line.split("→", 1)[1].strip()
                    break
            self._send_json({
                "ok": True,
                "package_path": pkg_path,
                "stdout": result.stdout,
            })
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "build timeout"}, 504)
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_editorial_mark_published(self, filename, ig_post_url):
        """Move draft to published/ and update calendar slot status."""
        path = self._editorial_draft_path(filename)
        if not path:
            self._send_json({"ok": False, "error": "draft not found"}, 404)
            return
        script = SCRIPT_DIR / "publish_instagram.py"
        try:
            cmd = [sys.executable, str(script), "--mark-published", path.name]
            if ig_post_url:
                cmd += ["--ig-post-url", ig_post_url]
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=30, cwd=str(ROOT_DIR),
            )
            if result.returncode != 0:
                self._send_json({
                    "ok": False,
                    "error": result.stderr.strip() or "mark-published failed",
                }, 500)
                return
            self._send_json({"ok": True, "stdout": result.stdout})
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_editorial_regenerate(self, filename):
        """Re-run editorial_generator.py for the slot tied to this draft."""
        path = self._editorial_draft_path(filename)
        if not path:
            self._send_json({"ok": False, "error": "draft not found"}, 404)
            return
        # Parse the draft's date+slug to scope the regeneration
        try:
            raw = path.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            if not m:
                self._send_json({"ok": False, "error": "missing frontmatter"}, 400)
                return
            try:
                import yaml as _yaml
                fm = _yaml.safe_load(m.group(1)) or {}
            except Exception:
                self._send_json({"ok": False, "error": "yaml parse failed"}, 400)
                return
            slot_date = str(fm.get("date") or "")
            if not slot_date:
                self._send_json({"ok": False, "error": "date not in frontmatter"}, 400)
                return
            month = slot_date[:7]
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
            return

        script = SCRIPT_DIR / "editorial_generator.py"
        if not script.exists():
            self._send_json({"ok": False, "error": "editorial_generator.py missing"}, 500)
            return
        try:
            result = subprocess.run(
                [sys.executable, str(script),
                 "--month", month, "--slot", slot_date, "--force"],
                capture_output=True, text=True, timeout=120, cwd=str(ROOT_DIR),
            )
            if result.returncode != 0:
                self._send_json({
                    "ok": False,
                    "error": result.stderr.strip() or "regenerate failed",
                    "stdout": result.stdout,
                }, 500)
                return
            self._send_json({"ok": True, "stdout": result.stdout})
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "regenerate timeout"}, 504)
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_editorial_plan(self, month):
        """Run editorial_planner.py for a given YYYY-MM month."""
        if not re.match(r"^\d{4}-\d{2}$", month or ""):
            self._send_json({"ok": False, "error": "month must be YYYY-MM"}, 400)
            return
        script = SCRIPT_DIR / "editorial_planner.py"
        if not script.exists():
            self._send_json({"ok": False, "error": "editorial_planner.py missing"}, 500)
            return
        try:
            result = subprocess.run(
                [sys.executable, str(script), "--month", month, "--force"],
                capture_output=True, text=True, timeout=30, cwd=str(ROOT_DIR),
            )
            if result.returncode != 0:
                self._send_json({
                    "ok": False,
                    "error": result.stderr.strip() or "plan failed",
                }, 500)
                return
            # Try to parse "Wrote N slots" from stdout
            slot_count = 0
            for line in result.stdout.splitlines():
                m = re.search(r"Wrote (\d+) slots", line)
                if m:
                    slot_count = int(m.group(1))
                    break
            self._send_json({
                "ok": True,
                "slot_count": slot_count,
                "stdout": result.stdout,
            })
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "plan timeout"}, 504)
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    # ── Editorial image picker handlers ─────────────────────────────

    # Whitelist /img/ assets that are editorial-grade. Mirrors the
    # blacklist in editorial_generator.EDITORIAL_BLACKLIST. Used by the
    # image picker modal so the UI offers only on-brand images.
    _EDITORIAL_IMG_BLACKLIST = {
        "structural-diagram.svg",
        "structural.webp",
        "tectonics.webp",
        "resilient.webp",
        "resilience-climate.webp",
        "resilience-energy.webp",
        "biophilic.webp",
        "biophilic-design.webp",
        "biophilic-design.png",
        "material-permanence.webp",
        "material-permanence.png",
    }

    def _handle_editorial_list_image_candidates(self, filename):
        """Return JSON with image candidates for the picker UI:
          - partner_post_thumbnails: cover + slides of the draft's own
            partner post (if any), so a Sidecar post lets you pick any slide
          - partner_other_posts: cover thumbnails of OTHER cached posts for
            the same partner, plus other partners' covers (cross-pollination)
          - local_assets: editorial-grade /img/ assets (blacklist filtered)
          - current_web_path: the draft's current image_web_path
        """
        try:
            import yaml as _yaml
        except ImportError:
            self._send_json({"ok": False, "error": "pyyaml missing"}, 500)
            return

        path = self._editorial_draft_path(filename)
        if not path:
            self._send_json({"ok": False, "error": "draft not found"}, 404)
            return
        try:
            raw = path.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n", raw, re.DOTALL)
            fm = _yaml.safe_load(m.group(1)) if m else {}
        except Exception as e:
            self._send_json({"ok": False, "error": f"parse: {e}"}, 500)
            return

        current_web_path = fm.get("image_web_path", "")
        partner_handle = fm.get("partner_handle", "")

        partner_post_thumbs = []
        partner_other = []
        cache_dir = SYSTEM_DIR / "social" / "partner_cache"

        # Section 1: thumbnails of THIS draft's partner post (cover + slides)
        thumbs = fm.get("partner_post_thumbnails") or []
        if isinstance(thumbs, list):
            for rel in thumbs:
                if not isinstance(rel, str) or not rel:
                    continue
                abs_path = ROOT_DIR / rel
                if not abs_path.exists():
                    continue
                partner_post_thumbs.append({
                    "web_path": "/" + rel,
                    "filename": Path(rel).name,
                })
        # Fallback: if no thumbnails persisted but we know shortcode, scan dir
        sc = fm.get("partner_post_shortcode")
        if not partner_post_thumbs and sc and partner_handle:
            handle_dir = cache_dir / partner_handle
            if handle_dir.exists():
                for f in sorted(handle_dir.glob(f"{sc}*.jpg")):
                    partner_post_thumbs.append({
                        "web_path": "/" + str(f.relative_to(ROOT_DIR)),
                        "filename": f.name,
                    })

        # Section 2: GROUPED by post — cover + collapsible slides per post.
        # Skip the post we're already echoing (it's section 1).
        # Returns: list of {handle, shortcode, cover, slides[]} dicts.
        # `slides` excludes the cover (only the -2/-3/... files).
        if cache_dir.exists():
            current_sc = (sc or "").strip()
            for handle_dir in sorted(cache_dir.iterdir()):
                if not handle_dir.is_dir():
                    continue
                # Group all .jpg files by shortcode
                by_sc = {}
                for f in sorted(handle_dir.glob("*.jpg")):
                    parts = f.stem.split("-", 1)
                    sc_id = parts[0]
                    by_sc.setdefault(sc_id, []).append(f)
                for sc_id, files in by_sc.items():
                    if sc_id == current_sc:
                        continue
                    # Sort: cover (no `-`) first, then -2, -3, ...
                    cover = next((f for f in files if "-" not in f.stem), None)
                    if cover is None:
                        continue
                    slide_files = [f for f in files if "-" in f.stem]
                    slide_files.sort(key=lambda f: int(f.stem.split("-")[1])
                                     if f.stem.split("-")[1].isdigit() else 999)
                    partner_other.append({
                        "handle": handle_dir.name,
                        "shortcode": sc_id,
                        "cover": {
                            "web_path": "/" + str(cover.relative_to(ROOT_DIR)),
                            "filename": cover.name,
                        },
                        "slides": [{
                            "web_path": "/" + str(f.relative_to(ROOT_DIR)),
                            "filename": f.name,
                        } for f in slide_files],
                    })
        # Cap posts (not images) to avoid an overwhelming modal. 30 is plenty.
        partner_other = partner_other[:30]

        # Section 3: editorial-grade /img/ assets
        local_assets = []
        img_dir = ROOT_DIR / "img"
        if img_dir.exists():
            for f in sorted(img_dir.iterdir()):
                if f.suffix.lower() not in (".webp", ".jpg", ".jpeg", ".png"):
                    continue
                if f.name in self._EDITORIAL_IMG_BLACKLIST:
                    continue
                local_assets.append({
                    "web_path": f"/img/{f.name}",
                    "filename": f.name,
                })

        self._send_json({
            "ok": True,
            "current_web_path": current_web_path,
            "partner_post_thumbnails": partner_post_thumbs,
            "partner_other_posts": partner_other,
            "local_assets": local_assets,
        })

    def _handle_editorial_swap_image(self, filename, image_web_path, image_filename):
        """Update a draft's image_web_path + image_filename in frontmatter."""
        try:
            import yaml as _yaml
        except ImportError:
            self._send_json({"ok": False, "error": "pyyaml missing"}, 500)
            return

        path = self._editorial_draft_path(filename)
        if not path:
            self._send_json({"ok": False, "error": "draft not found"}, 404)
            return
        if not image_web_path or not image_filename:
            self._send_json({"ok": False, "error": "missing image fields"}, 400)
            return

        # Sanity: the resolved path must exist on disk under ROOT_DIR.
        rel = image_web_path.lstrip("/")
        candidate = (ROOT_DIR / rel).resolve()
        try:
            candidate.relative_to(ROOT_DIR.resolve())
        except ValueError:
            self._send_json({"ok": False, "error": "path traversal blocked"}, 400)
            return
        if not candidate.exists() or not candidate.is_file():
            self._send_json({"ok": False, "error": "image file not found on disk"}, 404)
            return

        try:
            raw = path.read_text(encoding="utf-8")
            m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)", raw, re.DOTALL)
            if not m:
                self._send_json({"ok": False, "error": "missing frontmatter"}, 400)
                return
            fm = _yaml.safe_load(m.group(1)) or {}
            body = m.group(2)

            fm["image_web_path"] = image_web_path
            fm["image_filename"] = image_filename
            fm["image_swapped_at"] = datetime.now().isoformat(timespec="seconds")
            # Mark image_source: helps reconstruct provenance
            if "/_system/social/partner_cache/" in image_web_path:
                fm["image_source"] = "partner_picker_swap"
            elif image_web_path.startswith("/img/"):
                fm["image_source"] = "local_picker_swap"

            new_md = "---\n" + _yaml.safe_dump(fm, sort_keys=False, allow_unicode=True) + "---\n\n" + body.lstrip("\n")
            path.write_text(new_md, encoding="utf-8")
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _handle_editorial_generate(self, month, days_ahead):
        """Run editorial_generator.py to materialize next-N-days drafts."""
        if not re.match(r"^\d{4}-\d{2}$", month or ""):
            self._send_json({"ok": False, "error": "month must be YYYY-MM"}, 400)
            return
        script = SCRIPT_DIR / "editorial_generator.py"
        if not script.exists():
            self._send_json({"ok": False, "error": "editorial_generator.py missing"}, 500)
            return
        try:
            result = subprocess.run(
                [sys.executable, str(script),
                 "--month", month, "--days-ahead", str(days_ahead)],
                capture_output=True, text=True, timeout=300, cwd=str(ROOT_DIR),
            )
            if result.returncode != 0:
                self._send_json({
                    "ok": False,
                    "error": result.stderr.strip() or "generate failed",
                    "stdout": result.stdout,
                }, 500)
                return
            generated = sum(1 for ln in result.stdout.splitlines() if "Draft →" in ln)
            self._send_json({
                "ok": True,
                "generated": generated,
                "stdout": result.stdout,
            })
        except subprocess.TimeoutExpired:
            self._send_json({"ok": False, "error": "generate timeout"}, 504)
        except Exception as e:
            self._send_json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)


# ── Main ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="My Villa — Content Review Server"
    )
    parser.add_argument("--port", type=int, default=8787, help="Port (default 8787)")
    parser.add_argument(
        "--no-browser", action="store_true", help="Don't auto-open browser"
    )
    parser.add_argument(
        "--no-auto-archive", action="store_true",
        help=f"Skip the startup sweep that moves drafts older than "
             f"{AUTO_ARCHIVE_DAYS} days into _archive/. Useful for "
             f"debugging or one-off runs.",
    )
    parser.add_argument(
        "--archive-days", type=int, default=-1,
        help="Override the per-type auto-archive threshold with a single "
             "value applied across all draft types (journal/social/replies). "
             "Defaults to -1 = use per-type thresholds (journal=14d, "
             "social=7d, replies=14d). Use this only for one-off compactions "
             "(e.g. --archive-days 3 before a demo).",
    )
    parser.add_argument(
        "--no-auto-ig", action="store_true",
        help="Skip the startup pass that auto-generates IG companions "
             "for journal drafts. Useful for offline / no-API runs.",
    )
    args = parser.parse_args()

    # Auto-archive: prune stale drafts before the dashboard renders, so
    # the operator only sees recent work. Non-destructive — everything
    # goes to _archive/, never deleted.
    if not args.no_auto_archive:
        print("  Auto-archiving stale drafts...")
        # Pass None to use per-type defaults (journal=14, social=7,
        # replies=14). The CLI flag is only honored when explicitly
        # given by the operator (default is the sentinel below).
        threshold_override = args.archive_days if args.archive_days != -1 else None
        summary = auto_archive_old_drafts(threshold_days=threshold_override)
        if not summary:
            print(f"  [auto-archive] nothing to move "
                  f"(per-type thresholds: journal=14d, social=1d, replies=14d).")
        print()

    # Auto-generate IG companions for journal drafts that don't have one
    # yet. The operator should never have to click "Genera" — the IG
    # preview is always pre-populated by the time the dashboard loads.
    if not args.no_auto_ig:
        print("  Auto-generating missing IG companions...")
        ig_summary = auto_generate_ig_companions()
        if not ig_summary:
            print("  [ig-companion] all journal drafts already have a companion.")
        print()

    # Find a free port starting from --port (retries up to 10 ports)
    server = None
    chosen_port = args.port
    for attempt in range(10):
        try:
            addr = ("127.0.0.1", chosen_port)
            server = ThreadingHTTPServer(addr, ReviewHandler)
            break
        except OSError as e:
            if e.errno == 48:  # Address already in use
                print(f"  Port {chosen_port} in use, trying {chosen_port + 1}...")
                chosen_port += 1
                continue
            raise
    if server is None:
        print(f"  ERROR: could not bind to ports {args.port}-{args.port + 9}")
        sys.exit(1)

    url = f"http://127.0.0.1:{chosen_port}"
    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   My Villa — Content Review Server       ║")
    print(f"  ║   {url:<39s}║")
    print("  ║   Press Ctrl+C to stop                   ║")
    print("  ╚══════════════════════════════════════════╝")
    print()

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Shutting down review server...")
        server.shutdown()
        print("  Done.")


if __name__ == "__main__":
    main()
