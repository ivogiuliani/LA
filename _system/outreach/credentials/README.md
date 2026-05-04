# OAuth Credentials — MyVilla Outreach

This directory holds the OAuth 2.0 credentials used by the MyVilla
outreach system to authenticate against the Gmail API on behalf of
`info@myvilla.la`.

**EVERYTHING in this directory is secret** (except `.gitignore` and
`README.md`). The `.gitignore` ignores by default.

---

## Files

| File | Purpose | Never commit? |
|---|---|---|
| `oauth_client.json` | Desktop OAuth client (client_id + client_secret) from Google Cloud | ✅ yes |
| `token.json` | Access + refresh token, generated on first login | ✅ yes |
| `.gitignore` | Git ignore rules | — (committed) |
| `README.md` | This file | — (committed) |

---

## Provenance

- Google Cloud project: `myvilla-outreach` (organization `myvilla.la`)
- Client type: Desktop app
- Client name: `myvilla-outreach-desktop`
- OAuth consent screen: Internal (organization only, no verification needed)
- Scopes used by the outreach system:
  - `https://www.googleapis.com/auth/gmail.send` — send mail
  - `https://www.googleapis.com/auth/gmail.readonly` — detect replies
  - `https://www.googleapis.com/auth/gmail.modify` — label, mark read
- Created: 2026-04-22 by Ivo Giuliani via Google Cloud Console

## If a credential leaks

1. Go to https://console.cloud.google.com/auth/clients?project=myvilla-outreach
2. Delete the compromised client
3. Create a new OAuth Desktop client with the same name
4. Download the new JSON, overwrite `oauth_client.json`
5. Delete `token.json` and re-authenticate
