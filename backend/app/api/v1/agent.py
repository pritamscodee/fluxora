from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Response, status
from sqlalchemy import func, select

from app.agents import guardrails
from app.agents.bus import get_bus
from app.agents.graph import run_execute_phase, run_plan_phase
from app.agents.llm import trace_url
from app.agents.tools import tool_catalogue
from app.api.deps import DbSession, OwnedProject, Pagination
from app.core.config import settings
from app.core.errors import Conflict, FluxoraError, NotFound, RateLimited
from app.models import AgentRun, utcnow
from app.schemas.common import envelope
from app.schemas.platform import AgentApproval, AgentRunCreate, AgentRunOut

log = logging.getLogger("fluxora.api.agent")

router = APIRouter(prefix="/projects/{project_id}/agent", tags=["agent"])


class ModelUnavailable(FluxoraError):
    error_type = "model_unavailable"

    def __init__(self, messages: str | list[str]):
        super().__init__(messages, status.HTTP_503_SERVICE_UNAVAILABLE)


@router.get("/tools")
async def list_tools(project: OwnedProject):
    """The agent's full tool surface, including each tool's risk level, so a dashboard can
    show exactly what it is allowed to do."""
    return envelope(
        {
            "tools": tool_catalogue(),
            "limits": {
                "max_plan_steps": settings.agent_max_plan_steps,
                "max_tool_calls": settings.agent_max_tool_calls,
                "approval_required_for": ["expensive", "destructive"],
                "tool_retries": 5,
            },
            "provider": settings.llm_provider,
            "model": (
                settings.openai_model
                if settings.llm_provider == "openai"
                else settings.anthropic_model
            ),
            "configured": settings.llm_configured,
        }
    )


async def _enforce_rate_limit(db, project_id: str) -> None:
    since = utcnow() - timedelta(hours=1)
    recent = await db.scalar(
        select(func.count())
        .select_from(AgentRun)
        .where(AgentRun.project_id == project_id, AgentRun.created_at >= since)
    )
    if int(recent or 0) >= settings.agent_max_runs_per_hour:
        raise RateLimited(
            f"this project has started {recent} agent runs in the last hour; "
            f"the limit is {settings.agent_max_runs_per_hour}"
        )


@router.post("/runs", status_code=status.HTTP_201_CREATED)
async def create_run(
    body: AgentRunCreate, project: OwnedProject, db: DbSession
):
    """Plan a run. High-risk plans stop here and wait for approval."""
    if not settings.llm_configured:
        raise ModelUnavailable(
            f"no API key is configured for LLM provider {settings.llm_provider!r}; "
            "set OPENAI_API_KEY to enable the agent"
        )
    await _enforce_rate_limit(db, project.id)

    prompt = body.prompt
    if body.asset_id:
        # Appended rather than interpolated into the system prompt, so an asset id cannot
        # smuggle instructions past the input guardrail.
        prompt = f"{prompt}\n\n(The user is referring to asset id: {body.asset_id})"

    run = AgentRun(
        project_id=project.id,
        thread_id=f"thread_{project.id}",
        prompt=body.prompt,
        status="planning",
    )
    db.add(run)
    await db.flush()
    # Committed before the model call: a run that then crashes must still be visible and
    # attributable rather than silently absent.
    await db.commit()

    try:
        state = await run_plan_phase(
            run_id=run.id, project_id=project.id, prompt=prompt
        )
    except Exception as exc:
        log.exception("agent run %s failed during planning", run.id)
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        await db.flush()
        raise ModelUnavailable(f"the agent could not plan this request: {exc}") from exc

    # A plan with nothing risky in it is carried straight through to execution by the
    # graph itself, so this records the outcome rather than kicking off a second run —
    # invoking the executor again here would perform every tool call twice.
    run.plan = state.get("plan")
    run.requires_approval = bool(state.get("requires_approval"))
    run.status = state.get("status", "planned")
    run.steps = state.get("observations") or None
    run.error = state.get("error")
    run.result = {
        "answer": state.get("answer"),
        "reasoning": state.get("reasoning"),
        "tool_calls": state.get("tool_calls", 0),
    }
    run.trace_url = trace_url(run.id)
    if run.status == "completed" and not run.requires_approval:
        run.approved = True
        run.approved_at = utcnow()

    await db.flush()
    return envelope(_out(run, state))


@router.post("/runs/{run_id}/approve")
async def approve_run(
    run_id: str, body: AgentApproval, project: OwnedProject, db: DbSession
):
    run = await _run(db, project.id, run_id)
    if run.status != "awaiting_approval":
        raise Conflict(f"run is {run.status} and is not waiting for approval")

    run.approved = body.approved
    run.approved_at = utcnow()

    if not body.approved:
        run.status = "rejected"
        run.result = {"answer": "The plan was rejected, so nothing was run."}
        await db.flush()
        return envelope(_out(run, {}))

    state = await _execute(db, run, run.prompt)
    await db.flush()
    return envelope(_out(run, state))


async def _execute(db, run: AgentRun, prompt: str) -> dict:
    try:
        state = await run_execute_phase(
            run_id=run.id, project_id=run.project_id, prompt=prompt, plan=run.plan or []
        )
    except guardrails.GuardrailViolation as exc:
        run.status = "blocked"
        run.error = exc.reason
        return {}
    except Exception as exc:
        log.exception("agent run %s failed during execution", run.id)
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"
        return {}

    run.status = state.get("status", "completed")
    run.steps = state.get("observations")
    run.error = state.get("error")
    run.result = {
        "answer": state.get("answer"),
        "tool_calls": state.get("tool_calls", 0),
    }
    return state


@router.get("/runs")
async def list_runs(
    project: OwnedProject, db: DbSession, page: Pagination, response: Response
):
    base = select(AgentRun).where(AgentRun.project_id == project.id)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = (
        await db.scalars(
            base.order_by(AgentRun.created_at.desc()).limit(page.limit).offset(page.offset)
        )
    ).all()
    response.headers["X-Total-Count"] = str(total or 0)
    return envelope([AgentRunOut.model_validate(r) for r in rows])


@router.get("/runs/{run_id}")
async def get_run(run_id: str, project: OwnedProject, db: DbSession):
    return envelope(AgentRunOut.model_validate(await _run(db, project.id, run_id)))


# ---- Task queue visibility ----

@router.get("/queue")
async def queue_health(project: OwnedProject):
    """Queue depth and dead-letter count for the agent tool bus."""
    return envelope(await get_bus().health())


@router.get("/dead-letters")
async def list_dead_letters(project: OwnedProject, page: Pagination):
    """Tool tasks that exhausted all five attempts.

    Scoped to this project: the dead-letter queue is process-wide, so entries belonging
    to other projects are filtered out rather than exposed.
    """
    letters = await get_bus().dead_letters(limit=200)
    mine = [
        dead
        for dead in letters
        if (dead.get("payload") or {}).get("project_id") == project.id
    ]
    return envelope(mine[: page.limit])


@router.post("/dead-letters/{job_id}/replay")
async def replay_dead_letter(job_id: str, project: OwnedProject):
    bus = get_bus()
    owned = [
        dead
        for dead in await bus.dead_letters(limit=500)
        if dead.get("job_id") == job_id
        and (dead.get("payload") or {}).get("project_id") == project.id
    ]
    if not owned:
        raise NotFound(f"dead-lettered task {job_id} was not found for this project")
    new_id = await bus.replay_dead_letter(job_id)
    if new_id is None:
        raise Conflict("the task could not be replayed; it may have just been removed")
    return envelope({"replayed_from": job_id, "new_job_id": new_id})


# ---- helpers ----

async def _run(db, project_id: str, run_id: str) -> AgentRun:
    run = await db.scalar(
        select(AgentRun).where(AgentRun.id == run_id, AgentRun.project_id == project_id)
    )
    if run is None:
        raise NotFound(f"agent run {run_id} was not found")
    return run


def _out(run: AgentRun, state: dict) -> dict:
    payload = AgentRunOut.model_validate(run).model_dump()
    payload["answer"] = (run.result or {}).get("answer") or state.get("answer")
    payload["observations"] = run.steps or state.get("observations") or []
    return payload
