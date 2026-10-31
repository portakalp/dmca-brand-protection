"""``make openapi``: writes the API schema to ``backend/openapi.json`` (§17).

The web types are generated from that file (``make web-types``); CI checks that it is up to date.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from kalkan.api.app import create_app
from kalkan.config import Settings

OUTPUT = Path(__file__).parents[3] / "openapi.json"


def schema() -> dict[str, Any]:
    app = create_app(Settings(_env_file=None, env="local"))
    document: dict[str, Any] = app.openapi()
    return document


def main(argv: list[str]) -> int:
    target = Path(argv[1]) if len(argv) > 1 else OUTPUT
    target.write_text(json.dumps(schema(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {target}")  # noqa: T201
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
