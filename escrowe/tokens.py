"""Minimal HS256 JWT (stdlib only). Escrowe issues one of these at login, so a
server deployment can recognize the same session on later requests without
holding the database password anywhere."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def issue(secret: str, claims: dict, ttl_s: int) -> str:
    header = {"alg": "HS256", "typ": "JWT"}
    now = int(time.time())
    body = {**claims, "iat": now, "exp": now + ttl_s, "jti": secrets.token_hex(8)}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64(json.dumps(body, separators=(',', ':')).encode())}"
    sig = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(sig)}"


class TokenError(Exception):
    pass


def verify(secret: str, token: str) -> dict:
    try:
        h, p, s = token.split(".")
    except ValueError:
        raise TokenError("malformed token")
    expected = hmac.new(secret.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _unb64(s)):
        raise TokenError("bad signature")
    claims = json.loads(_unb64(p))
    if claims.get("exp", 0) < time.time():
        raise TokenError("token expired")
    return claims
