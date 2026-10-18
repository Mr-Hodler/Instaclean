"""Command line entry point.

    python -m instaclean                      # dry run (default)
    python -m instaclean --real --limit 3     # first real test: 3 likes
    python -m instaclean --mark-verified      # after checking the 3 by hand
    python -m instaclean --real --limit 20    # small first batch
    python -m instaclean --real               # normal batches (80-100), session cap 500
    python -m instaclean --status             # show checkpoints
    python -m instaclean --doctor             # check the environment

Real mode needs BOTH  INSTACLEAN_DRY_RUN=false  in .env  AND  --real.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from datetime import datetime

from .checkpoint import CheckpointStore, SessionState
from .config import REPO_ROOT, Config, load_config
from .errors import StopCondition, StopReason
from .runner import SessionRunner, check_cooldown, check_gate, mark_manual_verified

log = logging.getLogger("instaclean")

EXIT_CODES = {
    "dry_run_ok": 0,
    "completed": 0,
    "session_limit_reached": 0,
    "recovered": 0,
    "empty_unconfirmed": 4,
    "stopped": 2,
    "recovery_stopped": 2,
    "uncertain": 3,
    "error": 1,
}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="instaclean", description="Batch-remove your Instagram likes "
                                "through the normal web UI. Dry run by default.")
    p.add_argument("--real", action="store_true",
                   help="perform real unlikes (also requires INSTACLEAN_DRY_RUN=false in .env)")
    p.add_argument("--limit", type=int, default=None,
                   help="max unlikes this session (3 = first test, <=100 = first batch)")
    p.add_argument("--mark-verified", action="store_true",
                   help="record that you checked the 3-like test by hand")
    p.add_argument("--status", action="store_true", help="print checkpoint state and exit")
    p.add_argument("--doctor", action="store_true",
                   help="check Python, dependencies, Chromium, profile, .env and git hygiene")
    p.add_argument("--debug", action="store_true", help="save screenshots/DOM snapshots locally")
    return p


def setup_logging(cfg: Config) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(cfg.logs_dir, 0o700)
    logfile = cfg.logs_dir / f"instaclean-{datetime.now():%Y%m%d}.log"
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if cfg.debug else logging.INFO)
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    root.handlers[:] = [fh, sh]


def confirm_real(summary: str) -> bool:
    if not sys.stdin.isatty():
        log.error("Real mode needs an interactive terminal for confirmation.")
        return False
    print("\n" + summary)
    print("Instagram may restrict accounts that use automation; limits here are local caps only.")
    try:
        answer = input("Type UNLIKE (uppercase) to continue: ").strip()
    except EOFError:
        log.error("Confirmation input closed: real mode not confirmed")
        return False
    if answer != "UNLIKE":
        log.error("Confirmation not given (typed %d characters, expected 'UNLIKE')", len(answer))
        return False
    return True


def print_summary(s: SessionState) -> None:
    keys = ("session_id", "mode", "stage", "status", "attempted", "successful", "failed",
            "uncertain", "batches_completed", "last_batch_size", "dry_run_selected",
            "stop_reason", "stop_detail")
    print(json.dumps({k: getattr(s, k) for k in keys}, indent=2))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(env_file=REPO_ROOT / ".env", debug=True if args.debug else None)
    except ValueError as exc:
        print(f"Configuration error: {exc}")
        return 1
    if args.real and cfg.dry_run:
        print("Real mode refused: set INSTACLEAN_DRY_RUN=false in .env AND pass --real.")
        return 2
    if not args.real and not cfg.dry_run:
        cfg = load_config(env_file=REPO_ROOT / ".env", dry_run=True,
                          debug=True if args.debug else None)  # --real is mandatory
    if args.limit is not None and args.limit < 1:
        print("--limit must be >= 1")
        return 1

    store = CheckpointStore(cfg.checkpoints_dir)
    if args.doctor:
        from .doctor import run_doctor
        return run_doctor(cfg, store)
    try:
        if args.status:
            last = store.last_session()
            state = store.load_state()
            print(json.dumps({"stages": state["stages"],
                              "last_real_session": state.get("last_real_session"),
                              "last_block": state.get("last_block"),
                              "last_session": last.to_dict() if last else None}, indent=2))
            return 0
        if args.mark_verified:
            mark_manual_verified(store)
            print("Recorded: manual verification of the real test.")
            return 0
        last = store.last_session()
        if not (last and last.pending_batch):
            check_gate(store, cfg, args.limit)  # fail before opening the browser
            check_cooldown(store, cfg)
    except StopCondition as exc:
        print(f"STOP: {exc}")
        return 2

    setup_logging(cfg)
    log.info("Instaclean starting: dry_run=%s limit=%s debug=%s", cfg.dry_run, args.limit, cfg.debug)

    # Imported here so --status / --mark-verified work without a browser.
    from .browser import open_browser
    from .instagram import InstagramLikesUI

    rng = random.Random()
    with open_browser(cfg) as (_context, page):
        ui = InstagramLikesUI(page, rng)
        runner = SessionRunner(ui, cfg, store, rng)
        try:
            session = runner.run(limit=args.limit, confirm=confirm_real)
        except StopCondition as exc:  # gate / user abort before the session exists
            log.error("STOP before session start: %s", exc)
            return 2
        stopped = session.status not in ("dry_run_ok", "completed", "session_limit_reached",
                                         "recovered")
        if stopped:
            log.warning("Diagnostics: %s", ui.diagnostics() if not page.is_closed() else "closed")
            if cfg.debug and not page.is_closed():
                ui.capture_debug(cfg.debug_dir / session.session_id, session.status)
        print_summary(session)
        if stopped and sys.stdin.isatty() and not page.is_closed() \
                and session.stop_reason != StopReason.BROWSER_DISCONNECTED.value:
            try:
                input("Stopped. Inspect the browser, then press Enter to close it...")
            except EOFError:
                pass
    return EXIT_CODES.get(session.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
