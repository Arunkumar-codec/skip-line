"""
tests/test_config_security.py
─────────────────────────────
Production secret-enforcement regression tests (prodcheck finding, Nov 5–6).

Rules under test (all cases construct Settings directly from kwargs, so no
.env file or environment variable can influence a case — the results here are
purely the validators' behavior):

- valid production signing secrets are accepted (SECRET_KEY AND
  JWT_SECRET_KEY validated separately);
- known development/default signing-key placeholders are rejected;
- unfilled `.env.example` markers (`<set-a-long-random-64+-char-secret>`)
  are rejected;
- unfilled ADMIN_PASSWORD markers are rejected;
- development keeps its intended behavior (warnings only, ephemeral random
  substitution, admin not seeded, DB password warning, never hard rejects);
- DB_PASSWORD handling is unchanged: required in production, warned about in
  development, and NOT subjected to marker rejection (legitimate passwords
  may contain `<`/`>`).

No secret VALUE of any kind is printed; synthetic test values only.
"""
import warnings

import pytest

from app.config import Settings, _is_unfilled_marker

PROD = "production"
DEV = "development"


def _settings(**overrides):
    """Build a Settings instance with explicit kwargs (env files ignored)."""
    overrides.setdefault("_env_file", None)   # hermetic: .env cannot backfill
    overrides.setdefault("app_debug", False)  # production tests configure the
    # deployment honestly (compose sets APP_DEBUG=false by default); debug
    # policy is asserted explicitly in test_prod_rejects_debug_true.
    return Settings(**overrides)


# ── 1. valid production values are accepted ──────────────────────────────────

def test_prod_rejects_debug_true():
    """Debug pages/SQL echo in production are a hard configuration error
    (SEC review: FastAPI debug = traceback disclosure; app_debug also
    drives `echo` on the async engine)."""
    with pytest.raises(ValueError, match="APP_DEBUG"):
        _settings(
            app_env=PROD, app_debug=True,
            db_password="Prodcheck-DB-2026-strong",
            secret_key="pk-" + "securevalue" * 5,
            jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="Prodcheck-Admin-2026!",
        )
    # development may still use debug mode
    s = _settings(app_env="development", app_debug=True,
                  db_password="dev-db-password")
    assert s.app_debug is True


def test_prod_accepts_strong_secrets():
    s = _settings(
        app_env=PROD,
        db_password="Prodcheck-DB-2026-strong",
        secret_key="pk-" + "alphanumericonly" * 4 + "suffixA",
        jwt_secret_key="pk-jwt-" + "mixedsymbols" * 4 + "suffixB",
        admin_password="Prodcheck-Admin-2026!",
    )
    assert s.is_production
    assert len(s.secret_key) >= 32 and len(s.jwt_secret_key) >= 32


def test_prod_accepts_secrets_independently_validated():
    """SECRET_KEY and JWT_SECRET_KEY are validated as two separate fields."""
    for bad_field in ("secret_key", "jwt_secret_key"):
        kwargs = dict(
            app_env=PROD,
            db_password="Prodcheck-DB-2026-strong",
            secret_key="pk-" + "securevalue" * 5,
            jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="Prodcheck-Admin-2026!",
        )
        kwargs[bad_field] = "change-me-to-a-64-char-random-string"
        with pytest.raises(Exception):
            _settings(**kwargs)


# ── 2. known dev/default placeholders are rejected ───────────────────────────

@pytest.mark.parametrize("key", [("change-me-to-a-64-char-random-string"),
                                 ("change-me-to-another-64-char-random-string"),
                                 ("changeme"), ("secret")])
def test_prod_rejects_placeholder_secrets(key):
    with pytest.raises(ValueError, match="insecure placeholder"):
        _settings(
            app_env=PROD, db_password="Prodcheck-DB-2026-strong",
            secret_key=key, jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="Prodcheck-Admin-2026!",
        )
    with pytest.raises(ValueError, match="insecure placeholder"):
        _settings(
            app_env=PROD, db_password="Prodcheck-DB-2026-strong",
            secret_key="pk-" + "securevalue" * 5, jwt_secret_key=key,
            admin_password="Prodcheck-Admin-2026!",
        )


# ── 3. unfilled .env.example markers are rejected ────────────────────────────

@pytest.mark.parametrize("marker", [
    "<set-a-long-random-64+-char-secret>",
])
def test_prod_rejects_unfilled_secret_markers(marker):
    with pytest.raises(ValueError, match="insecure placeholder"):
        _settings(
            app_env=PROD, db_password="Prodcheck-DB-2026-strong",
            secret_key=marker,
            jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="Prodcheck-Admin-2026!",
        )
    with pytest.raises(ValueError, match="insecure placeholder"):
        _settings(
            app_env=PROD, db_password="Prodcheck-DB-2026-strong",
            secret_key="pk-" + "securevalue" * 5,
            jwt_secret_key="<set-a-long-random-64+-char-secret>",
            admin_password="Prodcheck-Admin-2026!",
        )


# ── 4. unfilled ADMIN_PASSWORD markers are rejected ──────────────────────────

def test_prod_rejects_unfilled_admin_marker():
    with pytest.raises(ValueError, match="insecure placeholder"):
        _settings(
            app_env=PROD, db_password="Prodcheck-DB-2026-strong",
            secret_key="pk-" + "securevalue" * 5,
            jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="<set-a-strong-password-12+-chars>",
        )


# ── 5. helper safety: malformed / incomplete markers, legitimate values ──────

def test_marker_helper_full_form_only():
    truthy = [
        "<set-a-long-random-64+-char-secret>",
        "<set-a-strong-password-12+-chars>",
        "<>",
    ]
    for v in truthy:
        assert _is_unfilled_marker(v), v
    falsy = [
        None,
        "",
        "   ",
        "<",                    # single char: not a template
        ">",                    # single char
        "foo<bar",              # incomplete/stray angle bracket inside value
        "<set-a-long-random",   # truncated marker fragment
        "set-a-long-random>",   # truncated marker fragment
        "real-random-secret-value-a" * 3,   # legitimate long secret
        "Base64Like_v2|finechars23",        # legitimate mixed-content secret
    ]
    for v in falsy:
        assert not _is_unfilled_marker(v), repr(v)


def test_prod_rejects_marker_shaped_short_truncated_markers_via_length():
    """A truncated marker fragment must not slip through production either."""
    with pytest.raises(ValueError, match="too short"):
        _settings(
            app_env=PROD, db_password="Prodcheck-DB-2026-strong",
            secret_key="<set-a-long-random",   # 16 chars: rejected by length
            jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="Prodcheck-Admin-2026!",
        )


def test_prod_does_not_reject_legitimate_value_with_internal_angle_brackets():
    """A 32+-char secret with internal `<`/`>` is exotic but not templated."""
    s = _settings(
        app_env=PROD, db_password="Prodcheck-DB-2026-strong",
        secret_key="abcdefghijklmnopqrstuvwxyz<ok>01",  # exactly 32 chars
        jwt_secret_key="pk-jwt-" + "securevalue" * 5,
        admin_password="Prodcheck-Admin-2026!",
    )
    assert s.is_production


# ── 6. development behavior unchanged ────────────────────────────────────────

def test_dev_masks_unfilled_marker_secret_with_warning_no_raise():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", RuntimeWarning)
        s = _settings(
            app_env=DEV, db_password="dev-db-pw",
            secret_key="<set-a-long-random-64+-char-secret>",
            jwt_secret_key="<set-a-long-random-64+-char-secret>",
            admin_password="change-me-admin",
        )
    # warned about (not hard-rejected), and the value was NOT substituted: an
    # unfilled marker stays as configured so the misconfiguration is visible
    insecure = [w for w in caught if "insecure placeholder" in str(w.message)]
    assert len(insecure) == 2  # SECRET_KEY + JWT_SECRET_KEY
    assert s.secret_key == "<set-a-long-random-64+-char-secret>"


def test_dev_missing_secret_gets_ephemeral_random_substitution(monkeypatch):
    # Remove conftest/env sources so the field is genuinely unset, then skip
    # dotenv via _env_file=None: only then is the "missing" branch reachable.
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.warns(RuntimeWarning, match="secure random value"):
        s = _settings(app_env=DEV, db_password="dev-db-pw")
    assert len(s.secret_key) >= 32 and s.secret_key != ""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.warns(RuntimeWarning, match="ADMIN_PASSWORD is not configured"):
        _settings(app_env=DEV, db_password="dev-db-pw",
                  secret_key="pk-" + "securevalue" * 5,
                  jwt_secret_key="pk-jwt-" + "securevalue" * 5)


def test_dev_missing_db_password_warns_only(monkeypatch):
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    with pytest.warns(RuntimeWarning, match="DB_PASSWORD"):
        _settings(app_env=DEV)


def test_prod_missing_db_password_raises(monkeypatch):
    # CI exports DB_PASSWORD as a real OS env var (workflow `env:` block);
    # pydantic-settings reads OS environment variables regardless of
    # _env_file=None, so an ambient value would mask the "missing" branch.
    # Isolate the field exactly like test_dev_missing_db_password_warns_only.
    monkeypatch.delenv("DB_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="DB_PASSWORD"):
        _settings(
            app_env=PROD,
            secret_key="pk-" + "securevalue" * 5,
            jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="Prodcheck-Admin-2026!",
        )


def test_db_password_with_angle_brackets_in_prod_is_accepted():
    """Marker rejection deliberately does NOT apply to DB_PASSWORD."""
    s = _settings(
        app_env=PROD, db_password="right-<mixed>-angles-88",
        secret_key="pk-" + "securevalue" * 5,
        jwt_secret_key="pk-jwt-" + "securevalue" * 5,
        admin_password="Prodcheck-Admin-2026!",
    )
    assert s.db_password == "right-<mixed>-angles-88"


def test_prod_placeholder_admin_password_rejected():
    with pytest.raises(ValueError, match="insecure placeholder"):
        _settings(
            app_env=PROD, db_password="Prodcheck-DB-2026-strong",
            secret_key="pk-" + "securevalue" * 5,
            jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="admin",
        )


def test_prod_short_admin_password_rejected():
    with pytest.raises(ValueError, match="ADMIN_PASSWORD is too short"):
        _settings(
            app_env=PROD, db_password="Prodcheck-DB-2026-strong",
            secret_key="pk-" + "securevalue" * 5,
            jwt_secret_key="pk-jwt-" + "securevalue" * 5,
            admin_password="short-pw",
        )
