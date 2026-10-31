"""Management CLI (``kalkan``)."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, NoReturn

import httpx2
import typer
from google.api_core.exceptions import GoogleAPIError
from pydantic import ValidationError
from pydantic_settings import SettingsError
from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from kalkan import __version__, admin
from kalkan.admin import AdminError
from kalkan.config import REPO_ROOT, REQUIRED_BY_SERVICE, ConfigError, Settings, get_settings
from kalkan.db.base import utcnow
from kalkan.db.models import Case, CaseEvent, Customer, Domain, Notice, Report
from kalkan.db.session import get_database
from kalkan.discovery.psl import PSL_FILE, update_public_suffix_list
from kalkan.domain.case_state import panel_status
from kalkan.domain.enums import ContactKind, CustomerStatus, Language, OfficialDomainKind
from kalkan.net.safe_http import FetchError, SafeHttpClient
from kalkan.reports import build as reports
from kalkan.scanning.brand_assets import UnknownBrandError, refresh_brand_favicons
from kalkan.scanning.references import NoReferenceError, issue_reference_capture
from kalkan.security import new_password, totp_uri
from kalkan.seed.demo import DEFAULT_CREDENTIALS_FILE, DEMO_SLUG, SeedError, seed_demo
from kalkan.seed.local_demo import (
    DemoCredentialsError,
    accounts,
    add_staff,
    current_code,
    load_credentials,
    reset_demo_operator,
    totp_secret_for,
)
from kalkan.storage.gcs import ObjectStore
from kalkan.workers.broker import setup_broker

app = typer.Typer(help="Kalkan management commands.", no_args_is_help=True)


@app.command()
def version() -> None:
    """Print the Kalkan version and the git SHA of this build."""
    try:
        git_sha = get_settings().git_sha
    except (ValidationError, SettingsError):
        git_sha = None
    typer.echo(f"kalkan {__version__}" + (f" ({git_sha})" if git_sha else ""))


@app.command("check-config")
def check_config(
    service: Annotated[
        str | None,
        typer.Option(help=f"Check one service: {', '.join(REQUIRED_BY_SERVICE)}."),
    ] = None,
) -> None:
    """Validate the environment and list missing settings per service. Never prints values."""
    if service is not None and service not in REQUIRED_BY_SERVICE:
        typer.echo(f"Unknown service: {service}", err=True)
        raise typer.Exit(code=2)

    try:
        settings = get_settings()
    except ValidationError as exc:
        typer.echo("Invalid configuration:", err=True)
        for error in exc.errors():
            # Input values are not printed: they may be secrets.
            name = ".".join(str(part) for part in error["loc"]).upper() or "settings"
            typer.echo(f"  {name}: {error['msg']}", err=True)
        raise typer.Exit(code=2) from None
    except SettingsError as exc:
        # Raised for values that are not valid JSON; the message names the variable only.
        typer.echo(f"Invalid configuration: {exc}", err=True)
        raise typer.Exit(code=2) from None

    typer.echo(f"ENV={settings.env}")
    services = [service] if service else list(REQUIRED_BY_SERVICE)
    failed = False
    for name in services:
        missing = settings.missing_for(name)
        if missing:
            failed = True
            typer.echo(f"{name}: missing {', '.join(missing)}")
        else:
            typer.echo(f"{name}: ok")
    if failed:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------------------------
# Operator commands (kalkan_worker). Each change is written to audit_log with actor "cli".
# --------------------------------------------------------------------------------------------

ACTOR = "cli"

customer_app = typer.Typer(help="Customers.", no_args_is_help=True)
brand_app = typer.Typer(help="Brands.", no_args_is_help=True)
official_domain_app = typer.Typer(help="Official domains of a brand.", no_args_is_help=True)
whitelist_app = typer.Typer(help="Whitelist.", no_args_is_help=True)
staff_app = typer.Typer(help="Operator (staff) accounts.", no_args_is_help=True)
case_app = typer.Typer(help="Cases.", no_args_is_help=True)
candidate_app = typer.Typer(help="Discovery candidates.", no_args_is_help=True)
psl_app = typer.Typer(help="Public Suffix List copy in the repository.", no_args_is_help=True)
contact_app = typer.Typer(help="Abuse contact overrides (host, registrar).", no_args_is_help=True)
report_app = typer.Typer(help="Monthly reports (§21).", no_args_is_help=True)
app.add_typer(customer_app, name="customer")
app.add_typer(brand_app, name="brand")
app.add_typer(official_domain_app, name="official-domain")
app.add_typer(whitelist_app, name="whitelist")
app.add_typer(staff_app, name="staff")
app.add_typer(case_app, name="case")
app.add_typer(candidate_app, name="candidate")
app.add_typer(psl_app, name="psl")
app.add_typer(contact_app, name="contact")
app.add_typer(report_app, name="report")
eval_app = typer.Typer(help="AI evaluation on labelled cases (spends money).", no_args_is_help=True)
app.add_typer(eval_app, name="eval")
EVAL_DIR = REPO_ROOT / "eval"
demo_app = typer.Typer(
    help="The local demo (`make demo`): seed, logins and TOTP codes. Refused with ENV=prod.",
    no_args_is_help=True,
)
app.add_typer(demo_app, name="demo")

# The seed and the demo commands share the file. Inside a container the default (next to the
# source checkout) does not exist, so compose points DEMO_CREDENTIALS_FILE at a bind mount.
CredentialsFile = Annotated[
    Path,
    typer.Option(
        "--credentials-file",
        envvar="DEMO_CREDENTIALS_FILE",
        help="The demo users' credentials file.",
    ),
]


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


@contextmanager
def _operator_session() -> Iterator[Session]:
    try:
        with get_database().worker_session() as session:
            yield session
    except AdminError as exc:
        _fail(str(exc))
    except ConfigError as exc:
        _fail(str(exc))


@customer_app.command("create")
def customer_create(
    name: Annotated[str, typer.Argument(help="Display name, e.g. Mintera Bank.")],
    slug: Annotated[str | None, typer.Option(help="URL-safe id; derived from the name.")] = None,
    monitor_only: Annotated[
        bool, typer.Option("--monitor-only", help="Watch only; never send any notice.")
    ] = False,
    demo: Annotated[bool, typer.Option("--demo", help="Read-only demo workspace.")] = False,
    alert_email: Annotated[str | None, typer.Option(help="Address for threat alerts.")] = None,
    timezone: Annotated[str, typer.Option(help="IANA time zone, e.g. Europe/Warsaw.")] = "UTC",
) -> None:
    """Create a customer: status trial, sending disabled, dry-run on."""
    with _operator_session() as session:
        customer = admin.create_customer(
            session,
            name=name,
            slug=slug,
            actor=ACTOR,
            monitor_only=monitor_only,
            demo=demo,
            alert_email=alert_email,
            timezone=timezone,
        )
        typer.echo(f"Created customer {customer.slug} ({customer.id})")


@customer_app.command("set-status")
def customer_set_status(
    slug: Annotated[str, typer.Argument(help="Customer slug.")],
    status: Annotated[str, typer.Argument(help="trial, active or suspended.")],
) -> None:
    """Change a customer's status; a suspended customer sends no notices."""
    try:
        value = CustomerStatus(status)
    except ValueError:
        _fail(f"unknown status {status!r}")
    with _operator_session() as session:
        customer = admin.get_customer(session, slug)
        changed = admin.set_customer_status(session, customer, value, actor=ACTOR)
    typer.echo(f"{slug}: {value.value}" + ("" if changed else " (unchanged)"))


@report_app.command("build")
def report_build(
    slug: Annotated[str, typer.Argument(help="Customer slug.")],
    period: Annotated[
        str | None, typer.Option(help="Month, YYYY-MM; the customer's last month by default.")
    ] = None,
) -> None:
    """Queue a monthly report: worker-default opens it, worker-browser prints the PDF."""
    with _operator_session() as session:
        customer = admin.get_customer(session, slug)
        try:
            month = reports.queue_build(session, customer, period, now=utcnow(), actor="cli")
        except reports.ReportError as exc:
            _fail(str(exc))
    typer.echo(f"The {month} report of {slug} is queued; it appears on the panel when printed.")


@report_app.command("list")
def report_list(slug: Annotated[str, typer.Argument(help="Customer slug.")]) -> None:
    """The customer's reports, newest first."""
    with _operator_session() as session:
        customer = admin.get_customer(session, slug)
        rows = session.scalars(
            select(Report).where(Report.customer_id == customer.id).order_by(Report.period.desc())
        ).all()
        for row in rows:
            state = f"ready {row.generated_at:%Y-%m-%d %H:%M}" if row.generated_at else "printing"
            typer.echo(f"{row.period}  {state}  {row.id}")


@brand_app.command("add")
def brand_add(
    name: Annotated[str, typer.Argument(help="Brand name.")],
    customer: Annotated[str, typer.Option(help="Customer slug.")],
    keyword: Annotated[
        list[str] | None, typer.Option(help="Discovery keyword (repeatable, at least 4 letters).")
    ] = None,
    variant: Annotated[
        list[str] | None, typer.Option(help="Spelling variant (repeatable).")
    ] = None,
    market: Annotated[
        list[str] | None, typer.Option(help="Target market, ISO country code (repeatable).")
    ] = None,
    language: Annotated[list[str] | None, typer.Option(help="Language code (repeatable).")] = None,
) -> None:
    """Add a brand to a customer."""
    with _operator_session() as session:
        brand = admin.add_brand(
            session,
            admin.get_customer(session, customer),
            name=name,
            actor=ACTOR,
            keywords=keyword or (),
            variants=variant or (),
            target_markets=market or (),
            languages=language or (),
        )
        typer.echo(f"Added brand {brand.name} ({brand.id})")


@brand_app.command("favicons")
def brand_favicons(brand: Annotated[uuid.UUID, typer.Option(help="Brand id.")]) -> None:
    """Fetch the official domains' favicons now and store the new ones."""
    settings = get_settings()
    if settings.gcs_evidence_bucket is None:
        _fail("GCS_EVIDENCE_BUCKET is required to store brand favicons")
    with SafeHttpClient(allow_cidrs=settings.ssrf_allow_cidrs) as client:
        try:
            added = refresh_brand_favicons(
                get_database(),
                client,
                ObjectStore.from_settings(settings),
                settings.gcs_evidence_bucket,
                brand,
                now=utcnow,
            )
        except (ConfigError, UnknownBrandError) as exc:
            _fail(str(exc))
        except (FetchError, OSError, GoogleAPIError) as exc:
            _fail(f"could not store the favicons: {type(exc).__name__}: {exc}")
    typer.echo(f"Stored {added} new favicon(s) for brand {brand}")


@brand_app.command("references")
def brand_references(brand: Annotated[uuid.UUID, typer.Option(help="Brand id.")]) -> None:
    """Queue reference screenshots of the brand's primary official site (browser stage)."""
    settings = get_settings()
    if settings.redis_url is None:
        _fail("REDIS_URL is required to queue the capture")
    setup_broker(settings.redis_url, prometheus=False)
    redis = Redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=5)
    try:
        job, domain = issue_reference_capture(get_database(), redis, brand, now=utcnow())
    except (ConfigError, NoReferenceError) as exc:
        _fail(str(exc))
    typer.echo(f"Queued reference capture {job.job_id} of {domain} for brand {brand}")


@official_domain_app.command("add")
def official_domain_add(
    domain: Annotated[str, typer.Argument(help="Host name, e.g. mintera.com.")],
    brand: Annotated[uuid.UUID, typer.Option(help="Brand id.")],
    kind: Annotated[OfficialDomainKind, typer.Option(help="Kind of domain.")] = (
        OfficialDomainKind.PRIMARY
    ),
) -> None:
    """Add an official domain; it is also listed in the whitelist."""
    with _operator_session() as session:
        official = admin.add_official_domain(
            session, admin.get_brand(session, brand), domain, actor=ACTOR, kind=kind
        )
        typer.echo(f"Added official domain {official.domain} ({official.kind.value})")


@whitelist_app.command("import")
def whitelist_import(
    csv_file: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="CSV with pattern,kind,note.")
    ],
    customer: Annotated[str, typer.Option(help="Customer slug.")],
    brand: Annotated[
        uuid.UUID | None, typer.Option(help="Brand id; without it the rows cover every brand.")
    ] = None,
) -> None:
    """Import whitelist rows. Nothing is imported when a row is invalid; errors are listed."""
    with _operator_session() as session:
        target = admin.get_customer(session, customer)
        brand_row = admin.get_brand(session, brand) if brand else None
        if brand_row is not None and brand_row.customer_id != target.id:
            raise admin.AdminError("the brand belongs to another customer")
        report = admin.import_whitelist(
            session,
            target,
            csv_file.read_text(encoding="utf-8-sig"),
            actor=ACTOR,
            brand=brand_row,
        )
        if report.errors:
            for error in report.errors:
                typer.echo(f"line {error.line}: {error.message}", err=True)
            typer.echo(f"{len(report.errors)} invalid rows; nothing imported.", err=True)
            raise typer.Exit(code=1)
        typer.echo(f"Imported {report.imported}; already listed {report.already_listed}.")


@staff_app.command("create")
def staff_create(
    email: Annotated[str, typer.Argument(help="Operator e-mail address.")],
    password_stdin: Annotated[
        bool,
        typer.Option(
            "--password-stdin",
            help="Read the password from the first line of standard input (no prompt).",
        ),
    ] = False,
    generate_password: Annotated[
        bool,
        typer.Option("--generate-password", help="Generate a password and show it once."),
    ] = False,
) -> None:
    """Create an operator account. Shows the TOTP secret once; store it in an authenticator.

    Without an option the password is asked twice. The options are for scripts and for
    `docker compose exec`, where nobody may be there to answer a prompt; the password never
    goes on the command line, where `ps` and the shell history would keep it.
    """
    if password_stdin and generate_password:
        _fail("--password-stdin and --generate-password cannot be used together")
    if generate_password:
        password = new_password()
    elif password_stdin:
        password = typer.get_text_stream("stdin").readline().rstrip("\r\n")
    else:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    with _operator_session() as session:
        created = admin.create_staff_user(
            session, get_settings(), email=email, password=password, actor=ACTOR
        )
        typer.echo(f"Created staff user {created.user.email}")
        if generate_password:
            typer.echo(f"Password (shown once): {password}")
        typer.echo(f"TOTP secret: {created.totp_secret}")
        typer.echo(f"otpauth link: {totp_uri(created.totp_secret, created.user.email)}")


@case_app.command("show")
def case_show(number: Annotated[str, typer.Argument(help="Case number, e.g. KLK-000123.")]) -> None:
    """Print a case with its latest events and notices."""
    with _operator_session() as session:
        case = session.scalar(select(Case).where(Case.number == number.upper()))
        if case is None:
            raise admin.AdminError(f"no case {number}")
        domain = session.get_one(Domain, case.domain_id)
        customer = session.get_one(Customer, case.customer_id)
        typer.echo(f"{case.number}  {domain.name}")
        typer.echo(f"  customer   {customer.slug}")
        typer.echo(f"  state      {case.state.value} ({panel_status(case.state).value})")
        typer.echo(f"  category   {case.category.value if case.category else '-'}")
        typer.echo(f"  first seen {case.first_seen_at.isoformat()}")
        typer.echo(f"  next check {case.next_check_at.isoformat() if case.next_check_at else '-'}")
        typer.echo(f"  last action {case.last_action or '-'}")
        events = session.scalars(
            select(CaseEvent)
            .where(CaseEvent.case_id == case.id)
            .order_by(CaseEvent.at.desc())
            .limit(10)
        )
        typer.echo("  events:")
        for event in events:
            typer.echo(f"    {event.at.isoformat()}  {event.type.value}  {event.actor}")
        notices = session.scalars(
            select(Notice).where(Notice.case_id == case.id).order_by(Notice.created_at)
        )
        typer.echo("  notices:")
        for notice in notices:
            sent = notice.sent_at.isoformat() if notice.sent_at else "-"
            dry = " (dry-run)" if notice.dry_run else ""
            typer.echo(f"    {notice.channel.value}  {notice.status.value}  {sent}{dry}")


@candidate_app.command("add")
def candidate_add(
    url: Annotated[str, typer.Argument(help="URL or host of the suspected site.")],
    brand: Annotated[uuid.UUID, typer.Option(help="Brand id.")],
) -> None:
    """Add a candidate by hand (source manual) and queue it for admission."""
    settings = get_settings()
    if settings.redis_url is None:
        _fail("REDIS_URL is required to queue the candidate")
    with _operator_session() as session:
        result = admin.add_candidate(session, admin.get_brand(session, brand), url, actor=ACTOR)
    setup_broker(settings.redis_url, prometheus=False)
    from kalkan.workers.discovery import queue_admit

    queue_admit([result.match_id])
    state = "Added" if result.created else "Already known"
    reasons = ", ".join(result.reasons)
    typer.echo(f"{state}: {result.host} (match {result.match_id}; {reasons}); queued for admission")


@contact_app.command("set")
def contact_set(
    kind: Annotated[
        ContactKind,
        typer.Argument(help="host (key: ASN), registrar (key: IANA id) or registry (key: TLD)."),
    ],
    key: Annotated[
        str, typer.Argument(help="ASN (24940 or AS24940), IANA registrar id or TLD (online).")
    ],
    email: Annotated[str | None, typer.Option(help="Abuse e-mail address.")] = None,
    form_url: Annotated[str | None, typer.Option(help="Abuse web form (https).")] = None,
    language: Annotated[Language | None, typer.Option(help="Language of the notices.")] = None,
    notes: Annotated[str | None, typer.Option(help="Free text for the operator.")] = None,
) -> None:
    """Override the abuse contact found in RDAP (it wins from the next enrichment), or set a
    registry's abuse contact, which only comes from here."""
    with _operator_session() as session:
        contact = admin.set_channel_contact(
            session,
            kind=kind,
            key=key,
            actor=ACTOR,
            email=email,
            form_url=form_url,
            language=language,
            notes=notes,
        )
        typer.echo(f"Saved {contact.kind.value} contact {contact.key}")


@psl_app.command("update")
def psl_update() -> None:
    """Download the Public Suffix List into the repository copy; commit the result."""
    try:
        rules = update_public_suffix_list()
    except (OSError, ValueError, httpx2.HTTPError) as exc:
        _fail(f"could not update the Public Suffix List: {exc}")
    typer.echo(f"Wrote {rules} rules to {PSL_FILE}")


@app.command("seed")
def seed(
    credentials_file: CredentialsFile = DEFAULT_CREDENTIALS_FILE,
    print_credentials: Annotated[
        bool,
        typer.Option(
            "--print-credentials",
            help="Write no file; show the new credentials once (refreshing the demo in prod).",
        ),
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Do not ask before replacing the demo.")
    ] = False,
) -> None:
    """Rebuild the Mintera Bank demo workspace. New credentials on every run.

    Only the is_demo customer is replaced. With ENV=prod only --print-credentials is allowed.
    """
    settings = get_settings()
    if settings.env == "prod" and not yes:
        typer.confirm(f"Replace the {DEMO_SLUG} demo workspace in prod?", abort=True)
    try:
        result = seed_demo(
            get_database(),
            ObjectStore.from_settings(settings),
            settings,
            credentials_file=None if print_credentials else credentials_file,
        )
    except (SeedError, ConfigError) as exc:
        _fail(str(exc))
    typer.echo(f"Seeded {DEMO_SLUG}: {result.cases} cases.")
    if result.credentials_file is not None:
        typer.echo(f"Demo credentials: {result.credentials_file}")
        return
    typer.echo("Demo credentials (shown once, stored nowhere; keep them in your password manager):")
    for user in result.credentials["users"]:
        typer.echo(f"  {user['role']}: {user['email']}")
        typer.echo(f"    password:    {user['password']}")
        typer.echo(f"    TOTP secret: {user['totp_secret']}")
        typer.echo(f"    recovery:    {' '.join(user['recovery_codes'])}")


def _local_only() -> Settings:
    """The demo commands write or print secrets: never against a production deployment."""
    try:
        settings = get_settings()
    except (ValidationError, SettingsError):
        _fail("invalid configuration; run `kalkan check-config`")
    if settings.env == "prod":
        _fail("the demo commands are for local use only and refuse ENV=prod")
    return settings


def _print_accounts(data: dict[str, Any]) -> None:
    for entry in accounts(data):
        where = "ops panel" if entry.get("role") == "operator" else "panel"
        typer.echo(f"  {where} ({entry.get('role', '?')}): {entry['email']}")
        typer.echo(f"    password: {entry.get('password', '?')}")
        if entry.get("totp_secret"):
            typer.echo(f"    otpauth:  {totp_uri(entry['totp_secret'], entry['email'])}")


@demo_app.command("setup")
def demo_setup(credentials_file: CredentialsFile = DEFAULT_CREDENTIALS_FILE) -> None:
    """Rebuild the Mintera Bank demo, renew the ops operator and show every demo login.

    New passwords and TOTP secrets on every run; all of them go to the credentials file.
    """
    settings = _local_only()
    try:
        result = seed_demo(
            get_database(),
            ObjectStore.from_settings(settings),
            settings,
            credentials_file=credentials_file,
        )
    except (SeedError, ConfigError) as exc:
        _fail(str(exc))
    try:
        with _operator_session() as session:
            operator = reset_demo_operator(session, settings, actor=ACTOR)
            # Inside the transaction: if the file cannot be written, the old login stays.
            add_staff(credentials_file, operator)
    except (DemoCredentialsError, OSError) as exc:
        _fail(f"cannot record the operator: {exc}")
    typer.echo(f"Seeded {DEMO_SLUG}: {result.cases} cases. Credentials: {credentials_file}")
    _print_accounts(load_credentials(credentials_file))


@demo_app.command("show")
def demo_show(credentials_file: CredentialsFile = DEFAULT_CREDENTIALS_FILE) -> None:
    """Show the demo logins from the credentials file (no database needed)."""
    _local_only()
    try:
        _print_accounts(load_credentials(credentials_file))
    except DemoCredentialsError as exc:
        _fail(str(exc))


# A code with less time left than this is not shown; `demo code` waits for the next one.
FRESH_CODE_SECONDS = 5


@demo_app.command("code")
def demo_code(
    email: Annotated[str, typer.Argument(help="A demo account, e.g. security@mintera.example.")],
    credentials_file: CredentialsFile = DEFAULT_CREDENTIALS_FILE,
    one_line: Annotated[
        bool, typer.Option("--one-line", help="Print the code and its validity on one line.")
    ] = False,
) -> None:
    """Print the account's current TOTP code, as an authenticator app would show it."""
    _local_only()
    try:
        secret = totp_secret_for(load_credentials(credentials_file), email)
    except DemoCredentialsError as exc:
        _fail(str(exc))
    code, remaining = current_code(secret)
    if remaining < FRESH_CODE_SECONDS:
        # Too little time left to type it: wait for the next window.
        typer.echo(f"waiting {remaining} s for the next code", err=True)
        time.sleep(remaining)
        code, remaining = current_code(secret)
    if one_line:
        typer.echo(f"{code}  (valid for {remaining} more seconds)")
        return
    # The code alone on stdout, so scripts can read it; the hint goes to stderr.
    typer.echo(code)
    typer.echo(f"valid for {remaining} more seconds", err=True)


@eval_app.command("export")
def eval_export(
    out: Annotated[
        Path, typer.Option(help="Directory of the dataset; images go to its images/.")
    ] = EVAL_DIR / "datasets",
) -> None:
    """Write the labelled cases to <out>/<date>.jsonl with their images (no API calls)."""
    from kalkan.ai.evaluation import export_dataset

    settings = get_settings()
    try:
        exported = export_dataset(
            get_database(), ObjectStore.from_settings(settings), out, now=utcnow()
        )
    except ConfigError as exc:
        _fail(str(exc))
    typer.echo(f"Exported {exported.cases} cases and {exported.images} images to {exported.path}")


@eval_app.command("run")
def eval_run(
    dataset: Annotated[Path, typer.Option(help="A dataset file from `kalkan eval export`.")],
    model: Annotated[
        list[str] | None,
        typer.Option(help="Model id; repeat to compare models. Default: AI_TRIAGE_MODEL."),
    ] = None,
    limit: Annotated[int | None, typer.Option(min=1, help="Only the first N cases.")] = None,
    batch: Annotated[
        bool, typer.Option("--batch", help="Message Batches API: half price, up to 24 hours.")
    ] = False,
    out: Annotated[Path, typer.Option(help="Directory of the reports.")] = EVAL_DIR / "reports",
    to_bucket: Annotated[
        bool, typer.Option("--to-bucket", help="Also store the reports under eval/ in the bucket.")
    ] = False,
) -> None:
    """Classify the dataset's cases with the triage prompt and write a report per model.

    Every call is billed to ANTHROPIC_API_KEY and, with a database, recorded in ai_calls.
    """
    from kalkan.ai.client import AiClient, DatabaseRecorder, MemoryRecorder, Recorder
    from kalkan.ai.evaluation import (
        comparison_markdown,
        load_dataset,
        report_markdown,
        report_name,
        run_eval,
    )

    settings = get_settings()
    if settings.anthropic_api_key is None:
        _fail("ANTHROPIC_API_KEY is required")
    if to_bucket and settings.gcs_reports_bucket is None:
        _fail("GCS_REPORTS_BUCKET is required with --to-bucket")
    models = model or [settings.ai_triage_model]
    unpriced = [name for name in models if name not in settings.ai_prices]
    if unpriced:
        _fail(f"no price in AI_PRICES for {', '.join(unpriced)}")
    try:
        loaded = load_dataset(dataset)
    except (OSError, ValueError) as exc:
        _fail(f"cannot read the dataset: {exc}")
    recorder: Recorder = (
        DatabaseRecorder(get_database()) if settings.database_url_worker else MemoryRecorder()
    )
    client = AiClient.create(
        settings.anthropic_api_key.get_secret_value(),
        prices=settings.ai_prices,
        monthly_budget_usd=settings.ai_monthly_budget_usd,
        recorder=recorder,
    )
    out.mkdir(parents=True, exist_ok=True)
    runs = []
    reports: list[tuple[str, str]] = []
    for name in models:
        run = run_eval(loaded, client, model=name, limit=limit, batch=batch, now=utcnow())
        runs.append(run)
        reports.append((report_name(run), report_markdown(run)))
        typer.echo(f"{name}: {len(run.results)} cases, ${run.cost_usd:.4f}")
    if len(runs) > 1:
        stamp = runs[0].started_at.date().isoformat()
        reports.append(
            (f"{stamp}-comparison-{runs[0].prompt_version}.md", comparison_markdown(runs))
        )
    store = ObjectStore.from_settings(settings) if to_bucket else None
    for file_name, text in reports:
        (out / file_name).write_text(text, encoding="utf-8")
        typer.echo(f"Wrote {out / file_name}")
        if store is not None and settings.gcs_reports_bucket is not None:
            stored = _store_report(store, settings.gcs_reports_bucket, file_name, text)
            typer.echo(f"Stored {stored}")


def _store_report(store: ObjectStore, bucket: str, file_name: str, text: str) -> str:
    """Reports are write-once objects: a second report of the day gets a time suffix."""
    from kalkan.storage.gcs import ObjectExistsError

    data = text.encode()
    try:
        return store.put(bucket, f"eval/{file_name}", data, "text/markdown").key
    except ObjectExistsError:
        stem = file_name.removesuffix(".md")
        path = f"eval/{stem}-{utcnow().strftime('%H%M%S')}.md"
        return store.put(bucket, path, data, "text/markdown").key
