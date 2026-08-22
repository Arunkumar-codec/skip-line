"""
app/utils/security.py
─────────────────────
Password hashing and verification using bcrypt via passlib.
"""

from passlib.context import CryptContext

pwd_context = CryptContext(
    schemes=["pbkdf2_sha256"],
    deprecated="auto"
)


def hash_password(plain: str) -> str:
    """Return a bcrypt hash of the given plaintext password."""
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored hash."""
    return pwd_context.verify(plain, hashed)
