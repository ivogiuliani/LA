# My Villa — Linee guida Social Media

> Fonte ufficiale (Ivo, 2026-06-15). La parte **testuale** (tono di voce +
> obiettivo) è applicata in automatico dai generatori via
> [`social_guidelines.py`](../scripts/social_guidelines.py). La parte
> **visiva/operativa** qui sotto è per chi produce le immagini e gestisce
> il profilo.

## Obiettivo
Incrementare la **brand awareness**, posizionando My Villa come il nuovo
riferimento del **lusso contemporaneo a Los Angeles**. Trasformare le ville
da prodotto immobiliare in **simboli di uno stile di vita** desiderabile e
sofisticato, raccontando una Los Angeles più lenta, autentica e immersa nel
verde.

## Tono di voce  ✅ *automatizzato nei prompt*
- Diretto e chiaro. Frasi **brevi e incisive**.
- **Mai autocelebrativo.**
- **Mai parlare di prezzi o costi.**
- Numeri **solo** quando supportano dati, risultati o percentuali utili a
  raccontare i valori di My Villa.

## Frequenza  ⚙️ *operativa*
- **3 post a settimana** sul profilo.
- **3 repost nelle Stories** dei contenuti già pubblicati sul profilo.

> Nota sistema: il flusso automatico propone contenuti ogni giorno con cap
> (1 IG/giorno) — selezionando i migliori si arriva naturalmente a ~3/sett.
> Le **Stories** non sono automatizzate (l'API Meta è limitata): repost
> manuale dei post migliori, 3/settimana.

## Linguaggio fotografico  ✋ *umano (parz. orientato in automatico)*
- Linguaggio visivo **coerente e riconoscibile**.
- Immagini in **golden hour**: ombre lunghe e morbide, contrasti delicati.
- Prospettive **frontali o leggermente decentrate**.
- Composizioni **simmetriche, essenziali**, con pochi elementi.
- Alternare **viste d'insieme** a **dettagli** (interni, materiali, elementi
  architettonici).

**Riferimenti visivi:** Studio MK27 · Tropical Space · Taller Hector Barroso
· Fernanda Canales · Aman Resorts.

> Nota sistema: lo stock auto-selezionato (Unsplash, fallback) è orientato
> verso questa estetica via `IMAGE_STYLE_HINT`, ma il controllo vero è
> umano. Per i video vedi [instagram_reels_playbook.md](../docs/instagram_reels_playbook.md).

## Linguaggio grafico  ✋ *umano*
Usare font, colori ed elementi grafici **già presenti sul sito** (myvilla.la).

---
*Modifiche al tono di voce: aggiornare `VOICE_RULES` in
[`social_guidelines.py`](../scripts/social_guidelines.py) — si applica a
tutti i generatori.*
