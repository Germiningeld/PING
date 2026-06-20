from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time


ADMIN_SESSION_COOKIE = "ping_admin_session"
ADMIN_SESSION_MAX_AGE_SECONDS = 60 * 60 * 12
LOCAL_ADMIN_USERNAME = "local-development"


def hash_probe_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_probe_token(*, token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_probe_token(token), token_hash)


def hash_admin_password(password: str, *, salt: str | None = None) -> str:
    password_salt = salt or secrets.token_hex(16)
    derived_key = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        password_salt.encode("utf-8"),
        120_000,
    )
    return f"pbkdf2_sha256${password_salt}${derived_key.hex()}"


def verify_admin_password(*, password: str, password_hash: str) -> bool:
    try:
        algorithm, salt, expected_hash = password_hash.split("$", 2)
    except ValueError:
        return False

    if algorithm != "pbkdf2_sha256":
        return False

    candidate_hash = hash_admin_password(password, salt=salt)
    return hmac.compare_digest(candidate_hash, password_hash)


def get_admin_username() -> str | None:
    return os.getenv("PING_ADMIN_USERNAME")


def get_admin_password_hash() -> str | None:
    return os.getenv("PING_ADMIN_PASSWORD_HASH")


def get_admin_session_secret() -> str | None:
    return os.getenv("PING_ADMIN_SESSION_SECRET")


def admin_auth_configured() -> bool:
    return all(
        (
            get_admin_username(),
            get_admin_password_hash(),
            get_admin_session_secret(),
        )
    )


def admin_auth_disabled() -> bool:
    return (
        os.getenv("PING_ENV", "").lower() == "development"
        and os.getenv("PING_AUTH_DISABLED", "").lower() in {"1", "true", "yes", "on"}
    )


def verify_admin_credentials(*, username: str, password: str) -> bool:
    expected_username = get_admin_username()
    password_hash = get_admin_password_hash()
    if expected_username is None or password_hash is None:
        return False

    username_matches = hmac.compare_digest(username, expected_username)
    password_matches = verify_admin_password(
        password=password,
        password_hash=password_hash,
    )
    return username_matches and password_matches


def create_admin_session_token(username: str, *, now: int | None = None) -> str:
    secret = get_admin_session_secret()
    if secret is None:
        raise RuntimeError("Admin session secret is not configured")

    issued_at = str(now or int(time.time()))
    payload = f"{username}:{issued_at}"
    signature = _sign_session_payload(payload, secret=secret)
    token = f"{payload}:{signature}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")


def verify_admin_session_token(token: str | None) -> str | None:
    secret = get_admin_session_secret()
    if not token or secret is None:
        return None

    try:
        decoded = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        username, issued_at_raw, signature = decoded.rsplit(":", 2)
        issued_at = int(issued_at_raw)
    except (ValueError, UnicodeDecodeError):
        return None

    payload = f"{username}:{issued_at_raw}"
    expected_signature = _sign_session_payload(payload, secret=secret)
    if not hmac.compare_digest(signature, expected_signature):
        return None

    if int(time.time()) - issued_at > ADMIN_SESSION_MAX_AGE_SECONDS:
        return None

    expected_username = get_admin_username()
    if expected_username is None or not hmac.compare_digest(username, expected_username):
        return None

    return username


def cookie_secure_enabled() -> bool:
    configured = os.getenv("PING_COOKIE_SECURE")
    if configured is not None:
        return configured.lower() in {"1", "true", "yes", "on"}
    return os.getenv("PING_ENV") == "production"


def _sign_session_payload(payload: str, *, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
