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
import time
from datetime import datetime

from .checkpoint import CheckpointStore, SessionState
from .config import REPO_ROOT, Config, load_config
from .errors import StopCondition, StopReason
from .runner import (
    SessionRunner,
    check_active_hours,
    check_cooldown,
    check_gate,
    mark_manual_verified,
)

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
    p.add_argument("--login", action="store_true",
                   help="open a visible browser, log in by hand, then close (no likes touched)")
    p.add_argument("--logout", action="store_true",
                   help="wipe the dedicated browser profile (local logout) and exit")
    p.add_argument("--unattended", action="store_true",
                   help="scheduled normal sessions without the UNLIKE prompt; needs "
                        "INSTACLEAN_ALLOW_UNATTENDED=true in .env and all stages done")
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


def unattended_refusal(cfg: Config, args: argparse.Namespace) -> str | None:
    """Unattended runs are only normal real sessions, explicitly allowed in .env."""
    if not cfg.allow_unattended:
        return "set INSTACLEAN_ALLOW_UNATTENDED=true in .env"
    if not args.real or cfg.dry_run:
        return "needs --real and INSTACLEAN_DRY_RUN=false"
    if args.limit is not None:
        return "only for normal sessions (no --limit)"
    return None


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
    if args.unattended:
        refusal = unattended_refusal(cfg, args)
        if refusal:
            print(f"Unattended mode refused: {refusal}")
            return 2

    store = CheckpointStore(cfg.checkpoints_dir)
    if args.logout:
        from .browser import wipe_profile
        wiped = wipe_profile(cfg.profile_dir)
        print("Profile wiped: logged out on this Mac." if wiped else "No profile to wipe.")
        print("Also log out the session on Instagram: Settings > Accounts Center > "
              "Password and security > Where you're logged in.")
        return 0
    if args.login:
        return run_login(cfg)
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
            check_active_hours(cfg)
            if args.unattended and cfg.start_jitter_minutes:
                # A scheduler fires at the same clock time every day; a person doesn't.
                delay = random.uniform(0, cfg.start_jitter_minutes * 60)
                print(f"Unattended: waiting {delay / 60:.1f} min before starting")
                time.sleep(delay)
                check_active_hours(cfg)
    except StopCondition as exc:
        print(f"STOP: {exc}")
        return 2

    setup_logging(cfg)
    log.info("Instaclean starting: dry_run=%s limit=%s browser=%s content=%s debug=%s",
             cfg.dry_run, args.limit, cfg.browser_mode, cfg.content, cfg.debug)

    # Imported here so --status / --mark-verified work without a browser.
    from .browser import expire_login_if_needed, open_browser, record_login, wipe_profile
    from .instagram import InstagramLikesUI

    expire_login_if_needed(cfg)
    rng = random.Random()
    interactive = cfg.browser_mode == "visible" and not args.unattended
    try:
        return _run_session(cfg, args, store, rng, interactive, open_browser, record_login,
                            InstagramLikesUI)
    finally:
        if cfg.wipe_profile_after_run:
            wipe_profile(cfg.profile_dir)


def run_login(cfg: Config) -> int:
    """Visible browser, manual login (2FA included), record the login time, close."""
    setup_logging(cfg)
    from .browser import open_browser, record_login
    from .instagram import InstagramLikesUI

    with open_browser(cfg, force_visible=True) as (_context, page):
        ui = InstagramLikesUI(page, random.Random(), on_logged_in=lambda: record_login(cfg.profile_dir))
        try:
            ui.ensure_logged_in()
        except StopCondition as exc:
            log.error("Login not completed: %s", exc)
            return 2
    print(f"Logged in. The login expires on this Mac after {cfg.session_max_days} days"
          if cfg.session_max_days else "Logged in.")
    return 0


def _run_session(cfg, args, store, rng, interactive, open_browser, record_login,
                 InstagramLikesUI) -> int:
    with open_browser(cfg) as (_context, page):
        ui = InstagramLikesUI(page, rng, content=cfg.content, interactive_login=interactive,
                              on_logged_in=lambda: record_login(cfg.profile_dir))
        runner = SessionRunner(ui, cfg, store, rng)
        try:
            if args.unattended:
                log.warning("UNATTENDED run: confirmation given by INSTACLEAN_ALLOW_UNATTENDED")
            confirm = (lambda _s: True) if args.unattended else confirm_real
            session = runner.run(limit=args.limit, confirm=confirm)
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
                and cfg.browser_mode != "headless" \
                and session.stop_reason != StopReason.BROWSER_DISCONNECTED.value:
            try:
                input("Stopped. Inspect the browser, then press Enter to close it...")
            except EOFError:
                pass
    return EXIT_CODES.get(session.status, 1)


if __name__ == "__main__":
    raise SystemExit(main())
