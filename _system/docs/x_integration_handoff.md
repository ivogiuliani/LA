# X (Twitter) — Handoff alla sessione radar (fase codice)

Setup (account + chiavi) gestito a parte → `x_setup.md`. Questo è lo spec del
**codice** da scrivere nel repo runtime. Scope: **Entrambi** (pubblicazione +
ascolto). Consiglio di ordine: prima il **publisher** (più semplice, template
IG già pronto), poi la **fonte radar**.

Prerequisito: `.env` con `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` /
`X_ACCESS_TOKEN_SECRET` (vedi `x_setup.md`).

---

## ✅ STATO 2026-06-13 — implementato direttamente (non più "da fare")

Quasi tutto fatto in questa sessione; lo spec sotto resta come riferimento.

- **Publisher CLI**: `_system/scripts/x_publisher.py` creato — OAuth1 stdlib
  (no tweepy), `--whoami` / `--text` / `--draft` / `--dir` / `--publish-live`
  / `--dry-run`, daily cap `X_DAILY_CAP`, mark-published in `social/posts/published/`.
  **Auth verificata** (`GET /2/users/me` → 200, @myvilla_la). Posting (POST)
  da confermare con un primo tweet reale.
- **Dashboard (approve.py)**: il wiring **esisteva già** (`/api/publish_social`
  → `publish_social.publish_to_x`). Era rotto da un **mismatch di naming**:
  leggeva `X_ACCESS_SECRET` invece di `X_ACCESS_TOKEN_SECRET`. **Corretto**
  in `publish_social.py` (con fallback). `check_credentials()` ora dà x=configured.
- **Ascolto**: **NON serve Apify** — `radar.py:grok_x_search()` già cerca X via
  Grok (xAI `x_search`, usa `XAI_API_KEY`), con autore/engagement/virality.
  La sezione B qui sotto è quindi **superata** (lasciata solo come nota).
- **Da fare**: (a) primo POST reale per validare la scrittura; (b) opzionale:
  aggiungere il batch X a `daily_publish.sh`/launchd per full automation;
  (c) le 4 `X_*` nei secrets GitHub per il rail cloud.

---

## A. Publisher — `_system/scripts/x_publisher.py`  (chiude l'ultimo miglio)

Stato attuale: i tweet sono **già generati** (`generate_social.py`, campo
`x_post`) e passano per `approve.py`, ma si fermano a link **`x.com/intent`
manuali** (≈ `approve.py:1649`). Manca solo il posting via API — esattamente
com'era IG prima della Phase 4.

**Template da clonare:** `publish_instagram.py` (stesso `load_dotenv`, stesse
modalità stub/live, stesso `--whoami` / `--check-token` / `--dry-run`, stesso
daily-cap).

- **Auth:** OAuth 1.0a user-context. `tweepy.Client(consumer_key,
  consumer_secret, access_token, access_token_secret)` → `client.create_tweet(text=...)`.
  (In alternativa raw: `POST https://api.x.com/2/tweets` con header OAuth1.)
- **Modalità:**
  - default (stub): scrive un pacchetto pronto in
    `_system/social/posts/.../_publish_ready/` (fallback copia-incolla),
    come fa l'IG publisher.
  - `--publish-live`: posta davvero. `--dry-run`: prepara senza chiamare l'API.
  - `--whoami`: `GET /2/users/me` → stampa `@handle` + id.
  - `X_DAILY_CAP` (default 4) come `IG_DAILY_CAP`.
- **Input:** il campo `x_post` dei draft + gli item `dtype == "tweet"` in
  `approve.py`. Il testo è già ≤280 char, senza hashtag, con link Journal →
  postare as-is. Tenere il check di `validate_links.py`.
- **Wiring `approve.py`:** dove oggi genera il link `x.com/intent/tweet`
  (≈1649), aggiungere un'azione **"🚀 Post to X"** che chiama `x_publisher.py`,
  poi marca l'URL come gestito — clonare il flusso Reddit
  **"🚀 Commenta su Reddit"** (già presente in `approve.py`).
- `requirements.txt`: aggiungere `tweepy>=4.14`.
- Scheduling: includere il publish X in `daily_publish.sh` + nel plist launchd
  che già lancia l'IG publisher.

## B. Ascolto — nuova fonte `x` nel radar  (`radar.py`)

**Template da clonare:** lo scraper IG Apify già in `radar.py` (`radar.py:1193`
`instagram-hashtag-scraper`, `radar.py:1293` `instagram-scraper`).

- Nuova funzione `apify_x_search(api_key, clusters, lookback_days=7)`:
  - attore **`apidojo/tweet-scraper`** (o `xquik/x-tweet-scraper`, più economico).
    Eventuale override da `X_APIFY_ACTOR`.
  - costruire i termini di ricerca dai cluster di `radar-keywords.yml`
    (stesso `clusters` passato a `google_cse_search` / `brave_search`).
  - `POST https://api.apify.com/v2/acts/<actor>/run-sync-get-dataset-items?token=…`
    (stesso schema delle chiamate IG).
  - mappare gli item al formato comune:
    `{"source":"x", "platform":"x", "url", "title"/"snippet", "publication":"@handle",
      "engagement":{"likes","retweets","replies"}, "date"}`.
- **Registrazione** (3 punti, come le altre fonti):
  1. lista preflight `SOURCES` (≈`radar.py:249`) — riusare `_ping_apify`.
  2. orchestrazione fetch/collect dove vengono chiamate google_cse/brave/reddit.
  3. il dedup canonicalizza già gli URL `x.com` (funzione di normalizzazione
     ≈`radar.py:467`) — verificare che gli URL X passino puliti.
- Lo scoring AI (Sonnet) e l'HTML dashboard sono a valle e agnostici alla
  fonte: nessuna modifica.

## C. Verifica end-to-end

1. `python3 _system/scripts/x_publisher.py --whoami` → `✓ @MyVillaLA`.
2. `--dry-run` su un draft `x_post` reale → anteprima testo+link.
3. `--publish-live` su un draft → tweet vero, poi URL marcato gestito in `approve.py`.
4. Run radar con la fonte `x` attiva → item `source:"x"` nel JSON + nel dashboard.

## Costi (promemoria)

- Publish: free tier (~1.500/mese) o pay-per-use (~$0.01/post) → centesimi/mese.
- Ascolto Apify: ~$0.15–0.25 / 1.000 tweet, dentro il credito gratis ~$5/mese.
- **Mai** usare l'API X ufficiale per leggere (cara: Basic ~$100–200/mese).
