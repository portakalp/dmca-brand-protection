"""The API never connects as kalkan_worker or kalkan_owner (§17)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import kalkan.api

pytestmark = pytest.mark.unit

API = Path(kalkan.api.__file__).parent
FORBIDDEN = {
    "worker_session",
    "worker_engine",
    "worker_sessionmaker",
    "database_url_worker",
    "database_url_migrate",
    "get_database",  # the process-wide Database of the workers
}


def test_no_api_module_reaches_a_worker_or_owner_connection() -> None:
    found = []
    for path in sorted(API.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            name = (
                node.attr
                if isinstance(node, ast.Attribute)
                else node.id
                if isinstance(node, ast.Name)
                else node.name
                if isinstance(node, ast.alias)
                else None
            )
            if name in FORBIDDEN:
                found.append(f"{path.relative_to(API)}:{getattr(node, 'lineno', 0)} {name}")
        text = path.read_text(encoding="utf-8")
        for role in ("kalkan_worker", "kalkan_owner"):
            if role in text and path.name != "__init__.py":
                found.append(f"{path.relative_to(API)} mentions {role}")
    assert found == []
