"""Resumable per-run state manager storing artifacts strictly inside workspace_runs/."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import STUDIO_ROOT
from core.models import STAGE_ORDER, VALID_STAGE_STATUSES, RunState, StageRecord
from core.validators import assert_path_within, validate_slug

DEFAULT_RUNS_ROOT = STUDIO_ROOT / "workspace_runs"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_run_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")[:-3]


def _atomic_write_json(target: Path, payload: dict[str, Any], runs_root: Path) -> None:
    assert_path_within(target, runs_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp_path, target)


class RunStateManager:
    """Manages workspace_runs/<app-slug>/<run-timestamp>/run_state.json."""

    def __init__(self, run_dir: Path, runs_root: Path = DEFAULT_RUNS_ROOT) -> None:
        self.runs_root = runs_root.resolve()
        self.run_dir = assert_path_within(run_dir, self.runs_root)
        self.state_path = self.run_dir / "run_state.json"

    @classmethod
    def create_new(
        cls,
        app_slug: str,
        mode: str,
        runs_root: Path = DEFAULT_RUNS_ROOT,
        run_id: str | None = None,
    ) -> tuple[RunStateManager, RunState]:
        slug = validate_slug(app_slug)
        resolved_run_id = run_id or make_run_timestamp()
        run_dir = runs_root / slug / resolved_run_id
        manager = cls(run_dir=run_dir, runs_root=runs_root)
        now = utc_now_iso()
        state = RunState(
            schema_version=1,
            run_id=resolved_run_id,
            app_slug=slug,
            mode=mode,
            created_at=now,
            updated_at=now,
            run_dir=str(manager.run_dir),
            overall_status="running",
            human_approved=False,
            stages={stage: StageRecord() for stage in STAGE_ORDER},
        )
        manager.save(state)
        return manager, state

    @classmethod
    def load(cls, run_dir: Path, runs_root: Path = DEFAULT_RUNS_ROOT) -> tuple[RunStateManager, RunState]:
        manager = cls(run_dir=run_dir, runs_root=runs_root)
        if not manager.state_path.exists():
            raise FileNotFoundError(f"run_state.json not found at {manager.state_path}")
        raw = json.loads(manager.state_path.read_text(encoding="utf-8"))
        return manager, RunState.from_dict(raw)

    @classmethod
    def find_latest_run(cls, app_slug: str, runs_root: Path = DEFAULT_RUNS_ROOT) -> Path | None:
        slug = validate_slug(app_slug)
        app_dir = runs_root / slug
        if not app_dir.exists():
            return None
        candidates = sorted(
            (p for p in app_dir.iterdir() if p.is_dir() and (p / "run_state.json").exists()),
            reverse=True,
        )
        return candidates[0] if candidates else None

    def save(self, state: RunState) -> None:
        state.updated_at = utc_now_iso()
        _atomic_write_json(self.state_path, state.to_dict(), self.runs_root)

    def write_json_artifact(self, relative_path: str, payload: dict[str, Any]) -> Path:
        target = self.run_dir / relative_path
        _atomic_write_json(target, payload, self.run_dir)
        return target

    def update_stage(
        self,
        state: RunState,
        stage_name: str,
        status: str,
        message: str = "",
        artifacts: dict[str, str] | None = None,
        warnings: list[str] | None = None,
    ) -> RunState:
        if stage_name not in state.stages:
            raise KeyError(f"Unknown stage '{stage_name}'")
        if status not in VALID_STAGE_STATUSES:
            raise ValueError(f"Invalid stage status '{status}'")

        rec = state.stages[stage_name]
        now = utc_now_iso()
        if status == "running" and rec.started_at is None:
            rec.started_at = now
        if status in ("completed", "skipped", "failed", "awaiting_human_approval"):
            if rec.started_at is None:
                rec.started_at = now
            rec.finished_at = now

        rec.status = status
        if message:
            rec.message = message
        if artifacts:
            rec.artifacts.update(artifacts)
        if warnings is not None:
            rec.warnings = list(warnings)

        # Update overall status cleanly
        statuses = {s.status for s in state.stages.values()}
        if "failed" in statuses:
            state.overall_status = "failed"
        elif "awaiting_human_approval" in statuses:
            state.overall_status = "awaiting_human_approval"
        elif all(st in ("completed", "skipped") for st in statuses):
            state.overall_status = "completed"
        elif "running" in statuses:
            state.overall_status = "running"

        self.save(state)
        return state
