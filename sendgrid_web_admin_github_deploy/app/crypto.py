import base64
import hashlib
from .config import get_settings


def _key_stream(length):
    secret = get_settings().secret_key.encode("utf-8")
    digest = hashlib.sha256(secret).digest()
    output = bytearray()
    while len(output) < length:
        output.extend(digest)
        digest = hashlib.sha256(digest + secret).digest()
    return bytes(output[:length])


def protect(value):
    if value is None:
        return ""
    data = value.encode("utf-8")
    ks = _key_stream(len(data))
    mixed = bytes([a ^ b for a, b in zip(data, ks)])
    return base64.urlsafe_b64encode(mixed).decode("ascii")


def unprotect(value):
    if not value:
        return ""
    raw = base64.urlsafe_b64decode(value.encode("ascii"))
    ks = _key_stream(len(raw))
    data = bytes([a ^ b for a, b in zip(raw, ks)])
    return data.decode("utf-8")


def mask_secret(value, prefix=6, suffix=4):
    if not value:
        return ""
    if len(value) <= prefix + suffix:
        return "*" * len(value)
    return value[:prefix] + "..." + value[-suffix:]
