"""
tests/acceptance/conftest.py
───────────────────────────
Browser acceptance-suite harness (Playwright + the REAL application stack).

Session lifecycle (ALL disposable, owned-cleanup doctrine):

1. Creates a uniquely-named disposable MySQL database
   (`skipline_acceptance_<UTC stamp>_<rand>`). A pre-existing name is
   REFUSED, never dropped. Protected names (skipline_phase9_prodcheck,
   skipline_synccheck, skipline, ...) are unreachable by construction.
2. Applies the real Alembic migration chain (0001→0018).
3. Seeds representative rows through the project's own ORM models
   (organisation + user + branch + employee + counters + services +
   a 3-stage workflow with mandatory/optional stages).
4. Starts ONE deterministic mock AI provider process
   (scripts/mock_ai_provider.py — never a paid provider, never real keys).
5. Starts ONE real uvicorn process bound to the disposable database
   (APP_ENV=development) and waits for its /health endpoint.
6. On session teardown: kills BOTH subprocesses and drops ONLY the
   database created by this run.

Terminal output never contains credentials.
"""
import asyncio
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[2]
IS_WINDOWS = os.name == "nt"
VENV_PY = str(PROJECT_ROOT / "venv" / "Scripts" / "python.exe" if IS_WINDOWS
               else PROJECT_ROOT / "venv" / "bin" / "python")

ARTIFACT_DIR = PROJECT_ROOT / "test-results" / "acceptance"

# Materialised test credentials (test-only, decrypted fixture state seen by
# the local disposable DB). NOT real production credentials.
PASSWORD = "AcceptancePass#2026"
EMPLOYEE_PASSWORD = "EmpAcceptance#2026"


def _env_db() -> dict[str, str]:
    """Load DB connection settings with .env file PRECEDENCE.

    The parent tests/conftest.py injects dummy values into os.environ for
    the hermetic suite, so process-env DB material cannot be trusted here
    (it may be the dummy) — .env file values win. Nothing is printed.
    """
    file_vals: dict[str, str] = {}
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            file_vals[k.strip()] = v.strip().strip('"').strip("'")
    dummy = "test-db-password-dummy"
    out = {}
    for key in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD"):
        val = file_vals.get(key) or os.environ.get(key) or ""
        if val == dummy:
            val = file_vals.get(key, "")
        out[key] = val
    out["DB_HOST"] = out["DB_HOST"] or "127.0.0.1"
    out["DB_PORT"] = out["DB_PORT"] or "3306"
    out["DB_USER"] = out["DB_USER"] or "skipline_app"  # WP2 least-privilege identity
    return out


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _engine_url(connect_db: str, db: dict, driver: str = "pymysql") -> str:
    import urllib.parse as up
    return (f"mysql+{driver}://{db['DB_USER']}:"
            f"{up.quote_plus(db['DB_PASSWORD'])}@"
            f"{db['DB_HOST']}:{db['DB_PORT']}/{connect_db}")


@dataclass
class AcceptanceServer:
    base_url: str
    ai_base_url: str
    db_name: str
    runstamp: str
    org_name: str
    service_name: str
    counter_name: str
    user1_email: str
    user2_email: str
    org_email: str
    org_password: str
    user_password: str
    employee_email: str
    employee_password: str
    db: dict = field(repr=False, default_factory=dict)
    # NOTE: annotated so they become dataclass fields — the fixture passes
    # engine=/sessionmaker=/loop= kwargs at construction (unannotated class
    # attributes would reject them with TypeError).
    engine: object | None = None
    sessionmaker: object | None = None
    loop: object | None = None
    processes: list = field(repr=False, default_factory=list)

    # ── WP1 loop-agnostic DB helpers (2026-11-10) ─────────────────────────
    # The engine (and every aiomysql transport) is bound to the server's
    # PRIVATE loop that runs on its own thread (see the `server` fixture).
    # pytest-asyncio 0.23.x proved unable to run tests and that fixture on
    # one shared loop despite scoped marks — so these helpers are SYNC from
    # the test's perspective and hop to the server loop via
    # run_coroutine_threadsafe. No "attached to a different loop" is
    # possible any more, for ANY pytest-asyncio version.
    def db_exec(self, stmt: str, params: dict | None = None):
        """Verification helper: run one statement against the disposable DB."""
        fut = _threadsafe(self._db_exec(stmt, params), self.loop)
        return fut.result(timeout=120)

    async def _db_exec(self, stmt: str, params: dict | None = None):
        async with self.sessionmaker() as session:
            res = await session.execute(sa.text(stmt), params or {})
            await session.commit()
            return res

    def db_fetchall(self, stmt: str, params: dict | None = None):
        return self.db_exec(stmt, params).fetchall()

    def db_fetchone(self, stmt: str, params: dict | None = None):
        return self.db_exec(stmt, params).fetchone()

    def db_count(self, table: str, where: str = "1=1") -> int:
        rows = self.db_fetchall(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}")
        return rows[0][0]


def _threadsafe(coro, loop) -> "asyncio.Future":
    """Schedule a coroutine on the server's private loop."""
    assert loop is not None, "server loop is not running"
    return asyncio.run_coroutine_threadsafe(coro, loop)


def _log(msg: str) -> None:
    print(f"[acceptance] {msg}", flush=True)


@pytest.fixture(scope="session")
def server():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    db = _env_db()
    if not db["DB_PASSWORD"]:
        pytest.skip("No DB credentials available for the acceptance harness")

    db_name = f"skipline_acceptance_{stamp}_{uuid.uuid4().hex[:6]}"
    runstamp = f"{stamp}{uuid.uuid4().hex[:6]}"

    import urllib.parse as up
    # SYNC admin engine (database create/drop) — aiomysql cannot sync-connect;
    # pymysql is the sync driver verified by the migration chain.
    server_url = (f"mysql+pymysql://{db['DB_USER']}:"
                  f"{up.quote_plus(db['DB_PASSWORD'])}@"
                  f"{db['DB_HOST']}:{db['DB_PORT']}/")
    admin_engine = sa.create_engine(server_url, pool_pre_ping=True)

    with admin_engine.connect() as c:
        existing = {r[0] for r in c.execute(sa.text("SHOW DATABASES"))}
        if db_name in (existing or set()):
            pytest.fail(f"Acceptance DB {db_name} already exists — refusing")
        forbidden = {n for n in existing if n in (
            "skipline", "skipline_phase9_prodcheck", "skipline_synccheck")}
        assert not forbidden & {db_name}
        c.execute(sa.text(
            f"CREATE DATABASE `{db_name}` CHARACTER SET utf8mb4 "
            "COLLATE utf8mb4_unicode_ci"))
        c.commit()
    admin_engine.dispose()
    _log(f"disposable DB {db_name} created")

    # Any failure BEFORE the yield must still drop the DB this run
    # created (owned-cleanup doctrine applies on every exit path).
    #
    # ── WP1 (2026-11-10): PRIVATE SERVER LOOP ─────────────────────────────
    # The server's async engine and every aiomysql transport live on their
    # own event loop, running on this dedicated thread. pytest-asyncio
    # 0.23.x runs each test — and its `page` fixture — on per-test loops,
    # and its scoped-mark loop unification does NOT reliably bind them to
    # the session fixture's loop (run 5/6 evidence: "Task ... got Future
    # ... attached to a different loop", teardown "Event loop is closed").
    # Hoping the loops coincide is version-dependent and fragile; instead,
    # ALL server DB access hops onto this private loop through
    # run_coroutine_threadsafe (see AcceptanceServer.db_exec). The test's
    # loop never touches the engine, for ANY pytest-asyncio version.
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(
        target=lambda: loop.run_forever(),
        name="acceptance-server-loop", daemon=True)
    loop_thread.start()

    def run_coro(coro, timeout=900):
        """Run a coroutine ON the private server loop, block for its result."""
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)

    engine = None
    try:
        # ── 2. real migrations (0001→0018) against the disposable DB ────────
        mig_env = dict(os.environ)
        # The parent hermetic conftest put a dummy DB_PASSWORD into
        # os.environ — the subprocesses MUST use the real local connection
        # material from .env (masked; never printed).
        mig_env.update({
            k: db[k] for k in ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD")
        })
        mig_env.update({
            "DB_NAME": db_name, "SECRET_KEY": "test-acceptance-secret-key-x!0123456789abcdef",
            "JWT_SECRET_KEY": "test-acceptance-jwt-secret-x!0123456789abcdef",
            "ADMIN_PASSWORD": "",  # admin seeding intentionally skipped (dev warning only)
            "APP_ENV": "development",
        })
        mig = subprocess.run(
            [VENV_PY, "-m", "alembic", "upgrade", "head"],
            cwd=str(PROJECT_ROOT), env=mig_env,
            capture_output=True, text=True, timeout=900)
        if mig.returncode != 0:
            raise RuntimeError(
                "alembic upgrade head failed on the acceptance DB:\n"
                + (mig.stdout + mig.stderr)[-2000:])
        _log("migration chain applied")

        # ── 3. seed via the project's own ORM models ─────────────────────────
        engine = create_async_engine(_engine_url(db_name, db, driver="aiomysql"))
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)

        import models  # noqa: F401 — registers all ORM tables
        from models.branch import Branch
        from models.counter import Counter, CounterStatus
        from models.organization import Organization, OrgStatus
        from models.service import Service
        from models.user import User
        from models.workflow import ServiceWorkflow, WorkflowStage
        from app.utils.security import hash_password
        from app.services import employee_service
        from tests.phase7_helpers import unique_suffix

        org_name = f"Acceptance AI Clinic {runstamp}"
        service_name = f"AI Offering {runstamp}"
        counter_name = f"AC Counter {runstamp}"
        # NOTE(WP1): NOT the RFC-reserved ".test" TLD — pydantic v2
        # EmailStr (email-validator) rejects special-use reserved names
        # ("The part after the @-sign is a special-use or reserved name"),
        # which made every JSON /auth/*/login a 422 while UI form logins
        # (no EmailStr) kept working. ".dev" is a real gTLD and validates.
        user1_email = f"user1_{runstamp}@acceptance.dev"
        user2_email = f"user2_{runstamp}@acceptance.dev"
        org_email = f"org_{runstamp}@acceptance.dev"
        employee_email = f"employee_{runstamp}@acceptance.dev"

        async def _seed():
            """Seed the disposable DB ON the private loop (returns row ids)."""
            row_ids: dict = {}
            async with sessionmaker() as s:
                org = Organization(
                    name=org_name,
                    registration_number=f"AC-{runstamp}",
                    org_type="hospital",
                    address="1 Acceptance Street",
                    city="Metro",
                    state="ST",
                    email=org_email,
                    mobile="9876543210",
                    password_hash=hash_password(PASSWORD),
                    status=OrgStatus.APPROVED,
                    is_active=True,
                )
                s.add(org)
                await s.flush()
                numeric_stamp = stamp[:6] + stamp[-6:]  # digits only (mobile/counter)
                s.add(User(
                    name="Customer One",
                    email=user1_email,
                    mobile=f"900{numeric_stamp}1",
                    password_hash=hash_password(PASSWORD),
                    is_active=True,
                ))
                s.add(User(
                    name="Customer Two",
                    email=user2_email,
                    mobile=f"900{numeric_stamp}2",
                    password_hash=hash_password(PASSWORD),
                    is_active=True,
                ))
                branch = Branch(
                    organization_id=org.id,
                    name=f"Branch {runstamp}",
                    branch_code=f"B{runstamp[-6:]}",
                    address="1 Acceptance Street",
                    city="Metro",
                    state="ST",
                    pincode="560001",
                )
                s.add(branch)
                await s.flush()
                s.add(Counter(
                    organization_id=org.id,
                    branch_id=branch.id,
                    counter_name=counter_name,
                    counter_number=int(numeric_stamp[-6:]),
                    token_prefix="AC",
                    daily_limit=100,
                    queue_mode="day_queue",
                    status=CounterStatus.ACTIVE,
                ))
                svc = Service(
                    organization_id=org.id,
                    name=service_name,
                    estimated_minutes=10,
                    price=Decimal("49.00"),
                    is_active=True,
                    requires_identity_verification=False,
                )
                s.add(svc)
                await s.flush()
                wf = ServiceWorkflow(
                    organization_id=org.id,
                    service_id=svc.id,
                    name=f"Workflow {runstamp}",
                )
                s.add(wf)
                await s.flush()
                s.add(WorkflowStage(
                    workflow_id=wf.id, organization_id=org.id,
                    code="registration", name="Registration",
                    position=1, is_mandatory=True, estimated_minutes=3))
                s.add(WorkflowStage(
                    workflow_id=wf.id, organization_id=org.id,
                    code="verification", name="Verification",
                    position=2, is_mandatory=True, estimated_minutes=4))
                s.add(WorkflowStage(
                    workflow_id=wf.id, organization_id=org.id,
                    code="consultation", name="Consultation",
                    position=3, is_mandatory=False, estimated_minutes=5))
                await s.flush()
                employee = await employee_service.create_employee(
                    s, org.id,
                    {
                        "name": f"Operator {runstamp}",
                        "email": employee_email,
                        "employee_code": f"AC-{runstamp[-6:]}",
                        "branch_id": branch.id,
                        "password": EMPLOYEE_PASSWORD,
                    })
                await s.commit()
                row_ids["svc_id"] = svc.id
                row_ids["org_id"] = org.id
                row_ids["employee_id"] = employee.id

            # capture row ids created above (autoincrement across connections)
            async with sessionmaker() as s:
                for var, email in (("user1_id", user1_email),
                                   ("user2_id", user2_email)):
                    row = (await s.execute(
                        sa.text("SELECT id FROM users WHERE email = :e"),
                        {"e": email})).fetchone()
                    row_ids[var] = row[0]
                counter_row = (await s.execute(
                    sa.text(
                        "SELECT id FROM counters WHERE organization_id = :o "
                        "ORDER BY id LIMIT 1"),
                    {"o": row_ids["org_id"]})).fetchone()
                row_ids["counter_id"] = counter_row[0]
            return row_ids

        ids = run_coro(_seed())
        org_id = ids["org_id"]
        svc_id = ids["svc_id"]
        counter_id = ids["counter_id"]
        user1_id = ids["user1_id"]
        user2_id = ids["user2_id"]
        _log(f"seeded: org={org_id} counter={counter_id} service={svc_id}")

        # ── 4+5. mock AI provider process + real uvicorn process ────────────
        ai_port = _free_port()
        app_port = _free_port()
        ai_proc = subprocess.Popen(
            [VENV_PY, str(PROJECT_ROOT / "scripts" / "mock_ai_provider.py"),
             "--port", str(ai_port)],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace")

        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if ai_proc.poll() is not None:
                    raise RuntimeError(
                        "mock AI provider exited: " + ai_proc.stdout.read())
                with httpx.Client() as hc:
                    r = hc.get(f"http://127.0.0.1:{ai_port}/healthz", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.3)
        else:
            ai_proc.kill()
            raise RuntimeError("mock AI provider never became healthy")
        _log(f"mock AI provider healthy on :{ai_port}")

        uv_env = dict(mig_env)
        uv_env.update({
            "DB_NAME": db_name,
            "AI_PROVIDER": "mock",
            "AI_BASE_URL": f"http://127.0.0.1:{ai_port}/v1",
            "AI_API_KEY": "synthetic-acceptance-key-0123456789abcdef",
            "AI_MODEL": "mock-model",
            "LOG_DIR": "",  # stderr-only locally; compose sets /app/logs instead
        })
        uv_out_path = Path(os.environ.get("TEMP", tempfile.gettempdir())) / (
            f"opencode_acceptance_uvicorn_{runstamp}.log")
        uv_out_path.parent.mkdir(parents=True, exist_ok=True)
        # WP1 evidence (2026-11-09): with stdout on a PIPE the uvicorn
        # child repeatedly froze mid-boot in this environment (3/3 reps —
        # SQLAlchemy DESCRIBE chain stall, ~4 KB of buffered echo output;
        # file-redirected stdout boots fully healthy 1/1). The stall did
        # not reproduce in isolated create_all diagnostics (6/6 OK), so it
        # is a child-pipe/echo interaction, not an application defect.
        uv_out = open(uv_out_path, "w", encoding="utf-8")
        uv_proc = subprocess.Popen(
            [VENV_PY, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(app_port),
             "--log-level", "warning"],
            cwd=str(PROJECT_ROOT), env=uv_env,
            stdout=uv_out, stderr=subprocess.STDOUT)

        base = f"http://127.0.0.1:{app_port}"
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                if uv_proc.poll() is not None:
                    uv_out.flush()
                    raise RuntimeError(
                        "uvicorn exited during startup: "
                        + uv_out_path.read_text(encoding="utf-8",
                                                errors="replace")[-4000:])
                with httpx.Client() as hc:
                    r = hc.get(f"{base}/health", timeout=2)
                if r.status_code == 200:
                    break
            except Exception:
                time.sleep(0.5)
        else:
            uv_proc.kill()
            uv_out.flush()
            tail = uv_out_path.read_text(encoding="utf-8", errors="replace")
            uv_out.close()
            raise RuntimeError(
                "uvicorn never became healthy; startup output tail:\n" + tail[-4000:])
        uv_out.close()
        _log(f"app healthy on :{app_port} (disposable DB {db_name})")

        ctx = AcceptanceServer(
            base_url=base,
            ai_base_url=f"http://127.0.0.1:{ai_port}",
            db_name=db_name,
            runstamp=runstamp,
            org_name=org_name,
            service_name=service_name,
            counter_name=counter_name,
            user1_email=user1_email,
            user2_email=user2_email,
            org_email=org_email,
            org_password=PASSWORD,
            user_password=PASSWORD,
            employee_email=employee_email,
            employee_password=EMPLOYEE_PASSWORD,
            db=db,
            engine=engine,
            sessionmaker=sessionmaker,
            loop=loop,
            processes=[uv_proc, ai_proc],
        )
        ctx.service_id = svc_id
        ctx.counter_id = counter_id
        ctx.org_id = org_id
        ctx.employee_id = ids["employee_id"]
        ctx.user1_id = user1_id
        ctx.user2_id = user2_id
        yield ctx
    except Exception:
        # Best-effort owned teardown on setup failure (all sync forms).
        try:
            if engine is not None:
                run_coro(engine.dispose(), timeout=120)
        except Exception:
            pass
        try:
            with sa.create_engine(server_url).connect() as c:
                c.execute(sa.text(f"DROP DATABASE IF EXISTS `{db_name}`"))
                c.commit()
        except Exception:
            pass
        raise

    # ── teardown (all paths): kill procs, dispose, stop loop, drop DB ──
    for proc in ctx.processes:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
    try:
        run_coro(engine.dispose(), timeout=120)
    except Exception:
        pass
    try:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=10)
    except Exception:
        pass
    with sa.create_engine(server_url).connect() as c:
        c.execute(sa.text(f"DROP DATABASE IF EXISTS `{db_name}`"))
        c.commit()
    _log(f"teardown: processed terminated; {db_name} dropped (owned)")


@pytest_asyncio.fixture
async def page(request, server):
    """One fresh browser page per test with console/page-error capture and
    always-written failure artifacts (screenshot + HTML + console log)."""
    from playwright.async_api import async_playwright

    test_name = request.node.name
    artifacts = []
    console_lines: list[str] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            base_url=server.base_url,
            viewport={"width": 1280, "height": 900},
        )
        pg = await context.new_page()
        pg.on(
            "pageerror",
            lambda err: console_lines.append(f"PAGEERROR: {err}"))
        pg.on(
            "console",
            lambda msg: console_lines.append(f"CONSOLE[{msg.type}]: {msg.text}")
            if msg.type in ("error", "warning") else None)
        yield pg
        try:
            ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            safe = test_name.replace("/", "_").replace("[", "_").replace("]", "_")
            shot = ARTIFACT_DIR / f"{safe}.png"
            await pg.screenshot(path=str(shot), full_page=True)
            artifacts.append(shot.name)
            (ARTIFACT_DIR / f"{safe}.console.log").write_text(
                "\n".join(console_lines) or "(no console errors)", encoding="utf-8")
        except Exception:
            pass
        await context.close()
        await browser.close()


# ── shared helpers for flow tests ─────────────────────────────────────────
async def ui_login(page, portal: str, email: str, password: str) -> None:
    """Log in through the real rendered form for portal admin|org|user|employee."""
    await page.goto(f"/{portal}/login", wait_until="domcontentloaded")
    await page.fill('input[name="email"]', email)
    await page.fill('#passwordField', password)
    await page.click("#loginBtn")
    await page.wait_for_url(f"**/{portal}/dashboard", timeout=20000)


async def api_login_cookie(base_url: str, portal: str,
                           email: str, password: str) -> dict:
    """Real JSON login route → cookies, for httpx-based boundary checks."""
    async with httpx.AsyncClient(base_url=base_url) as hc:
        r = await hc.post(f"/auth/{portal}/login",
                          json={"email": email, "password": password})
        r.raise_for_status()
        # httpx 0.27 Cookies has no requests-style get_dict(); build the
        # mapping from the cookie jar directly.
        return {c.name: c.value for c in hc.cookies.jar}
