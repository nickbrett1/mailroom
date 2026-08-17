"""Mint a PS App OAuth refresh token for the PSN sync (one interactive login).

Flow (community-documented PS App OAuth, as used by psnawp-api / psn-api):
  1. Open the authorize URL in a browser (email/password + 2FA).
  2. After login the browser lands on `com.scee.psxandroid.scecomp008://redirect?code=...`
     — copy that FULL URL and paste it here.
  3. The code is exchanged for a short-lived access token + a MONTHS-LONG
     refresh token. Store the refresh token via --store into the mailroom
     `credentials` table (source='psn'); the Tuesday sync uses it thereafter.

Usage:
    python3 scripts/psn_mint_token.py
    python3 scripts/psn_mint_token.py --store sqlite:////data/mailroom.db
    python3 scripts/psn_mint_token.py --verify sqlite:////data/mailroom.db
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import urllib.parse
import uuid
import webbrowser

try:
    import httpx
except ModuleNotFoundError:
    # System python lacks the project deps — re-exec into the venv if present
    # (e.g. `python3 scripts/psn_mint_token.py` on the devcontainer).
    _venv_py = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv", "bin", "python3")
    if os.path.exists(_venv_py):
        os.execv(_venv_py, [_venv_py, *sys.argv])
    raise

from mailroom.clients import PsnApiClient


def authorize_url() -> str:
    params = {
        "client_id": PsnApiClient.CLIENT_ID,
        "response_type": "code",
        "scope": PsnApiClient.SCOPE,
        "redirect_uri": PsnApiClient.REDIRECT_URI,
        "state": uuid.uuid4().hex,
        "ui": "pr",
        "service_entity": "urn:service-entity:psn",
        "duid": str(uuid.uuid4()),
    }
    return "https://ca.account.sony.com/api/authz/v3/oauth/authorize?" + urllib.parse.urlencode(params)


def parse_redirect_url(url: str) -> tuple[str, str]:
    """Extract (code, state) from the pasted redirect URL."""
    m = re.match(r"com\.scee\.psxandroid\.scecomp008://redirect\?(.*)", url, re.IGNORECASE)
    if not m:
        raise ValueError("URL must start with com.scee.psxandroid.scecomp008://redirect?")
    qs = urllib.parse.parse_qs(m.group(1))
    code = (qs.get("code") or [""])[0]
    state = (qs.get("state") or [""])[0]
    if not code:
        raise ValueError("no code= parameter found in the redirect URL")
    return code, state


def exchange_code(code: str, client: httpx.Client | None = None) -> dict:
    """authorization_code grant -> {access_token, refresh_token, expires_in}."""
    c = client or httpx.Client()
    resp = c.post(
        PsnApiClient.OAUTH_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": PsnApiClient.REDIRECT_URI,
            "client_id": PsnApiClient.CLIENT_ID,
            "scope": PsnApiClient.SCOPE,
            "token_format": "jwt",
        },
    )
    if resp.status_code in (400, 401):
        raise RuntimeError(f"token exchange rejected (HTTP {resp.status_code}): {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


def store_refresh_token(db_url: str, refresh_token: str) -> None:
    from mailroom.db import connect, init_db, set_credential

    conn = connect(db_url)
    init_db(conn)
    set_credential(conn, "psn", token=refresh_token, token_type="refresh_token", status="valid")
    conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--store", metavar="DB", help="sqlite URL: store the refresh token in credentials (source='psn')")
    ap.add_argument("--verify", metavar="DB", help="sqlite URL: pull the library with the stored token (first-sweep check)")
    args = ap.parse_args()

    if args.verify:
        from mailroom.db import connect, get_credential, init_db

        conn = connect(args.verify)
        init_db(conn)
        cred = get_credential(conn, "psn") or {}
        conn.close()
        token = cred.get("token")
        if not token:
            print("no refresh token stored for source='psn' — run the mint flow first (--store)")
            return 1
        print(f"verifying library pull with stored token ({len(token)} chars)…")
        titles = PsnApiClient(refresh_token=token).library_titles()
        print(f"library items: {len(titles)}")
        from mailroom.clients import psn_library_item_to_game

        games = [g for g in (psn_library_item_to_game(t) for t in titles) if g]
        classes: dict[str, int] = {}
        for g in games:
            classes[g["ownership_class"]] = classes.get(g["ownership_class"], 0) + 1
        print("normalized games:", len(games), "| by ownership_class:", classes)
        for g in games[:5]:
            print(f"  {g['title'][:50]:50s} {g['platform']:15s} {g['ownership_class']}")
        return 0

    url = authorize_url()
    print("1) Open this URL in a browser and sign in (email/password + 2FA if enabled):")
    print()
    print("   ", url)
    print()
    try:
        webbrowser.open(url)
        print("   (attempted to open your browser too)")
    except webbrowser.Error:
        print("   (could not open a browser automatically — copy the URL above)")
    pasted = input("2) After login you land on com.scee.psxandroid.scecomp008://redirect?…\n"
                   "   Paste that FULL URL here: ").strip()
    try:
        code, _state = parse_redirect_url(pasted)
    except ValueError as exc:
        print(f"parse error: {exc}")
        return 1
    print("3) Exchanging code for tokens…")
    try:
        tokens = exchange_code(code)
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"exchange failed: {exc}")
        return 1
    access = tokens.get("access_token")
    refresh = tokens.get("refresh_token")
    print(f"   access token:  {access[:24]}… ({tokens.get('expires_in')}s)" if access else "   access token: MISSING")
    if not refresh:
        print("   refresh token: MISSING — cannot store; check the exchange response")
        return 1
    print(f"   refresh token: {refresh[:24]}… ({len(refresh)} chars, months-long)")
    if args.store:
        store_refresh_token(args.store, refresh)
        print(f"   stored in credentials (source='psn', status=valid) at {args.store}")
        print("   next: run --verify to do the first full-library sweep")
    else:
        print()
        print("Refresh token (store it via --store <db>, or keep it somewhere safe):")
        print(refresh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
