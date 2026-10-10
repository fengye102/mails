"""Argon2id password hashing; invalid stored hashes fail closed."""

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hash: str, password: str) -> bool:
    try:
        return _hasher.verify(hash, password)
    except (VerificationError, InvalidHashError):
        return False
