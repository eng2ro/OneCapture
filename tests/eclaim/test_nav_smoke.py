"""Navigation smoke test: EVERY page reachable from the sidebar must render.

Pins the whole menu surface so a broken route/template/query can never ship as
a user-facing error page again (the class of failure behind "I click the menu
and get an error"). The path list is parsed from base.html itself, so adding a
nav item without a working page fails here immediately.
"""

from __future__ import annotations

import re
from pathlib import Path

BASE_HTML = Path(__file__).resolve().parents[2] / "src/eclaim/web/templates/base.html"

# Cookie-authed pages that exist outside the e-Claim sidebar (the ERP Sync
# review surface has its own templates/router but shares the session auth).
EXTRA_PATHS = ["/erpsync/review"]


def _sidebar_paths() -> list[str]:
    html = BASE_HTML.read_text(encoding="utf-8")
    paths = sorted(set(re.findall(r'href="(/[a-z][a-z/_-]*)"', html)))
    return [p for p in paths if p not in ("/login", "/logout")]


def test_sidebar_parser_sees_the_nav():
    paths = _sidebar_paths()
    # Sanity: if the regex ever breaks, this fails loudly instead of the loop
    # below trivially passing over an empty list.
    for expected in ("/claims", "/capture", "/approvals", "/payables",
                     "/admin/rates", "/admin/vehicles", "/intake/holding",
                     "/ap", "/ledger", "/coverage"):
        assert expected in paths, f"{expected} missing from base.html nav"


def test_every_sidebar_route_renders(client):
    failures = []
    for p in _sidebar_paths() + EXTRA_PATHS:
        r = client.get(p, follow_redirects=True)
        if r.status_code != 200:
            failures.append(f"{p} -> {r.status_code}")
    assert not failures, "menu pages returned errors: " + "; ".join(failures)
