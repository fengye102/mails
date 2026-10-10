import base64
import hashlib
import hmac
import os
from html.parser import HTMLParser

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_secret(value: str, digest: str) -> bool:
    return hmac.compare_digest(hash_secret(value), digest)


def new_code() -> str:
    return base64.urlsafe_b64encode(os.urandom(24)).decode("ascii").rstrip("=")


def encrypt_value(key_text: str, value: str) -> bytes:
    try:
        key = base64.b64decode(key_text, validate=True)
    except ValueError:
        key = hashlib.sha256(key_text.encode()).digest()
    if len(key) != 32:
        raise ValueError("DELIVERY_ENCRYPTION_KEY must decode to 32 bytes")
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, value.encode(), None)


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self.ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self.ignored:
            self.ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self.ignored:
            self.parts.append(data)


def plain_text(value: str) -> str:
    parser = _TextParser()
    parser.feed(value)
    parser.close()
    return " ".join(" ".join(parser.parts).split())

