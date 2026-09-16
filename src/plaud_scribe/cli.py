"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as config_module
from . import drive as drive_module
from .config import ConfigError, STATE_DB, Config, ensure_dirs
from .plaud import PlaudAuthError, PlaudClient, duration_seconds, parse_timestamp
from .render import format_duration
from .state import Store
from .sync import Pipeline, SyncResult, cache_path, slugify

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_AUTH = 2

DEFAULT_SYNC_DAYS = 7


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def _since(days: int | None) -> datetime | None:
    if not days:
        return None
    return datetime.now(timezone.utc) - timedelta(days=days)


def _report(result: SyncResult) -> None:
    if result.status == "done":
        details = [f"{result.cost_usd:.3f} USD"]
        if result.languages:
            details.append("/".join(result.languages))
        if result.speakers:
            details.append(f"{len(result.speakers)} speakers")
        if result.drive_files:
            details.append("uploaded " + ", ".join(sorted(result.drive_files)))
        print(f"  ok    {result.title}  ({'; '.join(details)})")
        for warning in result.warnings:
            print(f"        ! {warning}")
    elif result.status == "skipped":
        print(f"  skip  {result.title}: {result.error}")
    else:
        print(f"  FAIL  {result.title}: {result.error}")


# --- commands ------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and good
        print(f"  [{'ok' if good else 'XX'}] {label}{(' — ' + detail) if detail else ''}")

    print("plaud-scribe doctor")
    check("config file", cfg.path.exists(), str(cfg.path))
    check("output formats", True, ", ".join(cfg.drive.formats))
    check(
        "ElevenLabs API key",
        bool(cfg.elevenlabs.api_key),
        f"model {cfg.elevenlabs.model_id}" if cfg.elevenlabs.api_key
        else "set ELEVENLABS_API_KEY or [elevenlabs] api_key",
    )
    if cfg.summary_enabled:
        check(
            "Anthropic API key (summaries)",
            bool(cfg.summary.api_key),
            f"model {cfg.summary.model}, language {cfg.summary.language}" if cfg.summary.api_key
            else "set ANTHROPIC_API_KEY or [summary] api_key",
        )
    else:
        check("summaries", True, "disabled (no \"summary\" in [drive] formats)")

    cli_path = shutil.which(cfg.plaud.cli)
    check("Plaud CLI", bool(cli_path), cli_path or "npm install -g @plaud-ai/cli")
    try:
        with PlaudClient(cfg) as client:
            user = client.current_user()
            label = user.get("email") or user.get("nickname") or user.get("id") or "authenticated"
            check("Plaud auth", True, str(label))
    except PlaudAuthError as err:
        check("Plaud auth", False, str(err).splitlines()[0])
    except Exception as err:
        check("Plaud auth", False, f"{type(err).__name__}: {err}")

    if drive_module.GOOGLE_CLIENT_FILE.exists():
        try:
            uploader = drive_module.DriveUploader(cfg)
            folder = uploader.ensure_folder(cfg.drive.root_folder)
            check("Google Drive", True, f"{cfg.drive.root_folder} ({folder})")
        except Exception as err:
            check("Google Drive", False, f"{type(err).__name__}: {err}")
    else:
        check("Google Drive", False, "no OAuth client; run `plaud-scribe auth google`")

    check("state db", STATE_DB.parent.exists(), str(STATE_DB))
    check("cache dir", config_module.RAW_DIR.exists(), str(config_module.RAW_DIR))
    return EXIT_OK if ok else EXIT_ERROR


def cmd_auth(args: argparse.Namespace, cfg: Config) -> int:
    if args.target == "google":
        drive_module.authorize(cfg)
        print("Google Drive authorised.")
        return EXIT_OK

    with PlaudClient(cfg) as client:
        user = client.current_user()
        print(f"Plaud authenticated as {user.get('email') or user.get('id')}")
    return EXIT_OK


def cmd_list(args: argparse.Namespace, cfg: Config) -> int:
    with PlaudClient(cfg) as client, Store(STATE_DB) as store:
        rows = list(client.iter_files(since=_since(args.days), limit=args.limit))
        if not rows:
            print("No recordings found.")
            return EXIT_OK
        print(f"{'id':<26} {'recorded':<17} {'dur':>8}  {'state':<7} name")
        for item in rows:
            when = parse_timestamp(item.get("start_at") or item.get("created_at"))
            record = store.get(item.get("id", ""))
            state = record.status if record else "new"
            print(
                f"{item.get('id', ''):<26} "
                f"{when.astimezone().strftime('%Y-%m-%d %H:%M') if when else '-':<17} "
                f"{format_duration(duration_seconds(item)):>8}  "
                f"{state:<7} {item.get('name', '')}"
            )
    return EXIT_OK


def cmd_sync(args: argparse.Namespace, cfg: Config) -> int:
    days = None if args.all else args.days
    with PlaudClient(cfg) as client, Store(STATE_DB) as store:
        pipeline = Pipeline(cfg, plaud=client, store=store)
        pending = pipeline.select(
            since=_since(days), limit=args.limit, retry_failed=args.retry_failed
        )
        if not pending:
            print("Nothing to do.")
            return EXIT_OK

        print(f"{len(pending)} recording(s) to process:")
        if args.dry_run:
            for item in pending:
                print(
                    f"  would process {item.get('id')}  "
                    f"{format_duration(duration_seconds(item))}  {item.get('name', '')}"
                )
            return EXIT_OK

        failures = skipped = 0
        for item in pending:
            result = pipeline.process(
                item, upload=not args.no_upload, ignore_limits=args.ignore_limits
            )
            _report(result)
            failures += result.counts_as_failure
            skipped += result.status == "skipped"
        if skipped:
            print(
                f"{skipped} recording(s) held back by the audio limit; "
                "they are retried on the next run, or use --ignore-limits."
            )
        return EXIT_ERROR if failures else EXIT_OK


def cmd_transcribe(args: argparse.Namespace, cfg: Config) -> int:
    target = Path(args.target)
    with Store(STATE_DB) as store:
        if target.exists() and target.is_file():
            return _transcribe_local(args, cfg, store, target)
        with PlaudClient(cfg) as client:
            pipeline = Pipeline(cfg, plaud=client, store=store)
            item = client.get_file(args.target)
            item.setdefault("id", args.target)
            result = pipeline.process(
                item,
                upload=not args.no_upload,
                force=args.force,
                ignore_limits=args.ignore_limits,
            )
            _report(result)
            for extension, path in sorted(result.local_files.items()):
                print(f"        {extension}: {path}")
            return EXIT_OK if result.status == "done" else EXIT_ERROR


def _transcribe_local(
    args: argparse.Namespace, cfg: Config, store: Store, target: Path
) -> int:
    """Transcribe a local audio file — handy for testing without touching Plaud."""
    from .stt.elevenlabs import ElevenLabsProvider

    recording_id = f"local-{slugify(target.stem)}"
    pipeline = Pipeline(cfg, plaud=None, store=store, provider=ElevenLabsProvider(cfg))
    item = {
        "id": recording_id,
        "name": target.stem,
        "start_at": datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc).isoformat(),
        "duration": 0,
    }
    if args.force or not cache_path(recording_id).exists():
        budget = pipeline.budget()
        if budget is not None and not args.ignore_limits and budget.remaining_seconds <= 0:
            print(f"  skip  {item['name']}: audio limit reached; use --ignore-limits")
            return EXIT_OK
        transcript = pipeline.provider.transcribe(file_path=str(target))
        pipeline.write_cache(recording_id, item, transcript)
    rendered = pipeline.render_from_cache(recording_id)
    local = pipeline.write_local(rendered)
    store.note_seen(recording_id, item["name"], item["start_at"], 0.0)
    drive_files = {}
    if not args.no_upload:
        drive_files = pipeline.upload(rendered, {})
    store.mark_done(
        recording_id,
        provider=rendered.meta.provider,
        model_id=rendered.meta.model_id,
        cost_usd=pipeline.provider.estimate_cost(rendered.meta.duration_seconds),
        languages=",".join(rendered.languages),
        speaker_count=len(rendered.speaker_labels),
        drive_files=drive_files or None,
    )
    print(f"  ok    {item['name']}  ({'/'.join(rendered.languages) or 'unknown'})")
    for extension, path in sorted(local.items()):
        print(f"        {extension}: {path}")
    return EXIT_OK


def cmd_render(args: argparse.Namespace, cfg: Config) -> int:
    with Store(STATE_DB) as store:
        pipeline = Pipeline(cfg, plaud=None, store=store)
        rendered = pipeline.render_from_cache(args.recording_id, resummarize=args.resummarize)
        local = pipeline.write_local(rendered)
        note = "summary regenerated" if args.resummarize else "no transcription cost"
        print(f"Rendered {rendered.meta.title} from cache ({note}).")
        for extension, path in sorted(local.items()):
            print(f"  {extension}: {path}")
        for warning in rendered.warnings:
            print(f"  ! {warning}")
        if args.upload:
            record = store.get(args.recording_id)
            uploaded = pipeline.upload(rendered, record.drive if record else {})
            store.mark_done(
                args.recording_id,
                provider=rendered.meta.provider,
                model_id=rendered.meta.model_id,
                cost_usd=record.cost_usd if record else None,
                languages=",".join(rendered.languages),
                speaker_count=len(rendered.speaker_labels),
                drive_files={**(record.drive if record else {}), **uploaded},
            )
            print("  uploaded: " + ", ".join(sorted(uploaded)))
    return EXIT_OK


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    with Store(STATE_DB) as store:
        pipeline = Pipeline(cfg, plaud=None, store=store)
        budgets = pipeline.budgets()
        if not budgets:
            print("audio limits: none set")
        for budget in budgets:
            print(
                f"audio limit per {budget.period}: {budget.remaining_minutes:.0f} of "
                f"{budget.limit_minutes:.0f} minutes left"
            )
        counts = store.counts()
        if not counts:
            print("Nothing recorded yet. Run `plaud-scribe sync`.")
            return EXIT_OK
        print("counts: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        month_start = datetime.now(timezone.utc).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        )
        spend = store.spend_since(month_start.isoformat())
        # Two decimals would render a real but small spend as "$0.00".
        rendered_spend = f"${spend:.2f}" if spend >= 0.01 or spend == 0 else f"${spend:.4f}"
        print(f"month-to-date spend (transcription + summaries): {rendered_spend}")
        print()
        print(f"{'recorded':<17} {'state':<7} {'cost':>6}  {'langs':<12} name")
        for record in store.recent(args.limit):
            when = parse_timestamp(record.recorded_at)
            print(
                f"{when.astimezone().strftime('%Y-%m-%d %H:%M') if when else '-':<17} "
                f"{record.status:<7} "
                f"{(f'${record.cost_usd:.2f}' if record.cost_usd else '-'):>6}  "
                f"{(record.languages or '-'):<12} {record.name}"
            )
            if record.status == "failed" and record.error:
                print(f"                  ! {record.error}")
    return EXIT_OK


# --- argument parsing ----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="plaud-scribe",
        description="Transcribe Plaud recordings with ElevenLabs Scribe v2 and file them in Google Drive.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--config", type=Path, help="Path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check credentials and paths").set_defaults(func=cmd_doctor)

    auth = sub.add_parser("auth", help="authorise a service")
    auth.add_argument("target", choices=["google", "plaud"])
    auth.set_defaults(func=cmd_auth)

    listing = sub.add_parser("list", help="list Plaud recordings")
    listing.add_argument("--days", type=int, default=30)
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=cmd_list)

    sync = sub.add_parser("sync", help="transcribe and upload everything new")
    sync.add_argument("--days", type=int, default=DEFAULT_SYNC_DAYS)
    sync.add_argument("--all", action="store_true", help="ignore --days and walk the full history")
    sync.add_argument("--limit", type=int)
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--retry-failed", action="store_true")
    sync.add_argument("--no-upload", action="store_true")
    sync.add_argument(
        "--ignore-limits", action="store_true", help="transcribe even if [limits] is exhausted"
    )
    sync.set_defaults(func=cmd_sync)

    transcribe = sub.add_parser("transcribe", help="one recording id, or a local audio file")
    transcribe.add_argument("target")
    transcribe.add_argument("--no-upload", action="store_true")
    transcribe.add_argument("--force", action="store_true", help="re-transcribe, ignoring the cache")
    transcribe.add_argument(
        "--ignore-limits", action="store_true", help="transcribe even if [limits] is exhausted"
    )
    transcribe.set_defaults(func=cmd_transcribe)

    rerender = sub.add_parser("render", help="re-render from the cached transcript (free)")
    rerender.add_argument("recording_id")
    rerender.add_argument("--upload", action="store_true")
    rerender.add_argument(
        "--resummarize", action="store_true", help="ask Claude for a fresh summary (costs a model call)"
    )
    rerender.set_defaults(func=cmd_render)

    status = sub.add_parser("status", help="what has been processed, and what it cost")
    status.add_argument("--limit", type=int, default=20)
    status.set_defaults(func=cmd_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    ensure_dirs()
    try:
        cfg = config_module.load(args.config)
        return args.func(args, cfg)
    except PlaudAuthError as err:
        print(f"\n{err}", file=sys.stderr)
        return EXIT_AUTH
    except (ConfigError, drive_module.DriveError) as err:
        print(f"\n{err}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
