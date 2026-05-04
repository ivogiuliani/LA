# Google Support Ticket — Custom Search JSON API Persistent 403

**Status:** Ready to submit
**Issue:** Custom Search JSON API returns persistent `403 PERMISSION_DENIED` despite all visible configuration being correct.
**Project:** My Villa (project number `1057606374822`)
**Date:** 2026-04-28

---

## How to submit

Choose the channel that fits your support tier. The Issue Tracker (option 1) is free and the most appropriate for this kind of problem.

### Option 1 — Google Issue Tracker (free, public, recommended)

Open this URL and click **NEW ISSUE** (top-right):

> https://issuetracker.google.com/issues/new?component=191645&template=824102

Fill in:
- **Title:** `Custom Search JSON API returns 403 "This project does not have the access" despite API enabled, billing linked, and unrestricted key`
- **Type:** `Bug`
- **Priority:** `P3`
- **Description:** copy-paste the section below titled "Ticket body — copy this"

If the link above 404s (component IDs change), go to:
> https://issuetracker.google.com/  →  Search "Custom Search" → click the component → "New issue"

### Option 2 — Google Cloud Support Cases (requires Standard/Enhanced/Premium support tier)

Open:

> https://console.cloud.google.com/support/cases/create?project=1057606374822

Then:
- **Category:** `Technical issue`
- **Component:** `APIs > Custom Search API`
- **Title and description:** same as below

### Option 3 — Programmable Search Engine help center (slower, less reliable for API issues)

> https://support.google.com/programmable-search/community?hl=en

Post in the community forum with the same body below.

---

## Ticket body — copy this (English)

### Summary

The Custom Search JSON API returns a persistent `HTTP 403 PERMISSION_DENIED` with the message `"This project does not have the access to Custom Search JSON API."` for every request from project `My Villa` (project number `1057606374822`), despite every visible piece of configuration being correct. The same key successfully fetches the Custom Search API discovery document and produces a normal `400 INVALID_ARGUMENT` when called without a `cx` parameter, which proves the key is valid and the endpoint is reachable. The 403 fires only when a `cx` (any cx, including known-good public sample engines) is supplied.

### Project details

- **Project ID:** `myvilla-radar` (derived; verify in console)
- **Project number:** `1057606374822`
- **API key (last 4 chars):** `…x2oA` (full key withheld; please request via secure channel if needed)
- **CSE engine ID tested:** `21173300cf792478e` (created on the same Google account that owns the project)
- **Also tested:** `60154f6ee37264b82` (same account), `017576662512468239146:omuauf_lfve` (Google's public sample engine) — same 403 with all of them.

### Configuration verified

1. Custom Search API shows **Enabled** in the Cloud Console (status timestamp ~2026-04-28).
2. The API key is unrestricted (Application restrictions = `None`; API restrictions whitelist contains all currently-enabled APIs in the project, including `customsearch.googleapis.com`).
3. The project has a billing account linked: `My Billing Account` (Paid account, Active).
4. The CSE engines tested were created from the same Google account that owns the GCP project.
5. A brand-new project (My Villa) was created from scratch specifically for this; the issue reproduces from project creation onwards.
6. The same exact behaviour was previously observed on a different project (`makecom-427113`, project number `766482193987`) before we created My Villa.

### Reproduction

```bash
curl -s "https://www.googleapis.com/customsearch/v1?key=AIzaSy...x2oA&cx=21173300cf792478e&q=test"
```

Returns:

```json
{
  "error": {
    "code": 403,
    "message": "This project does not have the access to Custom Search JSON API.",
    "errors": [
      {
        "message": "This project does not have the access to Custom Search JSON API.",
        "domain": "global",
        "reason": "forbidden"
      }
    ],
    "status": "PERMISSION_DENIED"
  }
}
```

### What does work with the same key

- `GET /discovery/v1/apis/customsearch/v1/rest?key=…` returns the discovery document successfully (HTTP 200), proving the key is valid for the Custom Search service.
- `GET /customsearch/v1?key=…&q=test` (no cx) returns HTTP 400 `INVALID_ARGUMENT`, the expected error for a missing required parameter, which means the request reached the API and was authorised; only the parameter validation failed.

### What does NOT work

- Any call to `/customsearch/v1` with a `cx` parameter returns the 403 above, regardless of:
  - which `cx` is used (own engines, Google's documented sample, both)
  - URL host (`www.googleapis.com` vs canonical `customsearch.googleapis.com`)
  - auth method (URL `?key=` vs `X-Goog-Api-Key` header)
  - presence/absence of `alt=json`

### What we tried

- Created two API keys on the original project, then created an entirely fresh project with a brand-new key — same error.
- Disabled and re-enabled Custom Search API on both projects — no change.
- Removed all API restrictions on the key, then re-added all available APIs to the whitelist — no change.
- Linked a verified billing account to the new project — no change.
- Tested two different CSE engines (both created on the same Google account) plus Google's own public sample CSE — same 403 for all.
- Waited >5 minutes between every change for propagation — no change.

### Hypothesis

Either:
1. There is an organisation-level policy on the Google account that blocks Custom Search JSON API at runtime even though enablement is allowed at the console level, or
2. There is a stale "project access" record on the Custom Search service backend that is not cleared by disable→enable cycles, or
3. There is a country/region restriction on this account for Custom Search.

We cannot diagnose any of these from outside, hence the request for support.

### Requested action

Please clear or rebuild the Custom Search JSON API access record for project number `1057606374822` (My Villa) and confirm there is no organisation/region policy blocking it. If a policy is present, please tell us which one so we can request the appropriate exception.

Thank you.

---

## Italian summary (for the user)

L'errore 403 persiste su qualsiasi chiamata Custom Search da progetto My Villa, nonostante:
- API abilitata, key valida, billing linkato, restrictions ok, engine valido
- Nessuna delle configurazioni visibili è sbagliata (verificato dalla diagnostica)

Il ticket sopra contiene tutto il dettaglio tecnico in inglese. Segui le istruzioni in cima ("How to submit") per inviarlo. Il canale consigliato è l'**Issue Tracker** (Option 1) — è gratis e pubblico.

Conserva questo file per riferimento. Se Google chiede ulteriori dettagli, hanno tutto qui.
