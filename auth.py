"""
Google Workspace sign-in (OAuth 2.0 authorization-code flow), standard library only.

The ID token is received directly from Google's token endpoint over HTTPS in
exchange for the one-time code, so its signature check can be skipped (Google's
OpenID Connect docs allow this). Its claims are still validated: issuer,
audience, expiry, verified email and Workspace domain.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request

import config

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}


class AuthError(Exception):
    """`code` is shown to the user on the login page (see static/login.html)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def redirect_uri() -> str:
    return config.PUBLIC_URL + "/auth/callback"


def login_url(state: str) -> str:
    return AUTH_ENDPOINT + "?" + urllib.parse.urlencode({
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "hd": config.ALLOWED_DOMAIN,       # hint: show only company accounts
        "prompt": "select_account",
    })


def _jwt_claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception as e:
        raise AuthError("google", f"unreadable ID token ({e})")


def exchange_code(code: str) -> dict:
    """Swap the one-time code for the user's verified identity claims."""
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }).encode()
    req = urllib.request.Request(TOKEN_ENDPOINT, data=data,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            tokens = json.load(resp)
    except Exception as e:
        raise AuthError("google", f"token exchange failed ({e})")
    if not tokens.get("id_token"):
        raise AuthError("google", "no ID token in Google's response")
    claims = _jwt_claims(tokens["id_token"])
    if claims.get("iss") not in ISSUERS:
        raise AuthError("google", "wrong issuer")
    if claims.get("aud") != config.GOOGLE_CLIENT_ID:
        raise AuthError("google", "token is for a different app")
    if float(claims.get("exp", 0)) < time.time():
        raise AuthError("google", "token expired")
    if not claims.get("email") or not claims.get("email_verified"):
        raise AuthError("google", "email not verified")
    claims["email"] = claims["email"].strip().lower()
    if (claims.get("hd") or "").lower() != config.ALLOWED_DOMAIN:
        raise AuthError("domain")
    err = access_error(claims["email"])
    if err:
        raise AuthError(err)
    return claims


def access_error(email: str) -> str | None:
    """Checked at sign-in and on every request, so removing someone takes effect at once."""
    if not email.endswith("@" + config.ALLOWED_DOMAIN):
        return "domain"
    if config.ALLOWED_EMAILS and email not in config.ALLOWED_EMAILS:
        return "notallowed"
    return None


def role_for(email: str) -> str:
    return "viewer" if email in config.READONLY_EMAILS else "editor"
