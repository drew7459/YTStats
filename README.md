# YTStats

Tooling to pull YouTube stats via the YouTube Data and Analytics APIs.

## Getting a refresh token

`get_refresh_token.py` runs a one-time OAuth flow on your own machine to obtain a
`refresh_token` you can store as a secret (e.g. `GOOGLE_REFRESH_TOKEN`).

```bash
export GOOGLE_CLIENT_SECRET="your-oauth-client-secret"
python get_refresh_token.py
```

It opens a browser, you sign in and approve, and the script prints a refresh token.
Copy that token into your Routine/secrets store — **do not** paste it (or your client
secret) anywhere public.

Scopes requested:
- `https://www.googleapis.com/auth/youtube.readonly`
- `https://www.googleapis.com/auth/yt-analytics.readonly`

## Secrets

Never commit your OAuth client secret or any tokens. The `.gitignore` already excludes:
- `client_secret*.json`
- `token.json`, `credentials.json`
- `.env`

The OAuth **client ID** embedded in the script is not secret and is safe to keep in source.
