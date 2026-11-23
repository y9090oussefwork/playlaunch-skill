"""Typed schema dataclasses for PlayLaunch Studio pipeline state and manifests."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STAGE_ORDER = (
    "01_intake",
    "02_audit_and_build",
    "03_capture_screens",
    "04_aso_research",
    "05_copy_and_policy_guard",
    "06_render_visuals",
    "07_isolated_publish",
)

VALID_STAGE_STATUSES = frozenset(
    {
        "pending",
        "running",
        "completed",
        "skipped",
        "failed",
        "awaiting_human_approval",
    }
)


@dataclass
class StageRecord:
    status: str = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    message: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunState:
    schema_version: int
    run_id: str
    app_slug: str
    mode: str
    created_at: str
    updated_at: str
    run_dir: str
    overall_status: str
    human_approved: bool = False
    stages: dict[str, StageRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "app_slug": self.app_slug,
            "mode": self.mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_dir": self.run_dir,
            "overall_status": self.overall_status,
            "human_approved": self.human_approved,
            "stages": {k: v.to_dict() for k, v in self.stages.items()},
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunState:
        raw_stages = data.get("stages", {})
        stages = {
            name: StageRecord(
                status=rec.get("status", "pending"),
                started_at=rec.get("started_at"),
                finished_at=rec.get("finished_at"),
                message=rec.get("message", ""),
                artifacts=dict(rec.get("artifacts", {})),
                warnings=list(rec.get("warnings", [])),
            )
            for name, rec in raw_stages.items()
        }
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            run_id=str(data["run_id"]),
            app_slug=str(data["app_slug"]),
            mode=str(data["mode"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
            run_dir=str(data["run_dir"]),
            overall_status=str(data.get("overall_status", "pending")),
            human_approved=bool(data.get("human_approved", False)),
            stages=stages,
        )
