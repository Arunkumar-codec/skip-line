"""
tests/test_ci_migration_gate.py
───────────────────────────────
HERMETIC gate tests for scripts/ci_migration_chain_check.py (CI migration
verifier). Phase-9 production-readiness, pre-activation security review.

These tests import the script as a plain module (it is deliberately written
with NO import-time side effects) and exercise the safety gate with
SYNTHETIC configuration dictionaries only:

    - no database connection is ever attempted;
    - no real project database is ever named except to assert its refusal;
    - no .env values, production credentials or paid services are touched.

What is verified:
    validate_ci_config(env)
        · accepts exactly the allowlisted disposable name skipline_ci_migrate
          (loopback host, valid port, non-empty synthetic user/password);
        · rejects production-like names (skipline, skipline_production),
          empty DB_NAME, unknown / prefix-look-alike names;
        · rejects non-loopback hosts (a remote host could be a production
          server);
        · rejects bad ports (non-integer, 0, 65536+, negative);
        · rejects empty DB_USER / DB_PASSWORD.
    drop_disposable_check(name)
        · refuses (SystemExit) every name except the exact allowlist entry —
          so NO exception path can ever drop an unrelated database;
        · grants exactly skipline_ci_migrate.
    redact(message, db_url_str, password)
        · never lets the synthetic password or URL survive into output.

Run hermetically (works even where the DB-backed suite is blocked):

    pytest tests/test_ci_migration_gate.py -v --noconftest
"""
import importlib.util
import os
import sys

import pytest

# ── module loading: scripts/ is not a package ────────────────────────────────
_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts", "ci_migration_chain_check.py")

_spec = importlib.util.spec_from_file_location("ci_migration_chain_check",
                                               _SCRIPT)
cimig = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("ci_migration_chain_check", cimig)
_spec.loader.exec_module(cimig)

ALLOWED = cimig.ALLOWED_MIGRATION_DB  # "skipline_ci_migrate"


def good_env(**over):
    env = {
        "DB_NAME": ALLOWED,
        "DB_HOST": "127.0.0.1",
        "DB_PORT": "3306",
        "DB_USER": "ci-synthetic-user",
        "DB_PASSWORD": "ci-synthetic-password-not-real",
    }
    env.update(over)
    return env


# ── validate_ci_config: accepted configuration ────────────────────────────────

@pytest.mark.unit
def test_accepts_exact_allowlist_name():
    cfg = cimig.validate_ci_config(good_env())
    assert cfg["db_name"] == ALLOWED
    assert cfg["db_host"] == "127.0.0.1"
    assert cfg["db_port"] == 3306


@pytest.mark.unit
def test_accepts_localhost_host():
    cfg = cimig.validate_ci_config(good_env(DB_HOST="localhost"))
    assert cfg["db_host"] == "localhost"


@pytest.mark.unit
def test_defaults_applied_when_host_port_missing():
    env = good_env()
    del env["DB_HOST"]
    del env["DB_PORT"]
    cfg = cimig.validate_ci_config(env)
    assert cfg["db_host"] == "127.0.0.1"
    assert cfg["db_port"] == 3306


@pytest.mark.unit
def test_whitespace_only_around_values_is_tolerated():
    cfg = cimig.validate_ci_config(good_env(DB_NAME=" " + ALLOWED + " ",
                                            DB_HOST=" localhost "))
    assert cfg["db_name"] == ALLOWED
    assert cfg["db_host"] == "localhost"


# ── validate_ci_config: rejected database names ───────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("bad", [
    "skipline",                     # the real project database
    "SKIPLINE",                     # production-like, different case
    "skipline_production",
    "skipline_prod",
    "skipline_ci",                  # the pytest-job DB — NOT the migration job
    "skipline_ci_migrate2",
    "skipline_ci_migration",        # prefix look-alike, not exact
    "ci_migrate",
    "mysql",
    "information_schema",
    "skipline_ci_smoke",            # ad-hoc legacy smoke name
])
def test_rejects_every_non_exact_database_name(bad):
    with pytest.raises(SystemExit) as ei:
        cimig.validate_ci_config(good_env(DB_NAME=bad))
    assert "REFUSING TO RUN" in str(ei.value)


@pytest.mark.unit
@pytest.mark.parametrize("empty", ["", "   ", None])
def test_rejects_empty_database_name(empty):
    with pytest.raises(SystemExit) as ei:
        cimig.validate_ci_config(good_env(DB_NAME=empty))
    msg = str(ei.value)
    assert "REFUSING TO RUN" in msg and "empty" in msg


# ── validate_ci_config: rejected connection hosts ─────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("host", [
    "db.internal.example.com",
    "10.0.0.5",
    "192.168.1.10",
    "0.0.0.0",
    "::1",
])
def test_rejects_non_loopback_hosts(host):
    # NOTE: DB_HOST="" falls back to the loopback default by design
    # (test_defaults_applied_when_host_port_missing) — it is not rejected.
    with pytest.raises(SystemExit) as ei:
        cimig.validate_ci_config(good_env(DB_HOST=host))
    assert "REFUSING TO RUN" in str(ei.value)


# ── validate_ci_config: rejected ports ────────────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("port", [
    "not-a-number",
    "3306.0",
    "0",
    "-1",
    "65536",
    "99999",
])
def test_rejects_invalid_ports(port):
    # NOTE: DB_PORT="" falls back to the default 3306 by design.
    with pytest.raises(SystemExit):
        cimig.validate_ci_config(good_env(DB_PORT=port))


@pytest.mark.unit
def test_accepts_boundary_ports():
    for p in ("1", "65535"):
        assert cimig.validate_ci_config(good_env(DB_PORT=p))["db_port"] == int(p)


# ── validate_ci_config: rejected credentials ─────────────────────────────────

@pytest.mark.unit
@pytest.mark.parametrize("user", ["", "   ", None])
def test_rejects_empty_user(user):
    with pytest.raises(SystemExit) as ei:
        cimig.validate_ci_config(good_env(DB_USER=user))
    assert "REFUSING TO RUN" in str(ei.value)


@pytest.mark.unit
@pytest.mark.parametrize("pw", ["", "   ", None])
def test_rejects_empty_password(pw):
    with pytest.raises(SystemExit) as ei:
        cimig.validate_ci_config(good_env(DB_PASSWORD=pw))
    assert "REFUSING TO RUN" in str(ei.value)


# ── drop_disposable_check: the destructive-operation hard guard ──────────────

@pytest.mark.unit
def test_drop_guard_grants_only_exact_allowlist():
    assert cimig.drop_disposable_check(ALLOWED) is None


@pytest.mark.unit
@pytest.mark.parametrize("target", [
    "skipline",                  # real project DB
    "skipline_ci",
    "skipline_ci_migrate2",
    "skipline_ci_migrated",
    "skipline_ci_migrat",        # truncation of the allowlist entry
    "mysql",
    "sys",
    "information_schema",
    "performance_schema",
    "",
    "SKipline_ci_migrate",
    "skipline_ci_migrate; DROP DATABASE skipline",  # injection-shaped string
])
def test_drop_guard_refuses_everything_else(target):
    with pytest.raises(SystemExit) as ei:
        cimig.drop_disposable_check(target)
    assert "SAFETY GATE TRIPPED" in str(ei.value)


# ── redact: credentials never survive into output ────────────────────────────

@pytest.mark.unit
def test_redact_removes_password_and_url():
    pw = "ci-synthetic-password-not-real"
    url = f"mysql+pymysql://ci-synthetic-user:{pw}@127.0.0.1:3306/{ALLOWED}"
    out = cimig.redact(
        f"error connecting to {url} with {pw}", url, pw)
    assert pw not in out
    assert url not in out
    assert "***" in out
    assert "<disposable-db-url>" in out


@pytest.mark.unit
def test_redact_handles_empty_password():
    out = cimig.redact("plain message", "", "")
    assert out == "plain message"


# ── allowlist constants sanity ────────────────────────────────────────────────

@pytest.mark.unit
def test_allowlist_is_the_documented_disposable_name():
    assert ALLOWED == "skipline_ci_migrate"
    assert cimig.ALLOWED_MIGRATION_HOSTS == ("127.0.0.1", "localhost")


@pytest.mark.unit
def test_module_import_has_no_side_effects(tmp_path):
    """Reloading must not chdir/create engines/execute anything."""
    import importlib
    spec = importlib.util.spec_from_file_location("cimig_reload", _SCRIPT)
    m2 = importlib.util.module_from_spec(spec)
    before = os.getcwd()
    spec.loader.exec_module(m2)
    assert os.getcwd() == before
    assert not list(tmp_path.iterdir())


# ══ pre-activation destructive-safety review (final): preservation of a ══
# ══ PRE-EXISTING database — regression tests for the ownership rule     ══

class _RecordingCursor:
    """Fake SQLAlchemy cursor: records every executed statement."""

    def __init__(self, recorder):
        self._rec = recorder

    def execute(self, stmt, params=None):
        self._rec.append(str(stmt))
        self._rec.append(params)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _RecordingEngine:
    def __init__(self, recorder=None):
        self._rec = recorder if recorder is not None else []
        self.disposed = False

    def connect(self):
        return _RecordingCursor(self._rec)

    def dispose(self):
        self.disposed = True


@pytest.mark.unit
def test_preexisting_db_is_refused_never_touched():
    """A pre-existing skipline_ci_migrate on the server must cause a
    refusal — NOT a drop. (End-to-end behavioral proof of this rule against
    a disposable MySQL server follows as a script-level smoke; this unit
    pins the exact policy function.)"""
    with pytest.raises(SystemExit) as ei:
        cimig.refuse_if_preexisting(
            ["mysql", "sys", "information_schema", ALLOWED], ALLOWED)
    msg = str(ei.value)
    assert "REFUSING TO RUN" in msg
    assert "already exists" in msg
    assert "never drops a database it did not create" in msg


@pytest.mark.unit
def test_absent_db_proceeds_to_creation():
    assert cimig.refuse_if_preexisting(["mysql", "sys"], ALLOWED) is None


@pytest.mark.unit
def test_refuse_if_preexisting_ignores_lookalike_names():
    """Only the EXACT allowlist name blocks the run; every other existing
    database (even look-alikes) is irrelevant to the check."""
    for existing in (
        ["skipline"],
        ["skipline_ci"],
        ["skipline_ci_migrate2"],
        [],
    ):
        assert cimig.refuse_if_preexisting(existing, ALLOWED) is None


@pytest.mark.unit
def test_teardown_allowed_requires_ownership_flag():
    """Teardown policy: exact name + created-by-this-run."""
    assert cimig.teardown_allowed(ALLOWED, True) is True
    assert cimig.teardown_allowed(ALLOWED, False) is False
    with pytest.raises(SystemExit):
        cimig.teardown_allowed("skipline", True)          # non-allowlist name
    with pytest.raises(SystemExit):
        cimig.teardown_allowed("skipline_ci_migrate2", True)


@pytest.mark.unit
def test_perform_drop_executes_only_the_exact_guarded_ddl():
    """With an injected recording engine, prove the ONLY destructive
    statement ever issued names exactly skipline_ci_migrate."""
    rec: list = []
    eng = _RecordingEngine(rec)
    cimig.perform_drop(ALLOWED, lambda: eng, lambda m: m)
    drop_stmts = [s for s in rec if isinstance(s, str) and s.startswith("DROP")]
    assert drop_stmts == [f"DROP DATABASE IF EXISTS {ALLOWED}"]
    assert eng.disposed is True


@pytest.mark.unit
def test_perform_drop_refuses_any_other_name_before_connecting():
    """No engine is created and no SQL is sent for a guarded-out target —
    a wrong/production-like name can NEVER reach destructive SQL."""
    created_engines: list = []

    def engine_factory():
        eng = _RecordingEngine()
        created_engines.append(eng)
        return eng

    for bad in ("skipline", "skipline_ci", "skipline_ci_migrate2", ""):
        with pytest.raises(SystemExit) as ei:
            cimig.perform_drop(bad, engine_factory, lambda m: m)
        assert "SAFETY GATE TRIPPED" in str(ei.value)
    assert created_engines == []          # guard fired before even creating


@pytest.mark.unit
def test_perform_drop_disposes_engine_on_failure():
    """If the DROP itself raises, the injected engine is still disposed and
    the message is redacted via the injected redactor."""
    class FailEngine:
        class _Conn:
            def execute(self, stmt): raise RuntimeError("boom q.mysql+pymysql://u:p@h:1/d")
            def commit(self): pass
            def __enter__(self): return self
            def __exit__(self, *e): return False
        def connect(self): return FailEngine._Conn()
        def dispose(self): FailEngine.disposed = True
    FailEngine.disposed = False
    cimig.perform_drop(ALLOWED, FailEngine,
                       lambda m: m.replace("p", "***") if "p" in m else m)
    assert FailEngine.disposed is True
