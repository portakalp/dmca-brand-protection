"""The AI evaluation (architecture.md §13): labelled cases against a model and prompt version.

``export_dataset`` writes every labelled case (demo workspace left out) as one JSON line of a
dataset file, ``<date>.jsonl``: the label, the case snapshot, the whitelist summary and the
paths of the brand's official images and the candidate's screenshots, which are stored next
to it under ``images/`` (named by their SHA-256, already in a format Claude accepts). The
first line holds the decision thresholds of the moment, so a later run measures R1 with them.

``run_eval`` builds the production prompt for each case, with the images inline instead of
Files API ids (a dataset can be run with any key), asks the model and scores the answers. The
calls are ``ai_calls`` rows (purpose ``eval``) when a database is configured and count towards
the monthly budget. ``report_markdown`` writes the confusion matrix, precision and recall per
category, the accuracy on the cases R1 would send to confirmation and the cost.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select

from kalkan.ai.brand_files import newest_assets, read_image, whitelist_summary
from kalkan.ai.client import AiClient, AiError, Answer, Effort
from kalkan.ai.images import PreparedImage
from kalkan.ai.prompting import PROMPT_VERSION, BrandImage, Prompt, build_prompt
from kalkan.ai.schema import Verdict
from kalkan.db.models import Artifact, Case, Customer, Label
from kalkan.db.session import Database
from kalkan.db.settings_store import read_settings
from kalkan.decision.rules import r1_auto_candidate
from kalkan.domain.enums import AiPurpose, Category, Profile
from kalkan.logging import get_logger
from kalkan.signals.snapshot import CaseSnapshot, build_snapshot
from kalkan.storage.gcs import ObjectStore, sha256_hex

DATASET_FORMAT = 1
IMAGES_DIR = "images"
THRESHOLDS = ("triage_min_confidence", "visual_similarity_min")
EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
CATEGORIES = [category.value for category in Category]
NO_ANSWER = "no answer"
PARALLEL = 4

log = get_logger(__name__)


# --- export -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Exported:
    path: Path
    cases: int
    images: int


def _save_image(directory: Path, image: PreparedImage) -> dict[str, str]:
    name = f"{sha256_hex(image.data)}.{EXTENSIONS[image.media_type]}"
    target = directory / IMAGES_DIR / name
    if not target.exists():
        target.write_bytes(image.data)
    return {"file": f"{IMAGES_DIR}/{name}", "media_type": image.media_type}


def export_dataset(
    database: Database, store: ObjectStore, out_dir: Path, *, now: datetime
) -> Exported:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / IMAGES_DIR).mkdir(exist_ok=True)
    path = out_dir / f"{now.date().isoformat()}.jsonl"
    lines: list[dict[str, Any]] = []
    images: set[str] = set()
    with database.worker_session() as session:
        thresholds = {key: read_settings(session)[key] for key in THRESHOLDS}
        rows = session.execute(
            select(Label, Case)
            .join(Case, Case.id == Label.case_id)
            .join(Customer, Customer.id == Case.customer_id)
            .where(Customer.is_demo.is_(False))
            .order_by(Label.labeled_at, Label.id)
        ).all()
        for label, case in rows:
            snapshot = build_snapshot(session, case.id, now=now)
            if snapshot is None:
                continue
            brand = []
            for stored in newest_assets(session, case.brand_id):
                prepared = read_image(store, stored.storage_key)
                if prepared is not None:
                    saved = _save_image(out_dir, prepared)
                    images.add(saved["file"])
                    brand.append({"label": stored.label, **saved})
            shots: dict[str, dict[str, str]] = {}
            for scan in snapshot.scans:
                if scan.profile is None or scan.screenshot_artifact_id is None:
                    continue
                key = session.scalar(
                    select(Artifact.storage_key).where(Artifact.id == scan.screenshot_artifact_id)
                )
                prepared = read_image(store, key) if key is not None else None
                if prepared is not None:
                    shots[scan.profile.value] = _save_image(out_dir, prepared)
                    images.add(shots[scan.profile.value]["file"])
            lines.append(
                {
                    "type": "case",
                    "label": label.category.value,
                    "note": label.note,
                    "snapshot": snapshot.model_dump(mode="json"),
                    "whitelist": whitelist_summary(session, case.customer_id, case.brand_id),
                    "brand_images": brand,
                    "screenshots": shots,
                }
            )
    meta = {
        "type": "meta",
        "format": DATASET_FORMAT,
        "exported_at": now.isoformat(),
        "thresholds": thresholds,
        "cases": len(lines),
    }
    with path.open("w", encoding="utf-8") as handle:
        for line in [meta, *lines]:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    log.info("eval.exported", path=str(path), cases=len(lines), images=len(images))
    return Exported(path, len(lines), len(images))


# --- dataset ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalCase:
    label: Category
    snapshot: CaseSnapshot
    whitelist: list[str]
    brand_images: list[BrandImage]
    screenshots: dict[Profile, PreparedImage]

    @property
    def custom_id(self) -> str:
        return str(self.snapshot.case.id)

    def prompt(self) -> Prompt:
        return build_prompt(
            self.snapshot,
            whitelist=self.whitelist,
            brand_images=self.brand_images,
            candidate_images=dict(self.screenshots),
        )


@dataclass(frozen=True)
class Dataset:
    path: Path
    exported_at: str
    thresholds: dict[str, float]
    cases: list[EvalCase]


def _image(directory: Path, entry: dict[str, str]) -> PreparedImage:
    target = (directory / entry["file"]).resolve()
    if directory.resolve() not in target.parents:
        raise ValueError(f"image path outside the dataset: {entry['file']}")
    return PreparedImage(entry["media_type"], target.read_bytes())


def load_dataset(path: Path) -> Dataset:
    directory = path.parent
    meta: dict[str, Any] = {}
    cases: list[EvalCase] = []
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            if not raw.strip():
                continue
            line = json.loads(raw)
            if line.get("type") == "meta":
                if line.get("format") != DATASET_FORMAT:
                    raise ValueError(f"unsupported dataset format {line.get('format')}")
                meta = line
                continue
            cases.append(
                EvalCase(
                    label=Category(line["label"]),
                    snapshot=CaseSnapshot.model_validate(line["snapshot"]),
                    whitelist=list(line.get("whitelist") or []),
                    brand_images=[
                        BrandImage(item["label"], image=_image(directory, item))
                        for item in line.get("brand_images") or []
                    ],
                    screenshots={
                        Profile(profile): _image(directory, item)
                        for profile, item in (line.get("screenshots") or {}).items()
                    },
                )
            )
    if not meta:
        raise ValueError("the dataset has no meta line")
    return Dataset(path, str(meta.get("exported_at")), dict(meta.get("thresholds") or {}), cases)


# --- run --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseResult:
    case_number: str
    label: Category
    predicted: Category | None
    confidence: float | None
    explanation: str | None
    error: str | None
    auto_candidate: bool
    cost_usd: Decimal
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass
class EvalRun:
    model: str
    prompt_version: str
    dataset: Dataset
    batch: bool
    effort: Effort | None
    started_at: datetime
    results: list[CaseResult] = field(default_factory=list)

    @property
    def cost_usd(self) -> Decimal:
        return sum((result.cost_usd for result in self.results), Decimal(0))


def _thresholds(dataset: Dataset) -> dict[str, float]:
    from kalkan.db.settings_store import SPECS

    return {key: float(dataset.thresholds.get(key, SPECS[key].default)) for key in THRESHOLDS}


def _result(
    case: EvalCase, answer: Answer[Verdict] | AiError, thresholds: dict[str, float]
) -> CaseResult:
    number = case.snapshot.case.number
    if isinstance(answer, AiError):
        return CaseResult(number, case.label, None, None, None, str(answer), False, Decimal(0))
    verdict = answer.output
    predicted = Category(verdict.category)
    return CaseResult(
        case_number=number,
        label=case.label,
        predicted=predicted,
        confidence=verdict.confidence,
        explanation=verdict.short_explanation,
        error=None,
        auto_candidate=r1_auto_candidate(
            case.snapshot, predicted, verdict.confidence, **thresholds
        ),
        cost_usd=answer.cost_usd,
        input_tokens=answer.usage.input_tokens,
        output_tokens=answer.usage.output_tokens,
        cache_read_tokens=answer.usage.cache_read_tokens,
        cache_write_tokens=answer.usage.cache_write_tokens,
    )


def run_eval(
    dataset: Dataset,
    client: AiClient,
    *,
    model: str,
    limit: int | None,
    batch: bool = False,
    effort: Effort | None = "low",
    now: datetime,
    poll_seconds: float = 30.0,
) -> EvalRun:
    cases = dataset.cases[:limit] if limit is not None else dataset.cases
    run = EvalRun(model, PROMPT_VERSION, dataset, batch, effort, now)
    thresholds = _thresholds(dataset)
    answers: dict[str, Answer[Verdict] | AiError]
    if batch:
        answers = client.call_batch(
            [(case.custom_id, case.prompt()) for case in cases],
            output=Verdict,
            model=model,
            purpose=AiPurpose.EVAL,
            effort=effort,
            poll_seconds=poll_seconds,
        )
    else:

        def one(case: EvalCase) -> Answer[Verdict] | AiError:
            try:
                return client.call(
                    case.prompt(),
                    output=Verdict,
                    model=model,
                    purpose=AiPurpose.EVAL,
                    effort=effort,
                )
            except AiError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
            answers = dict(zip((c.custom_id for c in cases), pool.map(one, cases), strict=True))
    for case in cases:
        found = answers.get(case.custom_id)
        if found is None:
            found = AiError("no result")
        run.results.append(_result(case, found, thresholds))
    log.info(
        "eval.run",
        model=model,
        cases=len(cases),
        errors=sum(1 for r in run.results if r.error),
        cost_usd=str(run.cost_usd),
    )
    return run


# --- report -----------------------------------------------------------------------------------


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _ratio(part: int, whole: int) -> str:
    return f"{part / whole:.0%} ({part}/{whole})" if whole else "-"


@dataclass(frozen=True)
class Scores:
    answered: int
    correct: int
    matrix: dict[tuple[str, str], int]
    precision: dict[str, tuple[int, int]]  # category -> (correct, predicted)
    recall: dict[str, tuple[int, int]]  # category -> (correct, labelled)
    auto_total: int
    auto_correct: int
    phishing_labels: int


def score(results: Sequence[CaseResult]) -> Scores:
    matrix: Counter[tuple[str, str]] = Counter()
    for result in results:
        predicted = result.predicted.value if result.predicted else NO_ANSWER
        matrix[(result.label.value, predicted)] += 1
    precision = {}
    recall = {}
    for name in CATEGORIES:
        hit = matrix[(name, name)]
        precision[name] = (hit, sum(matrix[(label, name)] for label in CATEGORIES))
        recall[name] = (hit, sum(count for (label, _), count in matrix.items() if label == name))
    auto = [result for result in results if result.auto_candidate]
    return Scores(
        answered=sum(1 for result in results if result.predicted is not None),
        correct=sum(1 for result in results if result.predicted is result.label),
        matrix=dict(matrix),
        precision=precision,
        recall=recall,
        auto_total=len(auto),
        auto_correct=sum(1 for result in auto if result.label is Category.PHISHING_CLONE),
        phishing_labels=sum(1 for r in results if r.label is Category.PHISHING_CLONE),
    )


def report_name(run: EvalRun) -> str:
    return f"{run.started_at.date().isoformat()}-{run.model}-{run.prompt_version}.md"


def report_markdown(run: EvalRun) -> str:
    results = run.results
    scores = score(results)
    thresholds = _thresholds(run.dataset)
    total = len(results)
    tokens = {
        "input": sum(r.input_tokens for r in results),
        "cache read": sum(r.cache_read_tokens for r in results),
        "cache write": sum(r.cache_write_tokens for r in results),
        "output": sum(r.output_tokens for r in results),
    }
    columns = [*CATEGORIES, NO_ANSWER]
    lines = [
        f"# AI evaluation: {run.model}, prompt {run.prompt_version}",
        "",
        f"- Dataset: `{run.dataset.path.name}` (exported {run.dataset.exported_at}), {total} cases",
        f"- Run: {run.started_at.isoformat(timespec='seconds')}, "
        f"{'Batch API' if run.batch else 'Messages API'}, effort {run.effort or 'default'}",
        f"- Answered: {_ratio(scores.answered, total)}; accuracy: {_ratio(scores.correct, total)}",
        f"- Cost: ${run.cost_usd:.4f} in all, "
        f"${(run.cost_usd / total if total else Decimal(0)):.5f} per case",
        "- Tokens: " + ", ".join(f"{name} {count}" for name, count in tokens.items()),
        "",
        "## Confusion matrix",
        "",
        "Rows: label; columns: model answer.",
        "",
        "| label | " + " | ".join(columns) + " |",
        "| --- | " + " | ".join("---:" for _ in columns) + " |",
    ]
    for label in CATEGORIES:
        row = [str(scores.matrix.get((label, column), 0)) for column in columns]
        lines.append(f"| {label} | " + " | ".join(row) + " |")
    lines += [
        "",
        "## Per category",
        "",
        "| category | precision | recall |",
        "| --- | ---: | ---: |",
    ]
    for name in CATEGORIES:
        lines.append(
            f"| {name} | {_ratio(*scores.precision[name])} | {_ratio(*scores.recall[name])} |"
        )
    lines += [
        "",
        "## Automatic confirmation (R1)",
        "",
        "Thresholds: "
        + ", ".join(f"{key} {value:g}" for key, value in thresholds.items())
        + ". R1 sends a case to the confirmation model; only labelled phishing clones are hits.",
        "",
        f"- Cases R1 would send: {scores.auto_total}",
        f"- Of them labelled phishing_clone: {_ratio(scores.auto_correct, scores.auto_total)}",
        f"- Labelled phishing clones R1 would send: "
        f"{_ratio(scores.auto_correct, scores.phishing_labels)}",
        "",
        "## Wrong or missing answers",
        "",
    ]
    wrong = [r for r in results if r.predicted is not r.label]
    if not wrong:
        lines.append("None.")
    else:
        lines += [
            "| case | label | answer | confidence | explanation or error |",
            "| --- | --- | --- | ---: | --- |",
        ]
        for result in wrong:
            answer = result.predicted.value if result.predicted else NO_ANSWER
            confidence = f"{result.confidence:.2f}" if result.confidence is not None else "-"
            detail = result.explanation if result.error is None else result.error
            lines.append(
                f"| {result.case_number} | {result.label.value} | {answer} | {confidence} | "
                f"{_cell(detail or '')[:300]} |"
            )
    return "\n".join(lines) + "\n"


def comparison_markdown(runs: Iterable[EvalRun]) -> str:
    runs = list(runs)
    lines = [
        "# AI evaluation: model comparison",
        "",
        f"Prompt {runs[0].prompt_version}, dataset `{runs[0].dataset.path.name}`.",
        "",
        "| model | cases | answered | accuracy | phishing_clone precision | phishing_clone recall "
        "| R1 sends | R1 hits | cost (USD) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in runs:
        scores = score(run.results)
        total = len(run.results)
        lines.append(
            f"| {run.model} | {total} | {_ratio(scores.answered, total)} | "
            f"{_ratio(scores.correct, total)} | "
            f"{_ratio(*scores.precision[Category.PHISHING_CLONE.value])} | "
            f"{_ratio(*scores.recall[Category.PHISHING_CLONE.value])} | {scores.auto_total} | "
            f"{_ratio(scores.auto_correct, scores.auto_total)} | {run.cost_usd:.4f} |"
        )
    return "\n".join(lines) + "\n"
