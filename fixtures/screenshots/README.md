# Fixture screenshots

Viewport screenshots of the fixture sites (`../sites/`) for the visual signal tests
(architecture.md §12). Desktop is 1280 × 800, mobile 390 × 844, as in the browser
stage.

| File | Page |
| --- | --- |
| `official-*.png` | `mintera.test`, the official site (the reference) |
| `clone-*.png` | `mintera-login.test`, the clone |
| `unrelated-*.png` | the recipe blog that `mintera-mobile.test` shows on desktop |
| `parked-*.png` | `mintera-parked.test` |

Regenerate them after a fixture page or Playwright changes, from `backend/`:

```sh
uv run python -m tests.tools.screenshot_fixtures
```
