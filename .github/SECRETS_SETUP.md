# Setup GitHub Actions Secrets

Per far girare il workflow `daily-publish.yml` in cloud, devi caricare
i tuoi secrets (API keys + Gmail OAuth) nei **GitHub Secrets** del repo.

**Quanto dura**: 5-10 minuti, una sola volta.

## Come accedere

1. Vai su: <https://github.com/ivogiuliani/LA/settings/secrets/actions>
   (puoi cliccare "New repository secret" in alto a destra)

## Secrets da configurare

Per ogni riga qui sotto:
1. Click **"New repository secret"**
2. **Name**: copia il nome esatto della prima colonna
3. **Secret**: copia il valore dal file indicato nella seconda colonna
4. Click **"Add secret"**

### Da `~/Code/myvilla-la/.env`

Apri il file `.env` con TextEdit o vim, e copia il valore (la stringa
dopo `=`) per ogni chiave:

| Nome del secret in GitHub | Valore (vedi `.env`)            |
|----------------------------|---------------------------------|
| `ANTHROPIC_API_KEY`        | riga `ANTHROPIC_API_KEY=...`    |
| `GEMINI_API_KEY`           | riga `GEMINI_API_KEY=...`       |
| `XAI_API_KEY`              | riga `XAI_API_KEY=...`          |
| `GOOGLE_CSE_API_KEY`       | riga `GOOGLE_CSE_API_KEY=...`   |
| `GOOGLE_CSE_ENGINE_ID`     | riga `GOOGLE_CSE_ENGINE_ID=...` |
| `BRAVE_API_KEY`            | riga `BRAVE_API_KEY=...`        |
| `UNSPLASH_APPLICATION_ID`  | riga `UNSPLASH_APPLICATION_ID=...` |
| `UNSPLASH_ACCESS_KEY`      | riga `UNSPLASH_ACCESS_KEY=...`  |
| `UNSPLASH_SECRET_KEY`      | riga `UNSPLASH_SECRET_KEY=...`  |
| `APIFY_API_TOKEN`          | riga `APIFY_API_TOKEN=...`      |

### Da `~/Code/myvilla-la/_system/outreach/credentials/`

Questi 2 sono file JSON. Apri il file, **seleziona TUTTO** il contenuto
(Cmd+A), copia (Cmd+C), incolla nel campo "Secret" su GitHub.

| Nome del secret    | File da cui copiare (intero contenuto JSON)              |
|--------------------|----------------------------------------------------------|
| `GMAIL_OAUTH_CLIENT` | `_system/outreach/credentials/oauth_client.json`       |
| `GMAIL_OAUTH_TOKEN`  | `_system/outreach/credentials/token.json`              |

## Come testare dopo il setup

1. Vai su <https://github.com/ivogiuliani/LA/actions>
2. Click sul workflow **"Daily Publish Pipeline"** (sinistra)
3. Click **"Run workflow"** in alto a destra → seleziona "Dry-run: true"
   per il primo test (niente mail, niente push)
4. Aspetta 3-5 minuti, vedi i log step by step
5. Se è tutto verde, fai un secondo run con "Dry-run: false" e
   verifica che la mail digest arrivi

## Quando il workflow è verificato

Disabilita il launchd locale (non serve più, evita doppia esecuzione):

```bash
launchctl unload ~/Library/LaunchAgents/com.myvilla.daily-publish.plist
```

Il file plist puoi cancellarlo o tenerlo come archivio:

```bash
mv ~/Library/LaunchAgents/com.myvilla.daily-publish.plist \
   ~/Library/LaunchAgents/com.myvilla.daily-publish.plist.disabled
```

## Sicurezza

- I secrets sono **encrypted at rest** da GitHub. Solo i workflow del
  tuo repo possono accedervi (non altri repo, non public).
- Nei log dei workflow, i secrets appaiono mascherati come `***`.
- I file `.env` e i credentials che il workflow crea a runtime vengono
  cancellati (step 11 del workflow) prima che il runner termini.
- Niente passa da bash command history o disco persistente.

## Rotazione

Se rigeneri una API key (es. revoki + crei nuova ANTHROPIC_API_KEY),
basta editare il secret in GitHub UI. Il workflow successivo usa la
nuova chiave senza altre modifiche.

## Troubleshooting

- **"Bad credentials" in step "Run radar"**: una delle API keys è
  scaduta o sbagliata. Controlla `.env` locale e ri-importa.

- **"Token expired" su Gmail**: il `token.json` ha refresh_token, ma
  se hai revocato i permessi dell'app Google da
  myaccount.google.com/permissions, devi ri-fare il flow OAuth
  localmente (`python3 _system/scripts/gmail_client.py --auth`) e
  ri-caricare il nuovo `token.json` come secret.

- **Workflow non parte all'orario previsto**: GitHub Actions cron può
  avere ritardi di 5-30 min nei momenti di carico globale. Considera
  normale arrivo della mail entro le 08:00-08:30 CEST.
