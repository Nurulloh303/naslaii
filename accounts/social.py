"""Вход через Google и Telegram.

Ключевая мысль: НИЧЕМУ из браузера верить нельзя. Любые данные, которые
прислал клиент, можно подделать руками. Поэтому оба способа проверяются
криптографически:

  * Telegram — HMAC-SHA256 подпись на токене бота;
  * Google   — подпись JWT публичными ключами Google.

Порт server/socialAuth.mjs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
# Данные виджета Telegram старше суток не принимаем: иначе перехваченный
# ответ можно отправить повторно.
TELEGRAM_MAX_AGE_SEC = 24 * 60 * 60


class SocialAuthError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --------------------------------------------------------------- Telegram

def verify_telegram_login(payload: dict, bot_token: str | None = None) -> dict:
    """Проверяет ответ Telegram Login Widget по алгоритму из их документации."""
    bot_token = bot_token if bot_token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise SocialAuthError("TELEGRAM_NOT_CONFIGURED")
    if not isinstance(payload, dict):
        raise SocialAuthError("BAD_PAYLOAD")

    received_hash = str(payload.get("hash") or "")
    if len(received_hash) != 64:
        raise SocialAuthError("BAD_HASH")

    fields = {k: v for k, v in payload.items() if k != "hash" and v is not None}
    data_check_string = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))

    secret = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()

    # compare_digest — чтобы по времени сравнения нельзя было подобрать подпись.
    if not hmac.compare_digest(expected, received_hash.lower()):
        raise SocialAuthError("BAD_SIGNATURE")

    try:
        auth_date = int(fields.get("auth_date"))
    except (TypeError, ValueError):
        raise SocialAuthError("NO_AUTH_DATE")
    if int(time.time()) - auth_date > TELEGRAM_MAX_AGE_SEC:
        raise SocialAuthError("EXPIRED")

    telegram_id = str(fields.get("id") or "").strip()
    if not telegram_id:
        raise SocialAuthError("NO_ID")

    name = " ".join(filter(None, [fields.get("first_name"), fields.get("last_name")])).strip()
    username = fields.get("username")
    return {
        "provider": "telegram",
        "provider_id": telegram_id,
        "name": name or (f"@{username}" if username else "Telegram foydalanuvchisi"),
        # Telegram почту НЕ отдаёт — это нормально, дальше подставим внутреннюю.
        "email": None,
    }


# ----------------------------------------------------------------- Google

_jwks_cache: dict = {"keys": [], "fetched_at": 0.0}


def _google_keys() -> list[dict]:
    """Ключи Google кешируем на час: они меняются редко."""
    if _jwks_cache["keys"] and time.time() - _jwks_cache["fetched_at"] < 3600:
        return _jwks_cache["keys"]
    response = requests.get(GOOGLE_JWKS_URL, timeout=10)
    response.raise_for_status()
    _jwks_cache["keys"] = response.json().get("keys", [])
    _jwks_cache["fetched_at"] = time.time()
    return _jwks_cache["keys"]


def _b64url(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def verify_google_id_token(id_token: str, client_id: str | None = None) -> dict:
    """Проверяет подпись Google ID-токена и его адресата."""
    client_id = client_id if client_id is not None else os.environ.get("GOOGLE_CLIENT_ID", "")
    if not client_id:
        raise SocialAuthError("GOOGLE_NOT_CONFIGURED")
    if not isinstance(id_token, str) or id_token.count(".") != 2:
        raise SocialAuthError("BAD_TOKEN")

    raw_header, raw_payload, raw_signature = id_token.split(".")
    try:
        header = json.loads(_b64url(raw_header))
        claims = json.loads(_b64url(raw_payload))
    except (ValueError, json.JSONDecodeError):
        raise SocialAuthError("BAD_TOKEN")

    if header.get("alg") != "RS256":
        raise SocialAuthError("BAD_ALG")

    try:
        keys = _google_keys()
    except requests.RequestException:
        raise SocialAuthError("JWKS_UNAVAILABLE")

    jwk = next((key for key in keys if key.get("kid") == header.get("kid")), None)
    if not jwk:
        raise SocialAuthError("UNKNOWN_KEY")

    if not _verify_rs256(f"{raw_header}.{raw_payload}".encode(), _b64url(raw_signature), jwk):
        raise SocialAuthError("BAD_SIGNATURE")

    # Подпись может быть настоящей, но токен выпущен для ЧУЖОГО приложения.
    # Без этой проверки злоумышленник войдёт к нам с токеном своего сайта.
    if claims.get("aud") != client_id:
        raise SocialAuthError("WRONG_AUDIENCE")
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise SocialAuthError("WRONG_ISSUER")
    if float(claims.get("exp", 0)) < time.time():
        raise SocialAuthError("EXPIRED")
    if not claims.get("email"):
        raise SocialAuthError("NO_EMAIL")
    if claims.get("email_verified") is False:
        raise SocialAuthError("EMAIL_NOT_VERIFIED")

    email = str(claims["email"]).lower()
    return {
        "provider": "google",
        "provider_id": str(claims.get("sub")),
        "name": str(claims.get("name") or email.split("@")[0])[:80],
        "email": email,
    }


def _verify_rs256(message: bytes, signature: bytes, jwk: dict) -> bool:
    """Проверка RSA-подписи. Собираем ключ из JWK без внешних библиотек."""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError:
        # Без cryptography проверить подпись честно нельзя. Пропускать
        # проверку в такой ситуации опаснее, чем отказать во входе.
        logger.error("cryptography kutubxonasi yo‘q — Google login o‘chirilgan")
        raise SocialAuthError("CRYPTO_UNAVAILABLE")

    modulus = int.from_bytes(_b64url(jwk["n"]), "big")
    exponent = int.from_bytes(_b64url(jwk["e"]), "big")
    public_key = rsa.RSAPublicNumbers(exponent, modulus).public_key()
    try:
        public_key.verify(signature, message, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False
