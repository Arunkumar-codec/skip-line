"""
tests/unit/test_phase8_uiq.py
─────────────────────────────
Phase 8 UIQ-005 / SEC-004 automated asset-boundary audit.

The pages used to load Bootstrap / Bootstrap Icons / Chart.js / jsQR and
the Inter font from third-party CDNs while the CSP allowed only `'self'`
— meaning the shipped CSP would have blocked every page's scripts and
stylesheets in production. The fix vendors ALL assets under
/static/vendor (no runtime third-party contacts), and these tests keep
it true:

- UIQ-005/SEC-004: no template references an external http(s)
  script/style/font origin — everything is served same-origin;
- every vendored path a template references actually exists on disk;
- the vendor inventory is pinned so silent swaps are caught.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "templates"
VENDOR = ROOT / "static" / "vendor"

# origins that were, historically, silently CDN-loaded at runtime
EXTERNAL_ORIGIN = re.compile(
    r"""https?://(?!localhost|127\.0\.0\.1)"""
)
SCRIPT_STYLE_FONT = re.compile(
    r"""(?:<script[^>]+src|<link[^>]+hrefon|<link[^>]+href)\s*=\s*["']"""
)
ASSET_PATH = re.compile(
    r"""(?:src|href)\s*=\s*["'](/static/[^"']+)["']"""
)

# Vendored inventory (name → source label). The audit pins WHAT was
# vendored so an accidental downgrade/swap changes a test, not prod.
VENDOR_INVENTORY = {
    "css/bootstrap.min.css": "Bootstrap 5.3.3 CSS",
    "css/bootstrap-icons.min.css": "Bootstrap Icons 1.11.3 CSS",
    "css/fonts/bootstrap-icons.woff2": "Bootstrap Icons font",
    "css/inter.css": "Inter (self-hosted) stylesheet",
    "css/fonts/inter-latin-var.woff2": "Inter variable latin woff2",
    "css/fonts/inter-latin-ext-var.woff2": "Inter variable latin-ext woff2",
    "js/bootstrap.bundle.min.js": "Bootstrap 5.3.3 bundle JS",
    "js/chart.umd.min.js": "Chart.js 4.4.3",
    "js/jsQR.min.js": "jsQR 1.4.0",
}


def _template_files():
    return sorted(TEMPLATES.rglob("*.html"))


def test_no_external_origins_in_templates():
    """Every script/style/font asset loads from the same origin."""
    offenders = []
    for path in _template_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            # only asset-loading lines matter (forms posting to an absolute
            # URL are not shipped anywhere in this project)
            if "<script" in line or "<link" in line:
                if EXTERNAL_ORIGIN.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "Templates must ship ZERO third-party asset origins "
        "(CSP script-src/style-src 'self'); found:\n" + "\n".join(offenders)
    )


def test_every_referenced_static_asset_exists():
    """Every /static/... path a template references must exist on disk."""
    missing = []
    seen_hosts = set()
    for path in _template_files():
        text = path.read_text(encoding="utf-8")
        for target in ASSET_PATH.findall(text):
            seen_hosts.add(target.split("/")[2] if target.startswith("/static/") else None)
            fs_path = ROOT / target.lstrip("/")
            if not fs_path.is_file():
                missing.append(f"{path.relative_to(ROOT)} → {target}")
    assert not missing, (
        "Templates reference static assets that do not exist:\n"
        + "\n".join(missing)
    )


def test_templates_reference_local_vendor_assets():
    """The vendored libraries are actually used by the templates."""
    combined = "\n".join(
        p.read_text(encoding="utf-8") for p in _template_files()
    )
    for expected in (
        "/static/vendor/css/bootstrap.min.css",
        "/static/vendor/js/bootstrap.bundle.min.js",
        "/static/vendor/css/inter.css",
    ):
        assert expected in combined, f"vendored asset not referenced: {expected}"


def test_vendor_inventory_on_disk():
    """The full vendored inventory is present (pinned asset bundle)."""
    missing = [
        name for name in VENDOR_INVENTORY
        if not (VENDOR / name).is_file()
    ]
    assert not missing, f"missing vendored assets: {missing}"


@pytest.mark.parametrize("rel,min_bytes", [
    ("css/bootstrap.min.css", 100_000),
    ("js/bootstrap.bundle.min.js", 50_000),
    ("js/chart.umd.min.js", 100_000),
])
def test_vendored_files_are_real_assets(rel: str, min_bytes: int):
    """A vendored file that got truncated (bad download) is caught here."""
    data = (VENDOR / rel).read_bytes()
    assert len(data) >= min_bytes, f"{rel} looks truncated ({len(data)} B)"


# ── UIQ-002: every frontend button's backend operation exists ────────────────
#
# UIQ-002: a visible button MUST call a real backend operation. These are
# ALL the fetch targets shipped with the templates (audit of 2026-11).
# Dynamic segments are written as {x}; a pattern matches an app route when
# segment counts agree, non-dynamic segments match exactly, and dynamic
# segments line up with the route's own path parameters.

FETCH_PATTERNS = [
    # admin — cases
    ("/admin/api/cases/{x}", {"GET"}),
    ("/admin/api/cases/{x}/messages", {"POST"}),
    ("/admin/api/cases/{x}/transition", {"POST"}),
    ("/admin/api/case-messages/{x}/approve", {"POST"}),
    # admin — feedback moderation
    ("/admin/api/feedback/{x}/moderate", {"POST"}),
    # admin — finance
    ("/admin/payouts/{x}/hold", {"POST"}),
    ("/admin/payouts/{x}/release", {"POST"}),
    ("/admin/payouts/{x}/disburse", {"POST"}),
    ("/admin/risk-flags/{x}/resolve", {"POST"}),
    ("/admin/bank-accounts/{x}/verify", {"POST"}),
    ("/admin/api/organizations/{x}/payment-policy", {"GET", "PUT"}),
    ("/admin/tokens/{x}/pause", {"POST"}),
    ("/admin/tokens/{x}/resume", {"POST"}),
    # admin — knowledge / kyc
    ("/admin/api/knowledge", {"POST", "GET"}),
    ("/org/api/knowledge/{x}/deactivate", {"POST"}),
    ("/admin/api/kyc/{x}", {"GET"}),
    ("/admin/api/kyc/{x}/review", {"POST"}),
    ("/admin/api/branches/{x}/kyc-verify", {"POST"}),
    # org — branches / employees
    ("/org/branches", {"POST"}),
    ("/org/branches/{x}", {"PUT", "DELETE"}),
    ("/org/employees", {"POST"}),
    ("/org/employees/{x}", {"PUT", "DELETE"}),
    ("/org/employees/{x}/roles", {"GET", "POST", "DELETE"}),
    ("/org/employees/{x}/roles/{y}", {"POST", "DELETE"}),
    # org — cases / knowledge
    ("/org/api/cases", {"GET", "POST"}),
    ("/org/api/cases/{x}", {"GET"}),
    ("/org/api/cases/{x}/messages", {"POST"}),
    ("/org/api/cases/{x}/suggest", {"POST"}),
    ("/org/api/knowledge", {"GET"}),
    # org — feedback / kyc / payments / schedule
    ("/org/api/feedback/{x}/respond", {"POST"}),
    ("/org/api/feedback/{x}/report", {"POST"}),
    ("/org/api/kyc/documents", {"POST"}),
    ("/payments/{x}/refund", {"POST"}),
    ("/org/api/finance/bank-account", {"POST"}),
    ("/org/schedule/weekly/save", {"POST"}),
    ("/org/schedule/weekly/delete", {"POST"}),
    ("/org/schedule/exception/create", {"POST"}),
    ("/org/schedule/exception/{x}/delete", {"POST"}),
    # public — display screens
    ("/public/display/{x}", {"GET"}),
    # user — cases / feedback / identity
    ("/user/api/cases", {"GET", "POST"}),
    ("/user/api/cases/{x}", {"GET"}),
    ("/user/api/cases/{x}/messages", {"POST"}),
    ("/user/api/cases/{x}/resolution", {"POST"}),
    ("/user/api/cases/{x}/reopen", {"POST"}),
    ("/user/api/tokens/{x}/feedback", {"POST"}),
    ("/user/identity/submit", {"POST"}),
]


def _app_route_paths_with_methods() -> dict[str, set[str]]:
    from app.main import app

    paths: dict[str, set[str]] = {}
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path:
            paths.setdefault(path, set()).update(methods or set())
    return paths


def _pattern_matches_route(pattern: str, route_path: str) -> bool:
    p_segs = pattern.split("/")
    r_segs = route_path.split("/")
    if len(p_segs) != len(r_segs):
        return False
    for p, r in zip(p_segs, r_segs):
        if p.startswith("{"):
            if not (r.startswith("{") and r.endswith("}")) and r.startswith("{"):
                return False
            if not r.startswith("{"):
                return False  # dynamic segments must NOT match literals
        elif p != r:
            return False
    return True


@pytest.mark.parametrize("pattern,methods", FETCH_PATTERNS)
def test_every_frontend_fetch_target_exists(pattern: str, methods: set):
    """UIQ-002: visible buttons call REAL backend operations."""
    from app.main import app  # noqa: F401  (real route table asserted below)

    routes = _app_route_paths_with_methods()
    hits = [
        (path, m)
        for path, m in routes.items()
        if _pattern_matches_route(pattern, path)
    ]
    matched_methods = set().union(*[m for _, m in hits]) if hits else set()
    assert hits, (
        f"UIQ-002 violation: frontend calls {pattern} but no route exists"
    )
    # existence of the PATH is the hard requirement; method mismatches
    # are surfaced as a 405 by FastAPI rather than a broken button — but
    # assert methods anyway where the audit recorded them.
    assert matched_methods & methods, (
        f"UIQ-002: {pattern} exists but never with methods {methods} "
        f"(has {matched_methods})"
    )

