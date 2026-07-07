"""E2E: the dashboard is fully self-contained — rendering it makes zero
network requests beyond the HTML file itself."""
from __future__ import annotations

import pytest
from harness import wait_ready

pytestmark = pytest.mark.e2e


def test_no_external_requests(_browser, dashboard_path):
    page = _browser.new_page(viewport={"width": 1600, "height": 2400})
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    try:
        page.goto(dashboard_path.resolve().as_uri())
        wait_ready(page)
        external = [u for u in requests if not u.startswith(("file://", "data:"))]
        assert not external, f"dashboard fetched external resources: {external}"
    finally:
        page.close()


def test_no_external_references_in_html(dashboard_path):
    # The SVG namespace constant inside charts.js is a string, not a fetch;
    # what must not appear is any element that loads an external resource.
    html = dashboard_path.read_text(encoding="utf-8")
    for needle in ('<script src', "<link ", 'src="http', "src='http",
                   'href="http', "url(http", "@import"):
        assert needle not in html, f"external reference in HTML: {needle}"
