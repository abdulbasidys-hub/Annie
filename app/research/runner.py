"""Executes queued ResearchTasks end-to-end (§35-§36) — the piece that was
missing behind "Research Memory": tasks could be created but nothing ever
worked one. See README's former "Autonomous research task runner — Not
written" status line, and `app/db/models/research.py`'s module docstring
for how a completed task's ``ResearchNote`` relates to the newer, broader
``Memory`` model.

Reuses :class:`app.annie.agent.AnnieAgent`'s tool machinery directly — same
tool set (``search_tokens``, ``get_token``, ``live_token_lookup``,
``list_trends``, etc.), same per-call logging via
``FirestoreRepo.record_tool_call``, same "tools only ever read" guarantee —
rather than a second, drifting implementation of the same tools. What
differs from a chat turn is the loop's stopping condition (a task's own
budget — ``ResearchTask.budget_exhausted`` — not a fixed round count) and
what happens with the final answer (a ``ResearchNote``, not a chat message).

Two triggers call :func:`run_research_task`, both landing here:

* Immediately, fire-and-forget, the moment a task is created
  (``app/api/routes/intelligence.py``'s ``create_task``) — so a user who
  just asked a question doesn't wait for a daily cycle.
* A daily scheduled sweep (``app/scheduling/jobs.py``) that picks up any
  task still ``queued`` — catches a task orphaned by a process restart
  between creation and its fire-and-forget task actually running.

Both call the same function, so behavior never drifts between "ran right
away" and "ran because nothing else picked it up."
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from app.annie import persona
from app.annie.agent import AnnieAgent, _capabilities_note, _personality_overrides, _tool_specs
from app.config import Settings
from app.db.enums import ResearchTaskStatus
from app.db.models.ops import ToolCall
from app.db.models.research import ResearchNote
from app.db.repo import FirestoreRepo
from app.providers.openai_provider import estimate_cost
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

#: A research task is meant to dig deeper than one chat turn — more rounds
#: than AnnieAgent.MAX_TOOL_ROUNDS (6) — but still bounded by the task's own
#: max_iterations regardless, so this is a ceiling on the ceiling, not the
#: real limit.
HARD_ROUND_CAP = 12


async def run_research_task(
    task_id: str, *, repo: FirestoreRepo, registry: ProviderRegistry, settings: Settings
) -> None:
    """Run one task to completion (or failure). Never raises — a task that
    errors is recorded as ``failed`` with a reason, not lost."""
    task = await repo.get_research_task(task_id)
    if task is None:
        log.warning("research_task_missing", task_id=task_id)
        return
    if task.status != ResearchTaskStatus.QUEUED:
        # Already picked up (by the fire-and-forget trigger, if this call
        # came from the sweep) or already finished — never double-run.
        return

    await repo.update_research_task(
        task_id, status=ResearchTaskStatus.RESEARCHING, started_at=datetime.now(timezone.utc)
    )
    started = time.perf_counter()

    try:
        result = await _execute(task_id, repo=repo, registry=registry, settings=settings)
    except Exception as exc:
        log.error("research_task_failed", task_id=task_id, error=str(exc), exc_info=True)
        await repo.update_research_task(
            task_id,
            status=ResearchTaskStatus.FAILED,
            failure_reason=str(exc)[:500],
            completed_at=datetime.now(timezone.utc),
        )
        return

    latency_ms = int((time.perf_counter() - started) * 1000)
    log.info("research_task_complete", task_id=task_id, latency_ms=latency_ms, **result["counts"])


async def _execute(
    task_id: str, *, repo: FirestoreRepo, registry: ProviderRegistry, settings: Settings
) -> dict[str, Any]:
    task = await repo.get_research_task(task_id)
    agent = AnnieAgent(repo=repo, registry=registry, settings=settings)
    client = await registry.reasoning.raw_client()
    model = settings.openai_reasoning_model

    prior_notes = await repo.list_research_notes(current_only=True, limit=5)
    brief = _task_brief(task, prior_notes)

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": persona.system_prompt(
                autonomous=True,
                capabilities_note=_capabilities_note(settings),
                personality_overrides=await _personality_overrides(repo),
            ),
        },
        {"role": "user", "content": brief},
    ]
    tools = _tool_specs(settings)

    iterations = 0
    tool_calls_used = 0
    total_input = total_output = 0
    total_cost = Decimal("0")

    round_cap = min(HARD_ROUND_CAP, max(1, task.max_iterations))
    for _round in range(round_cap):
        if task.budget_exhausted:
            break
        iterations += 1

        response = await client.chat.completions.create(
            model=model, messages=messages, tools=tools, tool_choice="auto",
            temperature=0.2, max_completion_tokens=1200, reasoning_effort="none",
        )
        usage = getattr(response, "usage", None)
        total_input += getattr(usage, "prompt_tokens", 0) or 0
        total_output += getattr(usage, "completion_tokens", 0) or 0

        choice = response.choices[0]
        calls = choice.message.tool_calls or []
        if not calls:
            if choice.message.content:
                messages.append({"role": "assistant", "content": choice.message.content})
            break

        messages.append(
            {
                "role": "assistant",
                "content": choice.message.content,
                "tool_calls": [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.function.name, "arguments": c.function.arguments}}
                    for c in calls
                ],
            }
        )
        for call in calls:
            if tool_calls_used >= task.max_tool_calls:
                messages.append(
                    {"role": "tool", "tool_call_id": call.id,
                     "content": json.dumps({"error": "tool call budget exhausted for this task"})}
                )
                continue
            result, succeeded, error = await agent._run_tool(call.function.name, call.function.arguments)
            tool_calls_used += 1
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": json.dumps(result, default=str)}
            )
            await repo.record_tool_call(
                ToolCall(
                    tool=call.function.name,
                    arguments=_safe_args(call.function.arguments),
                    succeeded=succeeded,
                    error_message=error,
                    research_task_id=task_id,
                )
            )

        total_cost = estimate_cost(model, total_input, total_output) or Decimal("0")
        await repo.update_research_task(
            task_id, iterations_used=iterations, tool_calls_used=tool_calls_used, cost_usd=total_cost
        )
        task = await repo.get_research_task(task_id)  # refresh for budget_exhausted check

    final = await agent._finish(client, model, messages)
    total_input += final["input_tokens"]
    total_output += final["output_tokens"]
    total_cost = estimate_cost(model, total_input, total_output) or Decimal("0")

    note = await repo.create_research_note(
        ResearchNote(
            title=(task.question or "Research finding")[:120],
            body=final["content"],
            claim_type=final["claim_type"],
            confidence=final["confidence"],
            evidence={"citations": final["citations"]},
            task_id=task_id,
        )
    )

    await repo.update_research_task(
        task_id,
        status=ResearchTaskStatus.COMPLETED,
        result=final["content"],
        result_claim_type=final["claim_type"],
        confidence=final["confidence"],
        iterations_used=iterations,
        tool_calls_used=tool_calls_used,
        cost_usd=total_cost,
        completed_at=datetime.now(timezone.utc),
    )

    return {
        "note_id": note.id,
        "counts": {"iterations": iterations, "tool_calls": tool_calls_used, "cost_usd": str(total_cost)},
    }


def _task_brief(task: Any, prior_notes: list[ResearchNote]) -> str:
    lines = [f"Research question: {task.question}"]
    if task.reason:
        lines.append(f"Why this was queued: {task.reason}")
    if task.goal:
        lines.append(f"Goal: {task.goal}")
    if task.completion_condition:
        lines.append(f"You're done when: {task.completion_condition}")
    if prior_notes:
        lines.append("\nRelevant prior findings (Research Memory) — check these before re-deriving:")
        for n in prior_notes[:5]:
            lines.append(f"- [{n.claim_type}/{n.confidence}] {n.title}: {n.body[:200]}")
    lines.append(
        "\nInvestigate using the tools available. When you have enough evidence "
        "(or have exhausted what's checkable), give your final answer — this "
        "becomes a permanent research note, so ground every claim in what the "
        "tools actually returned this run."
    )
    return "\n".join(lines)


def _safe_args(raw: str) -> dict[str, Any]:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"_raw": raw[:200]}
