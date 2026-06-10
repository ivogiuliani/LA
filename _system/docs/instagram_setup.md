# Instagram — Setup Meta API (una tantum, ~15 min)

Per attivare la pubblicazione automatica servono 2 valori in `.env`:
`IG_ACCESS_TOKEN` e `IG_BUSINESS_ACCOUNT_ID`. Questa guida usa il flusso
**"Instagram API with Instagram Login"** (il più semplice dal 2024:
**non serve una Pagina Facebook**).

## Prerequisito: account Professional

Dall'app Instagram: **Impostazioni → Account → Passa a un account
professionale** → scegli **Business** (gratuito). Se è già
Business/Creator, salta.

## 1. Crea l'app Meta (5 min)

1. Vai su <https://developers.facebook.com/apps/> → **Create App**
2. Use case: **Other** → tipo **Business** → nome es. `MyVilla Publisher`
3. Nel pannello dell'app: **Add product** → trova **Instagram** →
   **API setup with Instagram login** → Set up

## 2. Collega l'account e genera il token (5 min)

1. Nella sezione **API setup with Instagram login**:
   step "**Generate access tokens**" → **Add account** → fai login con
   l'account Instagram di My Villa e autorizza
2. Accanto all'account apparso, clicca **Generate token** → copia il
   token (lungo, inizia con `IG…` o simile — è un long-lived da 60 giorni)

## 3. Configura il sistema (2 min)

Aggiungi in `~/Code/myvilla-la/.env`:

```
IG_ACCESS_TOKEN=<il token copiato>
```

Poi nel terminale:

```bash
cd ~/Code/myvilla-la
python3 _system/scripts/ig_publisher.py --whoami
```

Se il token è giusto stampa `✓ Token valido — @myvilla... (id 1784...)`
e ti dice l'ID da aggiungere:

```
IG_BUSINESS_ACCOUNT_ID=<l'id stampato>
```

## 4. Secrets GitHub (per il rail cloud)

I due valori vanno anche nei secrets del repo (come per Gmail):
<https://github.com/ivogiuliani/LA/settings/secrets/actions>
→ `IG_ACCESS_TOKEN` e `IG_BUSINESS_ACCOUNT_ID`.
(In alternativa: chiedi a Claude di caricarli col device-flow, come
fatto per gli altri secrets.)

## 5. Primo post di prova

C'è già un post approvato in coda
(`_system/social/posts/approved/2026-06-10-ig-test-palisades-standing.md`).
Per pubblicarlo subito senza aspettare il run delle 8:00:

```bash
python3 _system/scripts/ig_publisher.py            # publish reale
python3 _system/scripts/ig_publisher.py --dry-run  # solo anteprima
```

## Manutenzione

- **Il token scade ogni 60 giorni.** Rinnovo (dopo che il token ha
  almeno 24h di vita):
  ```bash
  python3 _system/scripts/ig_publisher.py --refresh-token
  ```
  Stampa il nuovo token → aggiorna `.env` + GitHub secret.
  Se scade del tutto: rigenera dal pannello Meta (passo 2).
- **Cap giornaliero**: 1 post/giorno (ramp-up account nuovo). Per
  alzarlo: `IG_DAILY_CAP=2` in `.env` (o secret).
- **Limiti API Meta**: 50 post/24h via API — non li toccheremo mai.

## Cosa NON può fare l'API (limiti di Meta, non nostri)

- **Commentare post di ALTRI account**: vietato/non supportato.
  Il flusso "commenti su post virali" resta assistito: il pannello
  propone testo + link, si pubblica a mano dall'app.
- Stories e collab post via API hanno vincoli aggiuntivi — fase 2.
