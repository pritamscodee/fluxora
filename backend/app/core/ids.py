import secrets

# Crockford-ish base58: no 0/O/I/l, so ids survive being read aloud or hand-copied.
_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def public_id(length: int = 32) -> str:
    """Opaque, unguessable public identifier. Used for every externally visible id."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(length))


def prefixed_id(prefix: str, length: int = 24) -> str:
    return f"{prefix}_{public_id(length)}"


def api_key_id() -> str:
    return prefixed_id("fx", 20)


def api_key_secret() -> str:
    return secrets.token_urlsafe(36)


def stream_key() -> str:
    return public_id(40)


def webhook_secret() -> str:
    return f"whsec_{secrets.token_urlsafe(32)}"
