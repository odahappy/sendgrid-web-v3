import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import get_settings

_V2_PREFIX = "v2:"


def _legacy_key_stream(length):
    """Legacy stream retained only so existing installations remain readable."""
    secret = get_settings().secret_key.encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    output = bytearray()
    while len(output) < length:
        output.extend(digest)
        digest = hashlib.sha256(digest + secret).digest()
    return bytes(output[:length])


def _aead_key():
    raw = get_settings().data_encryption_key.encode("utf-8")
    return hashlib.sha256(raw).digest()


def protect(value):
    if value is None:
        return ""
    data = str(value).encode("utf-8")
    nonce = os.urandom(12)
    encrypted = AESGCM(_aead_key()).encrypt(nonce, data, None)
    token = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
    return _V2_PREFIX + token


def unprotect(value):
    if not value:
        return ""
    value = str(value)
    if value.startswith(_V2_PREFIX):
        raw = base64.urlsafe_b64decode(value[len(_V2_PREFIX):].encode("ascii"))
        if len(raw) < 13:
            raise ValueError("Invalid protected value")
        nonce, encrypted = raw[:12], raw[12:]
        return AESGCM(_aead_key()).decrypt(nonce, encrypted, None).decode("utf-8")

    # Backward-compatible read path for values written by older releases.
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    ks = _legacy_key_stream(len(raw))
    data = bytes([a ^ b for a, b in zip(raw, ks)])
    return data.decode("utf-8")


def mask_secret(value, prefix=6, suffix=4):
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "*" * len(value)
    return value[:prefix] + "..." + value[-suffix:]
