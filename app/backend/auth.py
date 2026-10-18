"""
Bearer authentication for the Phronesis API.

Every data-bearing route resolves an authenticated ``user_id`` that scopes memory, threads, and history.
Configuration (environment variables):

- ``PHRONESIS_API_TOKENS``: comma-separated ``token:user_id`` pairs for static bearer tokens.
- ``PHRONESIS_JWT_SECRET``: HS256 secret for JWT bearer tokens; the ``sub`` claim is the user id and
  ``exp`` is required. Optional ``PHRONESIS_JWT_AUDIENCE`` / ``PHRONESIS_JWT_ISSUER`` are enforced when set.
- ``PHRONESIS_AUTH_MODE=disabled``: explicit single-user local development mode (all requests run as
  ``local_dev_user``). Never use this on a shared or network-exposed deployment.

If authentication is enabled (the default) but neither tokens nor a JWT secret are configured, requests are
rejected with 503 rather than silently falling back to a shared identity.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from typing import Dict, List, Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger("phronesis.backend.auth")

LOCAL_DEV_USER = "local_dev_user"
_USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.@:\-]{1,128}$")

_bearer = HTTPBearer(auto_error=False)


def auth_disabled() -> bool:
    return os.getenv("PHRONESIS_AUTH_MODE", "").strip().lower() == "disabled"


def _load_static_tokens() -> Dict[str, str]:
    tokens: Dict[str, str] = {}
    for pair in os.getenv("PHRONESIS_API_TOKENS", "").split(","):
        pair = pair.strip()
        if not pair:
            continue
        token, sep, user_id = pair.rpartition(":")
        if not sep or not token or not _USER_ID_PATTERN.match(user_id):
            logger.warning("Ignoring malformed PHRONESIS_API_TOKENS entry (expected token:user_id).")
            continue
        tokens[token] = user_id
    return tokens


def secret_strength_problems() -> List[str]:
    """Weak bearer secrets (SEC-009): static tokens shorter than 32 characters, an HS256 secret shorter than
    32 bytes. Checked by phronesis.config.validate_environment, so startup logs them and PHRONESIS_STRICT_CONFIG=1
    refuses to start; values are never included in the text."""
    from phronesis.config.settings import secret_problems

    return secret_problems(os.environ)


def _match_static_token(token: str) -> Optional[str]:
    for known, user_id in _load_static_tokens().items():
        if hmac.compare_digest(known.encode(), token.encode()):
            return user_id
    return None


def _decode_jwt(token: str) -> Optional[str]:
    secret = os.getenv("PHRONESIS_JWT_SECRET")
    if not secret:
        return None
    import jwt  # PyJWT

    options = {"require": ["exp", "sub"]}
    kwargs = {}
    if os.getenv("PHRONESIS_JWT_AUDIENCE"):
        kwargs["audience"] = os.getenv("PHRONESIS_JWT_AUDIENCE")
    if os.getenv("PHRONESIS_JWT_ISSUER"):
        kwargs["issuer"] = os.getenv("PHRONESIS_JWT_ISSUER")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"], options=options, **kwargs)
    except jwt.PyJWTError as err:
        # The reason (expired, bad audience, bad signature) stays in the server log: echoing it helps an attacker
        # probe the token configuration.
        logger.info("Rejected bearer JWT: %s: %s", type(err).__name__, err)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from err
    user_id = str(claims.get("sub", ""))
    if not _USER_ID_PATTERN.match(user_id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token subject is not a valid user id.")
    return user_id


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
) -> str:
    """FastAPI dependency returning the authenticated user id."""
    if auth_disabled():
        return LOCAL_DEV_USER

    if not os.getenv("PHRONESIS_API_TOKENS") and not os.getenv("PHRONESIS_JWT_SECRET"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Authentication is not configured. Set PHRONESIS_API_TOKENS or PHRONESIS_JWT_SECRET, "
                "or PHRONESIS_AUTH_MODE=disabled for single-user local development."
            ),
        )

    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    user_id = _match_static_token(token) or _decode_jwt(token)
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id
