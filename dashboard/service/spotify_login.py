#!/usr/bin/env python3
"""One-time Spotify login for the HUD service (Authorization Code + PKCE, no client secret).

Run on the Pi (over SSH):   python3 ~/sky/service/spotify_login.py

1. Enter the Client ID of your Spotify app (developer.spotify.com/dashboard).
2. Open the printed link in a browser (your PC is fine) and approve.
3. The browser then shows an error page ("can't reach 127.0.0.1"). That is expected:
   copy the FULL address from the address bar and paste it back here.
The refresh token is saved to ~/.config/hud/spotify.json (owner-only). Nothing is sent anywhere else.
"""
import base64
import hashlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

ACCOUNTS = os.environ.get("HUD_SPOTIFY_ACCOUNTS", "https://accounts.spotify.com")
API = os.environ.get("HUD_SPOTIFY_API", "https://api.spotify.com")
OUT = os.path.expanduser(os.environ.get("HUD_SPOTIFY_FILE", "~/.config/hud/spotify.json"))
SCOPE = "user-read-currently-playing"
DEFAULT_REDIRECT = "http://127.0.0.1:8888/callback"


def post(url, form):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(form).encode(), method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def main():
    client_id = (os.environ.get("SPOTIFY_CLIENT_ID") or input("Spotify Client ID: ")).strip()
    redirect = input(f"Redirect URI [{DEFAULT_REDIRECT}]: ").strip() or DEFAULT_REDIRECT
    if not client_id:
        sys.exit("No Client ID.")
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = f"{ACCOUNTS}/authorize?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": client_id, "scope": SCOPE, "redirect_uri": redirect,
        "state": state, "code_challenge_method": "S256", "code_challenge": challenge})
    print("\n1) Open this link, log in to Spotify and click Agree:\n\n" + url + "\n")
    print("2) The browser will then show an error page. Copy the FULL address from the address bar")
    print("   (it starts with " + redirect + "?code=...) and paste it here.\n")
    pasted = input("Pasted address: ").strip()

    q = urllib.parse.parse_qs(urllib.parse.urlparse(pasted).query)
    if "error" in q:
        sys.exit(f"Spotify said: {q['error'][0]}")
    code = (q.get("code") or [None])[0]
    if code is None and pasted and "/" not in pasted and "=" not in pasted:
        code = pasted                                   # a bare code was pasted
    elif code is not None and (q.get("state") or [None])[0] != state:
        sys.exit("The state does not match this login attempt. Start again.")
    if not code:
        sys.exit("Could not find ?code= in what you pasted.")

    status, d = post(f"{ACCOUNTS}/api/token", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect,
        "client_id": client_id, "code_verifier": verifier})
    if status != 200 or not d.get("refresh_token"):
        sys.exit(f"Token exchange failed (HTTP {status}): {d.get('error_description') or d.get('error') or d}")

    os.makedirs(os.path.dirname(OUT), mode=0o700, exist_ok=True)
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"client_id": client_id, "refresh_token": d["refresh_token"]}, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, OUT)
    print(f"\nSaved to {OUT}")

    req = urllib.request.Request(f"{API}/v1/me/player/currently-playing",
                                 headers={"Authorization": f"Bearer {d['access_token']}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            print("Test call OK (HTTP %d). Restart the service:  sudo systemctl restart hud-service" % r.status)
    except urllib.error.HTTPError as e:
        print(f"Saved, but the test call returned HTTP {e.code}. If it is 403, check the Premium requirement and that "
              "your Spotify account is the app owner / added under User Management in the dashboard.")


if __name__ == "__main__":
    main()
