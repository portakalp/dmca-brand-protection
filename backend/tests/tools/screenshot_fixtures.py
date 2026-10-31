"""Write the viewport screenshots of ``fixtures/screenshots/`` (the visual signal tests).

Each fixture page is loaded from disk into Chromium at the browser stage's viewport sizes and
saved as ``<name>-<profile>.png``. The pages are self-contained, so no server or proxy is
needed. Run it again when a fixture page or the Playwright version changes, and commit the
result:

    uv run --project backend python -m tests.tools.screenshot_fixtures

(from ``backend/``: ``uv run python -m tests.tools.screenshot_fixtures``).
"""

from __future__ import annotations

from kalkan.config import REPO_ROOT

SITES = REPO_ROOT / "fixtures" / "sites"
OUT = REPO_ROOT / "fixtures" / "screenshots"
PAGES = {
    "official": "official/index.html",
    "clone": "clone/index.html",
    "unrelated": "mobile/index.html",  # the recipe blog
    "parked": "parked/index.html",
}
# The browser stage's viewports (tests/integration/test_browser.py SIZES).
VIEWPORTS = {"desktop": (1280, 800), "mobile": (390, 844)}


def main() -> None:
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for profile, (width, height) in VIEWPORTS.items():
            context = browser.new_context(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
                is_mobile=profile == "mobile",
            )
            page = context.new_page()
            for name, path in PAGES.items():
                page.set_content((SITES / path).read_text(encoding="utf-8"), wait_until="load")
                page.screenshot(path=str(OUT / f"{name}-{profile}.png"))
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
