"""LangGraph orchestration for the video agent.

    guard_input -> plan -> policy -> [await_approval] -> execute -> respond

The graph is invoked twice for a run that needs approval: once to produce and screen a
plan, and again after a human approves it. Resumption is driven by the persisted
`AgentRun` row rather than a LangGraph checkpointer, because approval can arrive minutes
later and against a different API process — an in-memory checkpoint would not be there,
and the database already has to be the durable record for audit purposes.

Tool execution never happens inline: every call is dispatched to BullMQ (app/agents/bus)
and awaited, so retries, dead-lettering and process isolation apply uniformly.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

from app.agents import guardrails
from app.agents.bus import get_bus
from app.agents.llm import LLMUnavailable, build_llm, configure_tracing
from app.agents.tools import REGISTRY, tool_catalogue
from app.core.config import settings

log = logging.getLogger("fluxora.agents.graph")


# ---------------------------------------------------------------- planner contract

class PlanStep(BaseModel):
    tool: str = Field(description="Exact name of one of the available tools.")
    # A JSON string rather than a nested object: OpenAI's strict structured-output mode
    # rejects open-ended objects, and a string keeps one validation path through Pydantic.
    args_json: str = Field(
        default="{}", description="Arguments for the tool, as a JSON object string."
    )
    why: str = Field(default="", description="One short sentence on why this step is needed.")


class Plan(BaseModel):
    reasoning: str = Field(default="", description="Brief reasoning about the approach.")
    steps: list[PlanStep] = Field(default_factory=list)


PLANNER_SYSTEM = """You are the Fluxora video platform's operations agent.

You turn a user's request into a short plan of tool calls against their own video project.

Rules:
- Only use the tools listed. Never invent a tool or an argument.
- Prefer reading before writing: if you need an asset id and were not given one, plan a
  `list_assets` or `get_asset` step first.
- Keep plans minimal. Never plan more than {max_steps} steps.
- Tools marked risk `expensive` or `destructive` need human approval; include them only
  when the user clearly asked for that outcome.
- Never plan `delete_asset` unless the user explicitly asked to delete something.
- If the request cannot be done with these tools, return an empty plan and say why in
  `reasoning`.

Available tools:
{catalogue}
"""

RESPONDER_SYSTEM = """You are reporting the result of operations on the Fluxora video platform.

Write a short, factual summary for the user of what was done and what the results were.
Use the observations given. Do not invent ids, URLs, durations or statuses — if an
observation shows an error, say plainly what failed. Never include API keys, tokens or
connection strings. Keep it under 150 words."""


# ---------------------------------------------------------------- graph state

class AgentState(TypedDict, total=False):
    run_id: str
    project_id: str
    prompt: str
    phase: Literal["plan", "execute"]
    approved: bool

    plan: list[dict]
    reasoning: str
    requires_approval: bool
    policy_reasons: list[str]

    observations: Annotated[list[dict], lambda a, b: (a or []) + (b or [])]
    tool_calls: int
    answer: str
    status: str
    error: str | None


# ---------------------------------------------------------------- nodes

def _parse_args(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"__invalid_json__": raw[:200]}
    return parsed if isinstance(parsed, dict) else {"__not_an_object__": str(parsed)[:200]}


async def guard_input_node(state: AgentState) -> dict:
    try:
        guardrails.check_input(state["prompt"])
    except guardrails.GuardrailViolation as exc:
        return {
            "status": "blocked",
            "error": exc.reason,
            "answer": (
                "That request was blocked before it reached the model: "
                f"{_first_line(exc.reason)}"
            ),
        }
    return {"status": "planning"}


def _first_line(text: str) -> str:
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    return lines[-1] if lines else str(text)


async def plan_node(state: AgentState, llm=None) -> dict:
    catalogue = json.dumps(tool_catalogue(), indent=1)
    system = PLANNER_SYSTEM.format(
        max_steps=settings.agent_max_plan_steps, catalogue=catalogue
    )
    try:
        model = llm or build_llm()
    except LLMUnavailable as exc:
        return {"status": "failed", "error": str(exc), "answer": str(exc)}

    planner = model.with_structured_output(Plan)
    try:
        result = await planner.ainvoke(
            [("system", system), ("human", state["prompt"])],
            config={"run_name": "fluxora-plan", "metadata": {"run_id": state["run_id"]}},
        )
    except Exception as exc:
        log.exception("planner failed for run %s", state["run_id"])
        message = f"the planner could not produce a plan: {type(exc).__name__}"
        return {"status": "failed", "error": str(exc), "answer": message}

    plan = [
        {"tool": step.tool, "args": _parse_args(step.args_json), "why": step.why}
        for step in (result.steps or [])
    ]
    return {"plan": plan, "reasoning": result.reasoning or "", "status": "planned"}


async def policy_node(state: AgentState) -> dict:
    plan = state.get("plan") or []
    if not plan:
        return {
            "status": "completed",
            "requires_approval": False,
            "answer": state.get("reasoning")
            or "I could not find a way to do that with the tools available.",
        }

    decision = guardrails.check_plan(plan)
    if not decision.allowed:
        reasons = "; ".join(decision.reasons)
        return {
            "status": "blocked",
            "requires_approval": False,
            "policy_reasons": decision.reasons,
            "error": reasons,
            "answer": f"The proposed plan was rejected by policy: {reasons}",
        }

    if decision.requires_approval and not state.get("approved"):
        return {
            "status": "awaiting_approval",
            "requires_approval": True,
            "policy_reasons": decision.high_risk_steps,
            "answer": (
                "This plan includes steps that cost money or cannot be undone "
                f"({', '.join(decision.high_risk_steps)}). Approve it to continue."
            ),
        }

    return {"status": "approved", "requires_approval": decision.requires_approval}


async def execute_node(state: AgentState) -> dict:
    """Dispatch each step through BullMQ, in order, collecting observations."""
    bus = get_bus()
    observations: list[dict] = []
    calls = int(state.get("tool_calls") or 0)

    for index, step in enumerate(state.get("plan") or [], start=1):
        name = step["tool"]
        try:
            guardrails.check_tool_call(name, calls)
        except guardrails.GuardrailViolation as exc:
            observations.append({"step": index, "tool": name, "error": exc.reason})
            break

        result = await bus.call(
            tool=name,
            args=step.get("args") or {},
            project_id=state["project_id"],
            run_id=state["run_id"],
            step=index,
        )
        calls += 1
        observations.append(
            {
                "step": index,
                "tool": name,
                "risk": REGISTRY[name].risk.value,
                "job_id": result.job_id,
                "attempts": result.attempts,
                "ok": result.ok,
                "output": result.output,
                "error": result.error,
                "dead_lettered": result.dead_lettered,
            }
        )
        if not result.ok:
            # Stop on failure: later steps usually depend on earlier ones, and pressing
            # on would compound one bad result into several.
            log.warning(
                "run %s stopping at step %d (%s): %s", state["run_id"], index, name, result.error
            )
            break

    return {"observations": observations, "tool_calls": calls, "status": "executed"}


async def respond_node(state: AgentState, llm=None) -> dict:
    observations = state.get("observations") or []
    failed = [o for o in observations if not o.get("ok")]

    summary_input = json.dumps(
        {
            "request": state["prompt"],
            "plan": state.get("plan"),
            "observations": observations,
        },
        default=str,
    )[:12000]

    answer: str
    try:
        model = llm or build_llm()
        response = await model.ainvoke(
            [("system", RESPONDER_SYSTEM), ("human", summary_input)],
            config={"run_name": "fluxora-respond", "metadata": {"run_id": state["run_id"]}},
        )
        answer = getattr(response, "content", None) or str(response)
    except Exception as exc:
        # The work already happened; a failure to narrate it must not lose the result.
        log.warning("responder unavailable for run %s: %s", state["run_id"], exc)
        answer = _mechanical_summary(observations)

    if isinstance(answer, list):  # some providers return content blocks
        answer = " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in answer
        ).strip()

    try:
        answer = guardrails.check_output(answer)
    except guardrails.GuardrailViolation as exc:
        log.error("output guardrail rewrote run %s: %s", state["run_id"], exc.reason)
        answer = guardrails.redact(_mechanical_summary(observations))

    return {
        "answer": answer,
        "status": "failed" if failed and len(failed) == len(observations) else "completed",
    }


def _mechanical_summary(observations: list[dict]) -> str:
    if not observations:
        return "No operations were performed."
    lines = []
    for obs in observations:
        if obs.get("ok"):
            lines.append(f"{obs['tool']}: done")
        else:
            lines.append(f"{obs['tool']}: failed ({_first_line(obs.get('error') or 'unknown')})")
    return "; ".join(lines)


# ---------------------------------------------------------------- assembly

def _route_after_guard(state: AgentState) -> str:
    return "plan" if state.get("status") == "planning" else "__end__"


def _route_after_plan(state: AgentState) -> str:
    return "policy" if state.get("status") == "planned" else "__end__"


def _route_after_policy(state: AgentState) -> str:
    return "execute" if state.get("status") == "approved" else "__end__"


def _entry(state: AgentState) -> str:
    # A resumed run re-enters at execution with an already-screened, already-approved
    # plan; a fresh run starts at the input guardrail.
    return "execute" if state.get("phase") == "execute" else "guard_input"


def build_graph(llm=None):
    """Compile the agent graph. `llm` is injectable so tests can run it deterministically
    without a provider key."""
    from langgraph.graph import END, START, StateGraph

    configure_tracing()
    builder = StateGraph(AgentState)

    # Async closures, not `lambda s: plan_node(s, llm)`: LangGraph inspects the callable,
    # and a sync lambda that happens to return a coroutine is never awaited — the node
    # then "returns" a coroutine object and the graph rejects it.
    async def plan_with_llm(state: AgentState) -> dict:
        return await plan_node(state, llm)

    async def respond_with_llm(state: AgentState) -> dict:
        return await respond_node(state, llm)

    builder.add_node("guard_input", guard_input_node)
    builder.add_node("plan", plan_with_llm)
    builder.add_node("policy", policy_node)
    builder.add_node("execute", execute_node)
    builder.add_node("respond", respond_with_llm)

    builder.add_conditional_edges(
        START, _entry, {"guard_input": "guard_input", "execute": "execute"}
    )
    builder.add_conditional_edges(
        "guard_input", _route_after_guard, {"plan": "plan", "__end__": END}
    )
    builder.add_conditional_edges(
        "plan", _route_after_plan, {"policy": "policy", "__end__": END}
    )
    builder.add_conditional_edges(
        "policy", _route_after_policy, {"execute": "execute", "__end__": END}
    )
    builder.add_edge("execute", "respond")
    builder.add_edge("respond", END)

    return builder.compile()


_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


async def run_plan_phase(
    *, run_id: str, project_id: str, prompt: str, llm=None
) -> AgentState:
    graph = build_graph(llm) if llm is not None else get_graph()
    return await graph.ainvoke(
        {
            "run_id": run_id,
            "project_id": project_id,
            "prompt": prompt,
            "phase": "plan",
            "approved": False,
            "tool_calls": 0,
            "observations": [],
        },
        config={"run_name": "fluxora-agent", "metadata": {"run_id": run_id}},
    )


async def run_execute_phase(
    *, run_id: str, project_id: str, prompt: str, plan: list[dict], llm=None
) -> AgentState:
    """Resume an approved run. The plan comes from the persisted row, and is re-screened
    by `check_plan` on the way in — approval authorises a specific plan, so it must not be
    possible to swap a different one in afterwards."""
    decision = guardrails.check_plan(plan)
    if not decision.allowed:
        return {
            "run_id": run_id,
            "status": "blocked",
            "error": "; ".join(decision.reasons),
            "answer": "The stored plan is no longer valid and was not executed.",
            "observations": [],
        }

    graph = build_graph(llm) if llm is not None else get_graph()
    return await graph.ainvoke(
        {
            "run_id": run_id,
            "project_id": project_id,
            "prompt": prompt,
            "phase": "execute",
            "approved": True,
            "plan": plan,
            "tool_calls": 0,
            "observations": [],
        },
        config={"run_name": "fluxora-agent-resume", "metadata": {"run_id": run_id}},
    )
