"""Password hashing, access tokens and opaque tokens.

- Passwords: Argon2id (argon2-cffi defaults = RFC 9106 low-memory profile),
  rehashed on login when parameters change.
- Access tokens: short-lived HS256 JWTs carrying user id, role and session id.
- Refresh / email tokens: 32 random bytes, URL-safe; only their SHA-256 is
  stored, so a database leak does not reveal usable tokens.
- CSRF: double-submit token for cookie-authenticated unsafe requests."""
import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()
# Verified against when the account does not exist, so response time does not
# reveal which emails are registered.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "ofmp"
MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: Optional[str], password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def validate_password_strength(password: str) -> Optional[str]:
    """Error message, or None if acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"password must be at least {MIN_PASSWORD_LENGTH} characters"
    if len(password) > 256:
        return "password must be at most 256 characters"
    if password.isdigit() or password.isalpha():
        return "password must mix letters with digits or symbols"
    return None


@dataclass(frozen=True)
class AccessClaims:
    user_id: uuid.UUID
    role: str
    session_id: uuid.UUID
    expires_at: int


def create_access_token(secret: str, user_id: uuid.UUID, role: str, session_id: uuid.UUID,
                        ttl_sec: int, now: Optional[float] = None) -> str:
    now = int(now if now is not None else time.time())
    return jwt.encode({"sub": str(user_id), "role": role, "sid": str(session_id), "iss": JWT_ISSUER,
                       "iat": now, "exp": now + ttl_sec, "typ": "access"}, secret, algorithm=JWT_ALGORITHM)


def decode_access_token(secret: str, token: str) -> Optional[AccessClaims]:
    """Claims if the token is valid and unexpired, else None."""
    try:
        p = jwt.decode(token, secret, algorithms=[JWT_ALGORITHM], issuer=JWT_ISSUER,
                       options={"require": ["sub", "sid", "exp", "iat", "iss"]})
        if p.get("typ") != "access":
            return None
        return AccessClaims(uuid.UUID(p["sub"]), p.get("role", "user"), uuid.UUID(p["sid"]), int(p["exp"]))
    except (jwt.PyJWTError, ValueError, KeyError):
        return None


def new_opaque_token() -> tuple:
    """(token for the client, sha256 hex to store)."""
    token = secrets.token_urlsafe(32)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def csrf_matches(cookie_value: Optional[str], header_value: Optional[str]) -> bool:
    return bool(cookie_value and header_value and hmac.compare_digest(cookie_value, header_value))
