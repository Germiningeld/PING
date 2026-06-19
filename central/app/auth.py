from __future__ import annotations

import hashlib
import hmac


def hash_probe_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_probe_token(*, token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_probe_token(token), token_hash)
