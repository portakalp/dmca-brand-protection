"""``kalkan eval run`` with a fake API on the fixture dataset."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx2
import pytest
from typer.testing import CliRunner

from kalkan import cli
from kalkan.ai import client as client_module
from kalkan.ai.client import AiClient, MemoryRecorder
from kalkan.ai.evaluation import load_dataset, report_markdown, run_eval, score
from kalkan.ai.prompting import PROMPT_VERSION
from kalkan.config import Settings
from tests.ai_support import FIXTURES, FakeApi, Reply, candidate_text, recorded, verdict_message
from tests.support import make_settings

DATASET = FIXTURES / "dataset" / "2026-11-10.jsonl"
ANSWERS = {
    "KLK-000001": "phishing_clone",
    "KLK-000002": "parked_or_empty",
    "KLK-000003": "affiliate_or_review",
    "KLK-000004": "unclear",
}
runner = CliRunner()


def respond(body: dict[str, Any]) -> Reply:
    number = re.search(r"Case: (KLK-\d+)", candidate_text(body))
    assert number is not None
    category = ANSWERS[number.group(1)]
    if body["model"] == "claude-haiku-4-5":
        # The other model calls the review site parked and cannot answer case 4.
        if number.group(1) == "KLK-000003":
            category = "parked_or_empty"
        if number.group(1) == "KLK-000004":
            return 200, recorded("message_invalid.json")
    collects = category == "phishing_clone"
    return 200, verdict_message(category=category, collects_credentials_or_payment=collects)


def test_a_run_scores_the_dataset() -> None:
    dataset = load_dataset(DATASET)
    assert len(dataset.cases) == 4
    assert dataset.thresholds["triage_min_confidence"] == 0.9
    api = FakeApi(respond=respond)
    run = run_eval(dataset, api.client(), model="claude-haiku-5-5", limit=None, now=_now())
    scores = score(run.results)
    assert (scores.answered, scores.correct) == (4, 3)
    assert scores.matrix[("phishing_clone", "unclear")] == 1
    # R1: case 1 has every code signal; case 4 has no official_infrastructure result.
    assert [r.auto_candidate for r in run.results] == [True, False, False, False]
    assert (scores.auto_total, scores.auto_correct, scores.phishing_labels) == (1, 1, 2)
    assert run.cost_usd > 0
    # Brand and candidate images go inline: the dataset does not depend on Files API ids.
    sources = [
        block["source"]["type"]
        for block in api.bodies()[0]["messages"][0]["content"]
        if block["type"] == "image"
    ]
    assert sources
    assert set(sources) == {"base64"}


def test_limit_takes_the_first_cases() -> None:
    api = FakeApi(respond=respond)
    run = run_eval(
        load_dataset(DATASET), api.client(), model="claude-haiku-5-5", limit=2, now=_now()
    )
    assert [r.case_number for r in run.results] == ["KLK-000001", "KLK-000002"]
    assert len(api.bodies()) == 2


def test_the_report_has_the_matrix_scores_r1_and_cost() -> None:
    api = FakeApi(respond=respond)
    run = run_eval(
        load_dataset(DATASET), api.client(), model="claude-haiku-5-5", limit=None, now=_now()
    )
    report = report_markdown(run)
    assert report.startswith(f"# AI evaluation: claude-haiku-5-5, prompt {PROMPT_VERSION}")
    assert "| phishing_clone | 1 | 0 | 0 | 0 | 0 | 1 | 0 |" in report
    assert "| phishing_clone | 100% (1/1) | 50% (1/2) |" in report
    assert "- Of them labelled phishing_clone: 100% (1/1)" in report
    assert "| KLK-000004 | phishing_clone | unclear |" in report
    assert "- Cost: $" in report


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch, clean_env: pytest.MonkeyPatch) -> FakeApi:
    api = FakeApi(respond=respond)
    original = AiClient.create

    def create(*args: Any, **kwargs: Any) -> AiClient:
        kwargs["http_client"] = httpx2.Client(transport=httpx2.MockTransport(api.handler))
        kwargs["sleep"] = lambda _: None
        return original(*args, **kwargs)

    monkeypatch.setattr(client_module.AiClient, "create", create)

    def settings() -> Settings:
        return make_settings(env="local", anthropic_api_key="sk-ant-test-not-a-key")

    monkeypatch.setattr(cli, "get_settings", settings)
    return api


def test_two_models_on_the_same_dataset_are_compared(fake_api: FakeApi, tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        [
            "eval",
            "run",
            "--dataset",
            str(DATASET),
            "--out",
            str(tmp_path),
            "--model",
            "claude-haiku-5-5",
            "--model",
            "claude-haiku-4-5",
        ],
    )
    assert result.exit_code == 0, result.output
    names = sorted(path.name for path in tmp_path.iterdir())
    day = re.compile(r"\d{4}-\d{2}-\d{2}")
    assert [day.sub("DAY", name) for name in names] == [
        f"DAY-claude-haiku-4-5-{PROMPT_VERSION}.md",
        f"DAY-claude-haiku-5-5-{PROMPT_VERSION}.md",
        f"DAY-comparison-{PROMPT_VERSION}.md",
    ]
    comparison = (tmp_path / names[2]).read_text()
    assert "| claude-haiku-5-5 | 4 | 100% (4/4) | 75% (3/4) |" in comparison
    assert "| claude-haiku-4-5 | 4 | 75% (3/4) | 50% (2/4) |" in comparison
    other = (tmp_path / names[0]).read_text()
    assert "| KLK-000004 | phishing_clone | no answer |" in other
    # Haiku 4.5 takes no effort parameter; Haiku 5.5 gets low.
    efforts = {body["model"]: body["output_config"].get("effort") for body in fake_api.bodies()}
    assert efforts == {"claude-haiku-5-5": "low", "claude-haiku-4-5": None}


def test_an_unpriced_model_is_refused_before_any_call(fake_api: FakeApi, tmp_path: Path) -> None:
    result = runner.invoke(
        cli.app,
        ["eval", "run", "--dataset", str(DATASET), "--out", str(tmp_path), "--model", "gpt-x"],
    )
    assert result.exit_code == 1
    assert "no price in AI_PRICES for gpt-x" in result.output
    assert fake_api.requests == []


def test_a_memory_recorder_counts_the_eval_calls() -> None:
    recorder = MemoryRecorder()
    api = FakeApi(respond=respond)
    run_eval(
        load_dataset(DATASET),
        api.client(recorder=recorder),
        model="claude-haiku-5-5",
        limit=1,
        now=_now(),
    )
    assert [call.purpose.value for call in recorder.calls] == ["eval"]
    assert recorder.calls[0].case_id is None


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
