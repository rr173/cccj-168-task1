"""队列 API:FastAPI 路由层。所有一致性逻辑在 SafetyPlatform 内完成。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from .domain import PlatformError
from .service import SafetyPlatform
from .workbench import WORKBENCH_HTML


# ------------------------------------------------------------ 请求模型

class TrialIn(BaseModel):
    name: str
    phase: str = "I"
    committee: list[dict]          # [{"id": "dsmb1", "name": "..."}]
    quorum: int | None = None
    approve_threshold: int | None = None
    ruleset: dict | None = None


class RulesetIn(BaseModel):
    version: str
    body: dict


class CohortIn(BaseModel):
    trial_id: str
    label: str
    dose_mg: float
    schedule: str
    window_days: int
    subjects: list[str]


class DoseIn(BaseModel):
    subject_code: str
    given_at: str | None = None    # YYYY-MM-DD,缺省为当日


class EventIn(BaseModel):
    subject_code: str
    description: str
    severity: int
    evaluability: str = "PENDING"
    related: bool = True
    occurred_at: str | None = None


class EventPatch(BaseModel):
    severity: int | None = None
    evaluability: str | None = None
    related: bool | None = None


class ReplaceIn(BaseModel):
    new_code: str
    reason: str = ""


class AddSubjectsIn(BaseModel):
    codes: list[str]


class DataStatusIn(BaseModel):
    status: str                    # COMPLETE / PENDING


class PauseIn(BaseModel):
    reason: str


class SessionIn(BaseModel):
    intent: str                    # ESCALATE / EXPAND / STOP / HOLD


class VoteIn(BaseModel):
    member_id: str
    vote: str                      # APPROVE / REJECT
    seen_state_version: int | None = None


# ------------------------------------------------------------ 应用工厂

def create_app(db_path: str = ":memory:", clock=None) -> FastAPI:
    platform = SafetyPlatform(db_path=db_path, clock=clock)
    app = FastAPI(title="剂量递增安全决策平台", version="1.0.0")
    app.state.platform = platform

    @app.exception_handler(PlatformError)
    async def _(req: Request, exc: PlatformError):
        return JSONResponse(status_code=exc.status, content={
            "error": {"code": exc.code, "message": str(exc), "details": exc.details}})

    # ---------------- 试验 / 规则
    @app.post("/api/trials", status_code=201)
    def create_trial(body: TrialIn):
        return platform.create_trial(
            body.name, body.phase, body.committee,
            body.quorum, body.approve_threshold, body.ruleset)

    @app.post("/api/trials/{trial_id}/rulesets", status_code=201)
    def amend_rules(trial_id: str, body: RulesetIn):
        return platform.amend_rules(trial_id, body.version, body.body)

    @app.post("/api/trials/{trial_id}/pause", status_code=201)
    def pause_trial(trial_id: str, body: PauseIn):
        return platform.pause("TRIAL", trial_id, body.reason)

    @app.post("/api/trials/{trial_id}/resume")
    def resume_trial(trial_id: str):
        return platform.resume("TRIAL", trial_id)

    # ---------------- 队列
    @app.post("/api/cohorts", status_code=201)
    def create_cohort(body: CohortIn):
        return platform.create_cohort(
            body.trial_id, body.label, body.dose_mg, body.schedule,
            body.window_days, body.subjects)

    @app.get("/api/cohorts/{cohort_id}/snapshot")
    def cohort_snapshot(cohort_id: str):
        return platform.snapshot_view(cohort_id)

    @app.post("/api/cohorts/{cohort_id}/doses", status_code=201)
    def record_dose(cohort_id: str, body: DoseIn):
        return platform.record_dose(cohort_id, body.subject_code, body.given_at)

    @app.post("/api/cohorts/{cohort_id}/events", status_code=201)
    def report_event(cohort_id: str, body: EventIn):
        return platform.report_event(
            cohort_id, body.subject_code, body.description, body.severity,
            body.evaluability, body.related, body.occurred_at)

    @app.patch("/api/events/{event_id}")
    def update_event(event_id: str, body: EventPatch):
        return platform.update_event(
            event_id, body.severity, body.evaluability, body.related)

    @app.post("/api/cohorts/{cohort_id}/subjects/{code}/replace", status_code=201)
    def replace_subject(cohort_id: str, code: str, body: ReplaceIn):
        return platform.replace_subject(cohort_id, code, body.new_code, body.reason)

    @app.post("/api/cohorts/{cohort_id}/subjects", status_code=201)
    def add_subjects(cohort_id: str, body: AddSubjectsIn):
        return platform.add_subjects(cohort_id, body.codes)

    @app.post("/api/cohorts/{cohort_id}/subjects/{code}/data-status")
    def set_data_status(cohort_id: str, code: str, body: DataStatusIn):
        return platform.set_data_status(cohort_id, code, body.status)

    @app.post("/api/cohorts/{cohort_id}/pause", status_code=201)
    def pause_cohort(cohort_id: str, body: PauseIn):
        return platform.pause("COHORT", cohort_id, body.reason)

    @app.post("/api/cohorts/{cohort_id}/resume")
    def resume_cohort(cohort_id: str):
        return platform.resume("COHORT", cohort_id)

    # ---------------- 决策会话 / 投票
    @app.post("/api/cohorts/{cohort_id}/sessions", status_code=201)
    def open_session(cohort_id: str, body: SessionIn):
        return platform.open_session(cohort_id, body.intent)

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        return platform.get_session(session_id)

    @app.post("/api/sessions/{session_id}/votes", status_code=201)
    def vote(session_id: str, body: VoteIn):
        return platform.vote(
            session_id, body.member_id, body.vote, body.seen_state_version)

    @app.post("/api/sessions/{session_id}/close")
    def close_session(session_id: str):
        return platform.close_session(session_id)

    # ---------------- 决策证明 / 账本
    @app.get("/api/cohorts/{cohort_id}/proofs")
    def list_proofs(cohort_id: str):
        return platform.list_proofs(cohort_id)

    @app.get("/api/proofs/{session_id}")
    def get_proof(session_id: str):
        return platform.get_proof(session_id)

    @app.get("/api/proofs/{session_id}/verify")
    def verify_proof(session_id: str):
        return platform.verify_proof(session_id)

    @app.get("/api/ledger")
    def ledger_entries():
        return platform.ledger.entries()

    @app.get("/api/ledger/verify")
    def verify_ledger():
        return platform.ledger.verify()

    # ---------------- 安全工作台
    @app.get("/", response_class=HTMLResponse)
    def workbench():
        return WORKBENCH_HTML

    return app
