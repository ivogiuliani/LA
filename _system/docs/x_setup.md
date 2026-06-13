# X (Twitter) — Setup account + API (una tantum, ~20 min)

Due binari distinti, indipendenti:

- **Pubblicazione** → postare in automatico i tweet che `generate_social.py`
  già genera (campo `x_post`), oggi fermi ai link `x.com/intent` manuali in
  `approve.py`. Richiede l'**account X + un'app developer** (chiavi OAuth).
- **Ascolto** → X come fonte del radar, via **Apify** (token già in `.env`).
  **Non** richiede l'API X né, in teoria, l'account. Vedi §5.

> ⚠️ `XAI_API_KEY` nel `.env` è **Grok / xAI (l'LLM)**, *non* l'API social di
> X. Sono prodotti diversi: per postare servono le chiavi nuove qui sotto.

---

## 1. Crea l'account X di My Villa (manuale, ~5 min)

Account **brand** (coerente con `@myvilla.la` su IG). Su X l'handle non
ammette il punto (max 15 char, A-Z/0-9/`_`).

| Campo        | Valore                                                      |
|--------------|-------------------------------------------------------------|
| Display name | `My Villa`                                                  |
| Handle       | **`myvilla_la`** ← creato 2026-06-13 (user id 2065723715476672512) |
| Bio (≤160)   | vedi sotto                                                  |
| Location     | `Los Angeles, CA`                                           |
| Website      | `https://myvilla.la`                                        |
| Avatar       | logo mark My Villa (`img/logos/apple-touch-icon.png` come ripiego) |
| Header       | un hero in calcestruzzo/architettura, 1500×500 (opzionale)  |

**Bio (scegline una — inglese, editoriale, no hashtag, no claim di vendita):**

> Italian Soul, Californian Body. Design-led ultra-luxury villas in reinforced
> concrete for Los Angeles. Architecture by Paolo Mezzalama. myvilla.la

> Italian Soul, Californian Body. A new model for design-led luxury villas in
> reinforced concrete. By architect Paolo Mezzalama (IT'S). myvilla.la

Vincoli brand-voice (valgono per ogni post): contenuti in **inglese**, **Paolo
Mezzalama è la voce pubblica** (non Ivo), niente linguaggio basato sulla paura,
niente claim di case già costruite, niente dati finanziari, **niente hashtag su X**.

Verifica la disponibilità dell'handle direttamente al signup (è istantanea).

## 2. App developer + chiavi (~10 min)

1. Login su <https://developer.x.com> con l'account `@MyVillaLA` → **Sign up**
   per l'accesso developer. Use-case onesto e minimale: *"Publishing our own
   brand's posts."* (non richiedere accesso ai dati: la lettura la fa Apify).
2. Atterri nel **Developer Portal** con un Project + App di default.
3. App → **User authentication settings** → **Set up**:
   - **App permissions: Read and Write** ← obbligatorio per postare
   - Type of App: *Web App / Automated App or Bot*
   - Callback URI: `https://myvilla.la` · Website URL: `https://myvilla.la`
4. Tab **Keys and tokens** → genera e copia:
   - **API Key / API Key Secret** (consumer keys)
   - **Access Token / Access Token Secret** — devono mostrare **Read and Write**.
     Se li avevi generati *prima* del passo 3, **rigenerali**, altrimenti sono
     read-only.

> 💳 **Tier & costi.** Il free tier per i nuovi account è in dismissione: al
> signup vedrai *Free* (~1.500 post/mese in scrittura) **oppure** *pay-per-use*
> (~$0.01 per post). Per il nostro volume (pochi post/giorno) è **zero o
> qualche centesimo/mese** in entrambi i casi; il pay-per-use può chiedere una
> carta. La **lettura** via API ufficiale è cara e **non la usiamo** (→ Apify).

## 3. Aggiungi a `.env` (lo fai tu, sono segreti)

```
X_API_KEY=<API Key>
X_API_SECRET=<API Key Secret>
X_ACCESS_TOKEN=<Access Token>
X_ACCESS_TOKEN_SECRET=<Access Token Secret>
# X_DAILY_CAP=4        # tetto post/24h (default 4)
```

## 4. Verifica

Lo script publisher arriva con la fase-codice (sessione radar — vedi
`x_integration_handoff.md`). Una volta pronto:

```bash
cd ~/Code/myvilla-la
python3 _system/scripts/x_publisher.py --whoami        # atteso: ✓ @MyVillaLA (id …)
python3 _system/scripts/x_publisher.py --dry-run        # prepara, non posta
```

## 5. Ascolto via Apify (nessuna chiave nuova)

Il radar legge X tramite un attore Apify, stesso pattern dello scraper IG
(`radar.py`). Attore consigliato: **`apidojo/tweet-scraper`** (robusto) o
**`xquik/x-tweet-scraper`** (~$0.15/1.000 tweet, il più economico). Il credito
Apify gratis (~$5/mese) copre decine di migliaia di tweet — più che sufficiente.
Niente account X necessario per leggere. Config opzionale:

```
# X_APIFY_ACTOR=apidojo/tweet-scraper
```

## 6. Secrets GitHub (rail cloud)

Le 4 chiavi `X_*` vanno anche nei secrets del repo (come IG/Gmail):
<https://github.com/ivogiuliani/LA/settings/secrets/actions>
(oppure chiedi a Claude di caricarle col device-flow).

## Manutenzione & note

- **I token OAuth 1.0a non scadono** (a differenza di IG, 60 giorni): li
  rigeneri solo se cambi i permessi dell'app o li revochi. Un pensiero in meno.
- **Rate limit**: anche su Free/PPU restano finestre per-endpoint, ma con
  pochi post/giorno siamo ordini di grandezza sotto.
- **Formato già a norma**: `generate_social.py` produce `x_post` ≤280 char,
  senza hashtag, con link all'articolo Journal — il publisher lo posta così com'è.
