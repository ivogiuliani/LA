# My Villa — Categorie di contenuto per X (Twitter)

Riferimento unico di cosa pubblichiamo su **@myvilla_la** e da dove nasce.
I contenuti X si organizzano su **due assi che si incrociano**: il
**formato** (come appare su X) e il **tema** (di cosa parla). In più ci
sono i **pillar editoriali** del brand, che sono la tesi di contenuto.

> Niente è hardcodato come "lista X" nel codice: questo doc mette nero su
> bianco la struttura de-facto. Fonti citate a fianco di ogni voce.

---

## A. Per FORMATO — i 4 tipi di contenuto su X

Come il contenuto si presenta concretamente su X. Tutti passano per il
pannello di approvazione; il publisher è [`x_publisher.py`](../scripts/x_publisher.py)
(cap giornaliero `X_DAILY_CAP`, default 4).

| # | Formato | Cos'è | Da dove nasce |
|---|---------|-------|----------------|
| 1 | **Post reattivo** | Tweet che reagisce a una notizia/segnale del radar | `generate_reactive_posts` → campo `x_post` ([generate_social.py](../scripts/generate_social.py)); file `-x-` |
| 2 | **Post companion** | Rilancia un articolo del journal, con link `myvilla.la/blog/…` + @mention fonte | `generate_companion_posts` ([generate_social.py](../scripts/generate_social.py)) |
| 3 | **Risposta a virale** | Reply a un tweet ad alto engagement | sezione virali del radar → `post_tweet(reply_to=…)`; filtrata dal qualificatore (sotto) |
| 4 | **Quote tweet** | Cita un tweet virale con la nostra POV | `post_tweet(quote_of=…)` ([x_publisher.py](../scripts/x_publisher.py), dal 2026-06-13) |

**Qualificatore delle risposte (formati 3-4):** ogni post virale viene
classificato prima di proporre una risposta — **💰 Compratore**,
**🤝 Partner**, **✦ Brand**, oppure **scartato**. Pochi contatti di
qualità, non volume. Logica in [generate_radar_report.py](../scripts/generate_radar_report.py).

---

## B. Per TEMA — i cluster del radar

Di cosa parliamo. Valgono per tutti i canali (X, IG, journal). Fonte:
[radar-keywords.yml](../config/radar-keywords.yml).

| ID | Cluster | Priorità |
|----|---------|----------|
| A | Luxury Insurable New-Build | 1 |
| B | Materials & Fire-Resilient Construction | 1 |
| C | Insurance & Regulation (statewide) | 1 |
| D | Italian & Mediterranean Villa | 1 |
| E | Luxury Real Estate LA | 2 |
| F | Concrete / Museum-Grade Architecture | 2 |
| H | Climate, Resilience & Sustainability | 2 |
| G | LA Rebuild (Palisades/Altadena) | 3 — *secondario, news value* |

---

## C. I pillar editoriali del brand

La "tesi" di contenuto di My Villa. **Nota:** oggi questi pillar sono
gestiti dal team umano **su Instagram** (vedi [editorial-calendar.yml](../config/editorial-calendar.yml)),
non automatizzati su X — ma restano il riferimento strategico anche per
ciò che diciamo su X.

- **Vision** — la tesi: perché la tipologia villa italiana a LA; "Italian
  Soul, Californian Body"; resilienza come continuità, non paura.
- **Archetypes** — i 9 archetipi architettonici (Courtyard, Podium,
  Portico, Pergola, Fireplace, Window, Living Green Roof, Fence…),
  ciascuno con i maestri italiani.
- **System** — il cemento armato come linguaggio: produzione on-site,
  modularità, lignaggio DGU (Palazzo Grassi, Kimbell), Transsolar.
- **Partner Echo** — repost dei partner operativi: BUROMILAN
  (ingegneria strutturale), DGU (costruzione), Transsolar (clima),
  IT'S (architettura).

---

## Flusso operativo (in breve)

1. Il radar (ogni mattina) trova segnali per **tema** (cluster A-H) e
   post virali.
2. `generate_social.py` produce i **post reattivi** e **companion** (X +
   IG); il qualificatore prepara **risposte/quote** ai virali.
3. Tu approvi nel pannello (`localhost:8787`) — le card X mostrano i
   badge 💰/🤝/✦.
4. `x_publisher.py` pubblica gli approvati (max `X_DAILY_CAP`/giorno),
   storico in `social/posts/published/` per non ripetere.

**Voce/limiti X** (da [editorial-calendar.yml](../config/editorial-calendar.yml) e brand-voice):
≤280 char, mai "fireproof/bunker/investment", mai venderci addosso,
"Paolo Mezzalama" solo in attribuzione di citazione.
