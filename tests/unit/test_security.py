"""tests/unit/test_security.py"""

import pytest
from app.utils.security import hash_password, verify_password
from app.utils.jwt import create_access_token, decode_token


class TestPasswordHashing:
    def test_hash_is_not_plain(self):
        hashed = hash_password("mypassword")
        assert hashed != "mypassword"

    def test_verify_correct_password(self):
        hashed = hash_password("correct-horse")
        assert verify_password("correct-horse", hashed) is True

    def test_reject_wrong_password(self):
        hashed = hash_password("correct-horse")
        assert verify_password("wrong-horse", hashed) is False

    def test_two_hashes_differ(self):
        h1 = hash_password("same")
        h2 = hash_password("same")
        assert h1 != h2  # bcrypt salts are unique


class TestJWT:
    def test_create_and_decode(self):
        token = create_access_token(subject=42, role="user")
        payload = decode_token(token)
        assert payload is not None
        assert payload["sub"] == "42"
        assert payload["role"] == "user"

    def test_invalid_token_returns_none(self):
        result = decode_token("not.a.valid.token")
        assert result is None

    def test_tampered_token_returns_none(self):
        token = create_access_token(subject=1, role="admin")
        tampered = token[:-5] + "xxxxx"
        assert decode_token(tampered) is None

    def test_roles_are_distinct(self):
        t_admin = create_access_token(subject=1, role="admin")
        t_user = create_access_token(subject=1, role="user")
        assert decode_token(t_admin)["role"] == "admin"
        assert decode_token(t_user)["role"] == "user"
