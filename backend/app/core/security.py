import base64
import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError
from jose import JWTError, jwt

from app.core.config import settings
from app.core.errors import Forbidden, Unauthorized

_hasher = PasswordHasher()

# Signed-playback audiences, mirroring Mux's short codes so client code reads the same.
AUD_VIDEO = "v"
AUD_THUMBNAIL = "t"
AUD_GIF = "g"
AUD_STORYBOARD = "s"
VALID_AUDIENCES = {AUD_VIDEO, AUD_THUMBNAIL, AUD_GIF, AUD_STORYBOARD}


def hash_secret(raw: str) -> str:
    return _hasher.hash(raw)


def verify_secret(raw: str, hashed: str) -> bool:
    try:
        return _hasher.verify(hashed, raw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(hashed: str) -> bool:
    try:
        return _hasher.check_needs_rehash(hashed)
    except InvalidHashError:
        return False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: str) -> str:
    return _encode({"sub": user_id, "typ": "access"}, timedelta(minutes=settings.access_token_ttl_minutes))


def create_refresh_token(user_id: str, jti: str) -> str:
    return _encode({"sub": user_id, "typ": "refresh", "jti": jti}, timedelta(days=settings.refresh_token_ttl_days))


def _encode(claims: dict, ttl: timedelta) -> str:
    now = _now()
    payload = {**claims, "iat": int(now.timestamp()), "exp": int((now + ttl).timestamp())}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, expected_type: str) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise Unauthorized(f"Token is not valid: {exc}") from exc
    if payload.get("typ") != expected_type:
        raise Unauthorized(f"Expected a {expected_type} token")
    return payload


def parse_basic_auth(header: str) -> tuple[str, str]:
    """Split `Basic base64(key_id:secret)` into its parts."""
    scheme, _, encoded = header.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        raise Unauthorized("Expected HTTP Basic authentication with an API key")
    try:
        decoded = base64.b64decode(encoded, validate=True).decode()
    except Exception as exc:
        raise Unauthorized("Malformed Basic credentials") from exc
    key_id, sep, secret = decoded.partition(":")
    if not sep or not key_id or not secret:
        raise Unauthorized("Malformed Basic credentials")
    return key_id, secret


# ---- Signed playback tokens (RS256) ----

def generate_signing_keypair() -> tuple[str, str]:
    """Fresh RSA-2048 keypair for a project. Returns (private_pem, public_pem)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public_pem = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def _normalise_pem(raw: str) -> str:
    """Accept a PEM block or a base64-wrapped one, so keys survive being passed through
    environment variables that cannot hold newlines."""
    if not raw:
        raise Forbidden("Signed playback is not configured for this project")
    return raw if "BEGIN" in raw else base64.b64decode(raw).decode()


def sign_playback_token(
    private_key_pem: str,
    key_id: str,
    playback_id: str,
    audience: str,
    ttl_seconds: int = 3600,
    **extra,
) -> str:
    if audience not in VALID_AUDIENCES:
        raise ValueError(f"audience must be one of {sorted(VALID_AUDIENCES)}")
    now = int(time.time())
    claims = {
        "sub": playback_id,
        "aud": audience,
        "iat": now,
        "exp": now + ttl_seconds,
        **extra,
    }
    return jwt.encode(
        claims,
        _normalise_pem(private_key_pem),
        algorithm="RS256",
        headers={"kid": key_id},
    )


def playback_token_key_id(token: str) -> str | None:
    """Read the unverified `kid` header so the right public key can be fetched. Nothing is
    trusted from this until the signature itself verifies against that key."""
    try:
        return jwt.get_unverified_header(token).get("kid")
    except JWTError:
        return None


def verify_playback_token(
    token: str, public_key_pem: str, playback_id: str, audience: str
) -> dict:
    """Validate a signed-playback JWT.

    `aud` pins a token to one kind of resource, so a thumbnail token cannot be replayed to
    pull the full video. A mismatch is a 403 rather than a 404 so a misconfigured client
    gets an actionable error instead of a phantom missing asset.
    """
    try:
        claims = jwt.decode(
            token,
            _normalise_pem(public_key_pem),
            algorithms=["RS256"],
            audience=audience,
            options={"verify_aud": True, "verify_signature": True},
        )
    except JWTError as exc:
        raise Forbidden(f"Playback token rejected: {exc}") from exc
    if claims.get("sub") != playback_id:
        raise Forbidden("Playback token was issued for a different playback ID")
    return claims


# ---- Webhook signatures ----

def sign_webhook(secret: str, body: bytes, timestamp: int | None = None) -> str:
    ts = timestamp or int(time.time())
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


def verify_webhook(secret: str, body: bytes, header: str, tolerance: int | None = None) -> bool:
    """Reference verifier; also used by tests and the dashboard's webhook debugger."""
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts["t"])
        received = parts["v1"]
    except (KeyError, ValueError):
        return False
    window = settings.webhook_tolerance_seconds if tolerance is None else tolerance
    if abs(int(time.time()) - ts) > window:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received)
