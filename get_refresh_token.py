#!/usr/bin/env python3
"""
get_refresh_token.py  —  RUN THIS ONCE, ON YOUR OWN LAPTOP.

It opens a browser, you sign in + approve, and it prints a refresh token.
Copy that token into your Routine secrets as GOOGLE_REFRESH_TOKEN.
Do NOT paste the token (or your client secret) back into chat.

No pip installs needed — standard library only.

Usage:
    export GOOGLE_CLIENT_SECRET="your-secret-here"   # from the OAuth client
    python get_refresh_token.py
(If you skip the export, it will prompt you for the secret without echoing it.)
"""

import os
import sys
import json
import getpass
import webbrowser
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

# Your Desktop OAuth client ID (not secret — safe to embed)
CLIENT_ID = "555983252828-h7hdeslnt0fnh89kn7hkefa33q0fot18.apps.googleusercontent.com"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"

client_secret = os.environ.get("GOOGLE_CLIENT_SECRET") or getpass.getpass(
    "Paste your OAuth client secret (input hidden): ").strip()

# tiny local server to catch the redirect
auth_code = {}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        qs = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(qs)
        auth_code["code"] = params.get("code", [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        msg = "Done. You can close this tab and return to your terminal."
        self.wfile.write(f"<html><body><h2>{msg}</h2></body></html>".encode())

    def log_message(self, *a):  # silence the server logs
        pass

def main():
    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    redirect_uri = f"http://127.0.0.1:{port}"

    auth_url = AUTH_URI + "?" + urllib.parse.urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",   # <- required to receive a refresh token
        "prompt": "consent",        # <- forces a fresh refresh token every time
    })

    print("\nOpening your browser to approve access...")
    print("If it doesn't open, paste this URL manually:\n")
    print(auth_url + "\n")
    webbrowser.open(auth_url)

    server.handle_request()  # blocks until the redirect hits
    code = auth_code.get("code")
    if not code:
        print("No authorization code received. Try again.", file=sys.stderr)
        sys.exit(1)

    data = urllib.parse.urlencode({
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_URI, data=data)
    with urllib.request.urlopen(req) as resp:
        tokens = json.load(resp)

    refresh = tokens.get("refresh_token")
    if not refresh:
        print("\nNo refresh_token returned. This usually means the app already "
              "has consent. Revoke access at "
              "https://myaccount.google.com/permissions and run again.",
              file=sys.stderr)
        sys.exit(1)

    print("\n" + "=" * 60)
    print("SUCCESS — your refresh token (keep it secret):\n")
    print(refresh)
    print("\nPut it in the Routine as GOOGLE_REFRESH_TOKEN.")
    print("=" * 60)

if __name__ == "__main__":
    main()
