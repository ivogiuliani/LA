# Reddit — Setup (commenti + submission articoli)

Il pannello può, via API ufficiale Reddit, sia pubblicare i **commenti**
approvati ai thread virali altrui, sia **submittare i nostri articoli**
come self-post nei subreddit in allowlist. Entrambi con l'account My Villa,
entrambi con **approvazione manuale** (un click esplicito, niente autopilot).

## 1. Crea l'app "script" (sull'account che commenterà)

1. Login su Reddit con l'account My Villa
2. Vai su <https://www.reddit.com/prefs/apps>
3. In fondo: **create another app...**
   - name: `myvilla-radar`
   - tipo: **script** ← importante
   - redirect uri: `http://localhost:8080` (non usato, ma obbligatorio)
4. **create app** → prendi nota di:
   - **client_id**: la stringa corta SOTTO il nome dell'app
   - **secret**: il campo "secret"

## 2. Aggiungi a `.env` (lo fai tu, contiene la password)

```
REDDIT_CLIENT_ID=<client_id>
REDDIT_CLIENT_SECRET=<secret>
REDDIT_USERNAME=<username dell'account>
REDDIT_PASSWORD=<password dell'account>
# REDDIT_DAILY_CAP=5          # tetto commenti/24h (default 5)
# REDDIT_SUBMIT_DAILY_CAP=1   # tetto submission articoli/24h (default 1)
```

Nota: il "password grant" è il flusso UFFICIALE Reddit per le app
script personali. Se l'account ha la **2FA**, il password grant non
funziona: o disattivi la 2FA, o usi `password:codice2fa` (scomodo) —
meglio un account dedicato senza 2FA.

## 3. Verifica

```bash
cd ~/Code/myvilla-la
python3 _system/scripts/reddit_client.py --whoami
```

Atteso: `{"ok": true, "username": "...", "karma": ...}`.

## 4. Commenti — uso dal pannello

Sezione **🔥 Commenti ai post virali** → card Reddit → modifica il
testo se serve → **🚀 Commenta su Reddit** → conferma → live.
L'URL viene marcato come gestito (non riproposto dal radar).

## 5. Submission articoli (self-post) — uso dal pannello

Genera i draft (di solito dai nostri articoli Journal). È **opt-in**:
il rail quotidiano NON genera submission da solo, serve `--channels reddit`.

```bash
cd ~/Code/myvilla-la
python3 _system/scripts/generate_social.py \
  --articles _drafts/journal/<articolo>.json --channels reddit
```

Nel pannello compare la sezione **🔥 Reddit — self-post dei nostri
articoli**: scegli il **subreddit** (dall'allowlist), aggiusta **titolo**
e **corpo**, poi **🚀 Pubblica su Reddit** → conferma → live. Il draft
viene archiviato in `_system/social/posts/published/` (non riproposto).

L'allowlist per le submission è in `_system/config/radar-keywords.yml` →
`reddit.submission_subreddits` (DIVERSA dalla lista di ascolto: solo i
sub "discussione" che tollerano un self-post di valore).

## Avvertenze (Reddit non perdona)

- **Cap commenti 5/giorno** di default — su Reddit la qualità batte il
  volume: 2-3 commenti di valore al giorno sono il massimo sostenibile.
- **Account nuovo / karma basso**: AutoModerator di molti subreddit
  rimuove automaticamente i commenti di account giovani. I primi
  commenti potrebbero non apparire — è normale, serve costruire
  karma con attività genuina.
- **Submission = il rischio massimo**: postare propri articoli è la cosa
  più ban-prone. Cap 1/giorno di default. Su account nuovo aspettati
  rimozioni finché non c'è karma — **prima costruisci reputazione con i
  commenti**, poi prova qualche submission.
- **Self-post, non link nudo**: il link all'articolo va NEL corpo,
  contestualizzato. Un link di brand secco viene rimosso quasi ovunque.
- **Mai nei sub immagine/arte** (architecture, InteriorDesign,
  ArchitecturePorn…): lì il blogspam di brand è bannato. L'allowlist
  li esclude apposta.
- **Mai promozionale**: il prompt genera contenuti di valore, ma
  rileggi sempre — percepito come spam = ban dal subreddit + danno
  reputazionale all'account.
