"""Public Suffix List copy (architecture.md §8)."""

from __future__ import annotations

from pathlib import Path

import httpx2
import pytest

from kalkan.discovery import psl


def test_split_host_uses_the_repository_copy() -> None:
    assert psl.split_host("login.mintera.com.pl") == ("login", "mintera", "com.pl")
    assert psl.split_host("mintera.pages.dev") == ("", "mintera", "pages.dev")
    assert psl.count_rules(psl.PSL_FILE.read_text(encoding="utf-8")) > psl.MIN_RULES


def _serve(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    def get(url: str, **_: object) -> httpx2.Response:
        return httpx2.Response(200, text=text, request=httpx2.Request("GET", url))

    monkeypatch.setattr(psl.httpx2, "get", get)


def test_update_writes_a_valid_list(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve(monkeypatch, psl.PSL_FILE.read_text(encoding="utf-8"))
    target = tmp_path / "psl.dat"
    assert psl.update_public_suffix_list(target) > psl.MIN_RULES
    assert target.read_text(encoding="utf-8") == psl.PSL_FILE.read_text(encoding="utf-8")


def test_update_rejects_something_else(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _serve(monkeypatch, "<html>maintenance</html>")
    target = tmp_path / "psl.dat"
    with pytest.raises(ValueError, match="does not look like the PSL"):
        psl.update_public_suffix_list(target)
    assert not target.exists()
