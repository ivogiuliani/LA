# Reddit — Setup commenti automatici (3 minuti)

Il pannello può pubblicare i commenti approvati direttamente
sull'account Reddit di My Villa, via API ufficiale.

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
# REDDIT_DAILY_CAP=5        # tetto commenti/24h (default 5)
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

## 4. Uso dal pannello

Sezione **🔥 Commenti ai post virali** → card Reddit → modifica il
testo se serve → **🚀 Commenta su Reddit** → conferma → live.
L'URL viene marcato come gestito (non riproposto dal radar).

## Avvertenze (Reddit non perdona)

- **Cap 5/giorno** di default — su Reddit la qualità batte il volume:
  2-3 commenti di valore al giorno sono il massimo sostenibile.
- **Account nuovo / karma basso**: AutoModerator di molti subreddit
  rimuove automaticamente i commenti di account giovani. I primi
  commenti potrebbero non apparire — è normale, serve costruire
  karma con attività genuina.
- **Mai promozionale**: il prompt genera commenti di valore, ma
  rileggi sempre — un commento percepito come spam = ban dal
  subreddit + danno reputazionale.
