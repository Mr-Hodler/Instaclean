"""Local checkpoints (JSON, atomic writes).

Files under checkpoints/ (gitignored, 0600):
- state.json            : stage progression + id of the last session
- session-<id>.json     : counters of one session, saved after every batch

No cookies, tokens, usernames or media URLs are stored. Items of an
in-flight batch are stored only as short SHA-256 hashes, to allow a
non-destructive verification after a crash.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import StopCondition, StopReason

SCHEMA_VERSION = 1
STAGES = ("dry_run", "real_test", "manual_verified", "first_batch")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class SessionState:
    session_id: str
    mode: str  # "dry_run" | "real"
    stage: str  # which stage this run belongs to
    action_limit: int
    started_at: str
    updated_at: str = ""
    attempted: int = 0
    successful: int = 0
    failed: int = 0
    uncertain: int = 0
    batches_completed: int = 0
    last_batch_size: int = 0
    status: str = "running"
    stop_reason: str | None = None
    stop_detail: str | None = None
    dry_run_selected: int = 0
    pending_batch: dict[str, Any] | None = None
    recovery: dict[str, Any] | None = None
    batches: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


class CheckpointStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)

    # ---- low level -------------------------------------------------------
    def _ensure_dir(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)

    def _write(self, path: Path, data: dict[str, Any]) -> None:
        self._ensure_dir()
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)

    def _read(self, path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # Never overwrite a checkpoint we cannot read: the user must look.
            raise StopCondition(StopReason.CHECKPOINT_ERROR, f"unreadable {path.name}: {exc}")
        if not isinstance(data, dict):
            raise StopCondition(StopReason.CHECKPOINT_ERROR, f"invalid {path.name}")
        return data

    # ---- state -----------------------------------------------------------
    @property
    def state_path(self) -> Path:
        return self.dir / "state.json"

    def load_state(self) -> dict[str, Any]:
        data = self._read(self.state_path) or {}
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("stages", {})
        data.setdefault("last_session_id", None)
        return data

    def save_state(self, state: dict[str, Any]) -> None:
        self._write(self.state_path, state)

    def stages(self) -> dict[str, str]:
        return dict(self.load_state()["stages"])

    def mark_stage(self, name: str) -> None:
        if name not in STAGES:
            raise ValueError(f"unknown stage {name}")
        state = self.load_state()
        state["stages"][name] = utcnow()
        self.save_state(state)

    # ---- sessions --------------------------------------------------------
    def session_path(self, session_id: str) -> Path:
        return self.dir / f"session-{session_id}.json"

    def new_session(self, *, mode: str, stage: str, action_limit: int) -> SessionState:
        now = utcnow()
        sid = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
        session = SessionState(
            session_id=sid, mode=mode, stage=stage, action_limit=action_limit, started_at=now
        )
        self.save_session(session)
        state = self.load_state()
        state["last_session_id"] = sid
        self.save_state(state)
        return session

    def save_session(self, session: SessionState) -> None:
        session.updated_at = utcnow()
        self._write(self.session_path(session.session_id), session.to_dict())

    def load_session(self, session_id: str) -> SessionState | None:
        data = self._read(self.session_path(session_id))
        return SessionState.from_dict(data) if data else None

    def last_session(self) -> SessionState | None:
        sid = self.load_state().get("last_session_id")
        return self.load_session(sid) if sid else None
