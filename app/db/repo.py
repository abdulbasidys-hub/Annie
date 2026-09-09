"""The Firestore repository — every read and write in the application goes
through here.

This is the module that replaces SQLAlchemy sessions, ``select()`` statements
and foreign keys. Three structural differences from the Postgres design worth
knowing before editing this file:

**No joins.** A token's launchpad, creator and features are separate
documents; assembling a detail view means several round trips, done
concurrently with ``asyncio.gather`` where it matters. This is the real cost
of Firestore versus Postgres for this workload — accepted deliberately per
Build.md §72.5, the amendment that moved this project off Postgres.

**No server-side aggregation for the trend engine.** ``compare_proportions``
still runs in Python (:mod:`app.analysis.stats`, unchanged) — only the *data
fetching* changed. Cohort membership is a single ranged query on
``qualified_at``; feature counting then reads each cohort token's
``features`` subcollection and tallies in Python instead of a SQL
``GROUP BY``. This is more reads, not more roundtrip complexity, and at
this project's scale (a research dataset, not a firehose) that trade is fine.

**Provider telemetry is a live rollup, not an event log.** The original
``provider_events`` table recorded one row per HTTP call. Doing that in
Firestore means one write per call, which adds up fast under Firestore's
free-tier write quota for no benefit an operator actually reads day to day.
:meth:`FirestoreRepo.record_provider_event` instead updates one document per
provider with a self-resetting ~24h window (`window_started_at`) — same
numbers on System Health, a fraction of the writes. A full per-call audit log
can be added later (Build.md's "Picking up the unfinished work" pattern)
without changing this interface.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from google.cloud.firestore import AsyncClient, FieldFilter, Query

from app.db.base import doc_id_safe, from_doc, money_from_doc, money_to_doc, to_doc, utcnow
from app.db.models.discord import DiscordChannel
from app.db.models.people import PlatformUser
from app.db.models.pipeline import PipelineRun
from app.db.models.entities import Launchpad, Narrative
from app.db.models.intelligence import Anomaly
from app.db.models.ops import AuditLog, DataQuality, ProviderHealth, Setting, ToolCall
from app.db.models.research import (
    Conversation,
    Message,
    Report,
    ResearchHypothesis,
    ResearchNote,
    ResearchTask,
)

#: Latency samples kept per provider for p50/p95. A ring buffer, not a log —
#: bounded so the document never grows unbounded under high call volume.
_LATENCY_SAMPLE_CAP = 200
_HEALTH_WINDOW = timedelta(hours=24)


class FirestoreRepo:
    def __init__(self, db: AsyncClient) -> None:
        self.db = db

    # =========================================================================
    # Tokens, creators, trends and memories are NOT here any more
    # =========================================================================
    # Removed 2026-09-08. Every one of them was a per-item Firestore
    # collection, and together they were the entire cost problem:
    #
    #   tokens (+ features, milestones)  ->  app/memory/ledger.py `sightings`
    #   creators                         ->  the ledger's `creators` + `moves`
    #   trends (+ observations, history) ->  app/memory/signals.py `signals`
    #   memories, consolidation_runs     ->  markdown files under ANNIE_MEMORY_DIR
    #
    # The methods are deleted rather than deprecated on purpose. A working
    # code path back to a retired collection is how one gets quietly
    # reintroduced by a later change; an ImportError is a much better
    # conversation than a surprise on the next bill.

    # =========================================================================
    # Launchpads
    # =========================================================================

    def _launchpad_ref(self, slug: str):
        return self.db.collection("launchpads").document(doc_id_safe(slug))

    async def get_launchpad(self, slug: str) -> Launchpad | None:
        snap = await self._launchpad_ref(slug).get()
        if not snap.exists:
            return None
        return from_doc(Launchpad, snap.id, snap.to_dict() or {}, slug=snap.id)

    async def touch_launchpad_seen(
        self, slug: str, *, name: str | None, program_id: str | None, when: datetime
    ) -> None:
        """Get-or-create a launchpad row on first sighting (§5, §18).

        Never hardcoded — every launchpad this system knows about got here
        because discovery actually saw a launch attributed to it.
        """
        ref = self._launchpad_ref(slug)
        snap = await ref.get()
        if not snap.exists:
            launchpad = Launchpad(
                slug=slug,
                name=name or slug,
                program_ids=[program_id] if program_id else [],
                first_seen_at=when,
                last_seen_at=when,
                is_known=True,
                discovered_by="discovery",
                launch_count=1,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            await ref.set(to_doc(launchpad))
        else:
            existing = snap.to_dict() or {}
            await ref.set(
                {
                    "last_seen_at": when,
                    "launch_count": int(existing.get("launch_count") or 0) + 1,
                    "updated_at": utcnow(),
                },
                merge=True,
            )

    async def list_launchpads(self, *, limit: int = 50, offset: int = 0) -> tuple[list[Launchpad], int]:
        query = self.db.collection("launchpads").order_by(
            "launch_count", direction=Query.DESCENDING
        )
        all_docs = [s async for s in query.stream()]
        page = all_docs[offset : offset + limit]
        return [
            from_doc(Launchpad, s.id, s.to_dict() or {}, slug=s.id) for s in page
        ], len(all_docs)

    # =========================================================================
    # Narratives (§16) — collection defined; no pipeline stage populates it
    # yet in this deployment. See Narrative's docstring.
    # =========================================================================

    async def get_narrative(self, slug: str) -> Narrative | None:
        snap = await self.db.collection("narratives").document(doc_id_safe(slug)).get()
        if not snap.exists:
            return None
        return from_doc(Narrative, snap.id, snap.to_dict() or {}, slug=snap.id)

    async def upsert_narrative(self, narrative: Narrative) -> Narrative:
        """Preserves ``first_seen_at`` across runs — a narrative's first
        appearance shouldn't reset every time clustering re-runs and finds
        it again. Everything else (counts, shares, last_seen_at) is
        recomputed fresh each run, so ``merge=True`` here would leave stale
        numbers from a run where the narrative briefly had fewer/more
        matches; a full overwrite of the stats fields is correct."""
        ref = self.db.collection("narratives").document(doc_id_safe(narrative.slug))
        existing = await ref.get()
        if existing.exists:
            prior_first_seen = (existing.to_dict() or {}).get("first_seen_at")
            if prior_first_seen is not None:
                narrative.first_seen_at = prior_first_seen
        narrative.updated_at = utcnow()
        await ref.set(to_doc(narrative), merge=True)
        return narrative

    async def features_with_value(self, *, namespace: str, key: str, value: str) -> list[str]:
        """Every token mint carrying a specific TokenFeature — a Firestore
        collection-group query across every token's `features` subcollection
        at once, rather than fetching each qualified token's features
        one-by-one. Since deterministic features (app/analysis/features.py)
        are only ever written by Stage-3 enrichment, which only ever runs
        for tokens that already qualified, a match here is inherently a
        qualified token — no separate qualified-only filter is needed."""
        query = (
            self.db.collection_group("features")
            .where(filter=FieldFilter("namespace", "==", namespace))
            .where(filter=FieldFilter("key", "==", key))
            .where(filter=FieldFilter("value", "==", value))
        )
        return [s.to_dict().get("token_mint") async for s in query.stream() if s.to_dict()]

    async def list_narratives(self, *, limit: int = 50, offset: int = 0) -> tuple[list[Narrative], int]:
        query = self.db.collection("narratives").order_by(
            "share_of_qualified", direction=Query.DESCENDING
        )
        all_docs = [s async for s in query.stream()]
        page = all_docs[offset : offset + limit]
        return [
            from_doc(Narrative, s.id, s.to_dict() or {}, slug=s.id) for s in page
        ], len(all_docs)

    # =========================================================================
    # Anomalies (§30)
    # =========================================================================
    # Kept in Firestore deliberately, unlike trends: an anomaly is a small,
    # rare, operator-facing document that someone acknowledges by hand, so it
    # needs to be visible across processes and to survive a volume wipe. The
    # detector that writes them is the cycle, which produces a handful a week
    # rather than thousands a day.

    async def list_anomalies(
        self, *, unacknowledged_only: bool = False, limit: int = 50
    ) -> list[Anomaly]:
        query = self.db.collection("anomalies")
        if unacknowledged_only:
            query = query.where(filter=FieldFilter("acknowledged", "==", False))
        query = query.order_by("detected_at", direction="DESCENDING").limit(limit)
        return [
            from_doc(Anomaly, snap.id, snap.to_dict() or {})
            async for snap in query.stream()
        ]

    async def record_anomaly(self, anomaly: Anomaly) -> Anomaly:
        ref = self.db.collection("anomalies").document()
        anomaly.id = ref.id
        await ref.set(to_doc(anomaly))
        return anomaly

    async def acknowledge_anomaly(self, anomaly_id: str) -> None:
        await self.db.collection("anomalies").document(anomaly_id).set(
            {"acknowledged": True, "acknowledged_at": utcnow()}, merge=True
        )

    # =========================================================================
    # Data quality (§20, §50)
    # =========================================================================

    async def record_data_quality(self, dq: DataQuality) -> None:
        await self.db.collection("data_quality").document(dq.doc_id).set(to_doc(dq), merge=True)

    async def data_quality_since(self, start: datetime, end: datetime) -> list[DataQuality]:
        query = self.db.collection("data_quality").where(
            filter=FieldFilter("measured_on", ">=", start)
        ).where(filter=FieldFilter("measured_on", "<", end))
        return [
            from_doc(DataQuality, s.id, s.to_dict() or {}) async for s in query.stream()
        ]

    # =========================================================================
    # Research: tasks, notes, hypotheses, reports
    # =========================================================================

    async def create_research_task(self, task: ResearchTask) -> ResearchTask:
        ref = self.db.collection("research_tasks").document()
        task.id = ref.id
        task.created_at = utcnow()
        task.updated_at = utcnow()
        await ref.set(to_doc(task))
        return task

    async def get_research_task(self, task_id: str) -> ResearchTask | None:
        snap = await self.db.collection("research_tasks").document(task_id).get()
        if not snap.exists:
            return None
        return from_doc(ResearchTask, snap.id, snap.to_dict() or {}, id=snap.id)

    async def update_research_task(self, task_id: str, **updates: Any) -> None:
        # Generic merge-write, not routed through to_doc() (there's no
        # ResearchTask instance here, just field names) — so any Decimal
        # (cost_usd, max_cost_usd) is converted by hand here instead. Without
        # this, a raw Decimal would hit Firestore directly, which has no
        # native Decimal type — silently breaking the "money is an exact
        # string in Firestore" rule this codebase otherwise enforces
        # everywhere else (see app/db/base.py's module docstring).
        updates = {k: (money_to_doc(v) if isinstance(v, Decimal) else v) for k, v in updates.items()}
        updates["updated_at"] = utcnow()
        await self.db.collection("research_tasks").document(task_id).set(updates, merge=True)

    async def list_research_tasks(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[ResearchTask], int]:
        query = self.db.collection("research_tasks")
        if status:
            query = query.where(filter=FieldFilter("status", "==", status))
        query = query.order_by("created_at", direction=Query.DESCENDING)
        all_docs = [s async for s in query.stream()]
        page = all_docs[offset : offset + limit]
        return [
            from_doc(ResearchTask, s.id, s.to_dict() or {}, id=s.id) for s in page
        ], len(all_docs)

    async def create_research_note(self, note: ResearchNote) -> ResearchNote:
        ref = self.db.collection("research_notes").document()
        note.id = ref.id
        note.created_at = utcnow()
        note.updated_at = utcnow()
        await ref.set(to_doc(note))
        return note

    async def list_research_notes(
        self, *, current_only: bool = True, limit: int = 20
    ) -> list[ResearchNote]:
        query = self.db.collection("research_notes")
        if current_only:
            query = query.where(filter=FieldFilter("is_current", "==", True))
        query = query.order_by("created_at", direction=Query.DESCENDING).limit(limit)
        return [
            from_doc(ResearchNote, s.id, s.to_dict() or {}, id=s.id) async for s in query.stream()
        ]

    async def get_research_note(self, note_id: str) -> ResearchNote | None:
        snap = await self.db.collection("research_notes").document(note_id).get()
        if not snap.exists:
            return None
        return from_doc(ResearchNote, snap.id, snap.to_dict() or {}, id=snap.id)

    async def supersede_research_note(self, old_note_id: str, new_note_id: str) -> None:
        """Mark a note superseded by a newer one — the pattern
        ``superseded_by_id``/``is_current`` exist for, previously unused by
        any repo method. The old note is never deleted (§4/§49: evidence is
        never discarded), just no longer surfaced by
        ``list_research_notes(current_only=True)``."""
        await self.db.collection("research_notes").document(old_note_id).set(
            {"is_current": False, "superseded_by_id": new_note_id, "updated_at": utcnow()},
            merge=True,
        )

    async def upsert_hypothesis(self, hyp: ResearchHypothesis) -> None:
        hyp.updated_at = utcnow()
        await self.db.collection("research_hypotheses").document(doc_id_safe(hyp.slug)).set(
            to_doc(hyp), merge=True
        )

    async def list_hypotheses(self, limit: int = 50) -> list[ResearchHypothesis]:
        query = self.db.collection("research_hypotheses").limit(limit)
        return [
            from_doc(ResearchHypothesis, s.id, s.to_dict() or {}, slug=s.id)
            async for s in query.stream()
        ]

    # -- memory (Annie's work memory — distinct from research notes and chat) --

    async def upsert_report(self, report: Report) -> Report:
        report.id = report.doc_id
        await self.db.collection("reports").document(report.id).set(to_doc(report), merge=True)
        return report

    async def list_reports(self, *, kind: str | None = None, limit: int = 20) -> list[Report]:
        query = self.db.collection("reports")
        if kind:
            query = query.where(filter=FieldFilter("kind", "==", kind))
        query = query.order_by("period_end", direction=Query.DESCENDING).limit(limit)
        return [
            from_doc(Report, s.id, s.to_dict() or {}, id=s.id) async for s in query.stream()
        ]

    async def get_report(self, report_id: str) -> Report | None:
        snap = await self.db.collection("reports").document(report_id).get()
        if not snap.exists:
            return None
        return from_doc(Report, snap.id, snap.to_dict() or {}, id=snap.id)

    # =========================================================================
    # Annie: conversations, messages, tool calls
    # =========================================================================

    async def create_conversation(self) -> Conversation:
        ref = self.db.collection("conversations").document()
        convo = Conversation(id=ref.id, created_at=utcnow(), updated_at=utcnow())
        await ref.set(to_doc(convo))
        return convo

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        snap = await self.db.collection("conversations").document(conversation_id).get()
        if not snap.exists:
            return None
        return from_doc(Conversation, snap.id, snap.to_dict() or {}, id=snap.id)

    async def list_conversations(self, *, limit: int = 20) -> list[Conversation]:
        query = (
            self.db.collection("conversations")
            .where(filter=FieldFilter("archived", "==", False))
            .order_by("last_message_at", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [
            from_doc(Conversation, s.id, s.to_dict() or {}, id=s.id) async for s in query.stream()
        ]

    async def add_message(self, conversation_id: str, message: Message) -> Message:
        convo_ref = self.db.collection("conversations").document(conversation_id)
        msg_ref = convo_ref.collection("messages").document()
        message.id = msg_ref.id
        message.conversation_id = conversation_id
        message.created_at = utcnow()
        await msg_ref.set(to_doc(message))

        snap = await convo_ref.get()
        existing = snap.to_dict() or {}
        await convo_ref.set(
            {
                "message_count": int(existing.get("message_count") or 0) + 1,
                "last_message_at": message.created_at,
                "title": existing.get("title") or (message.content[:60] if message.role == "user" else None),
            },
            merge=True,
        )
        return message

    async def list_messages(self, conversation_id: str, limit: int = 200) -> list[Message]:
        query = (
            self.db.collection("conversations")
            .document(conversation_id)
            .collection("messages")
            .order_by("created_at", direction=Query.ASCENDING)
            .limit(limit)
        )
        return [
            from_doc(Message, s.id, s.to_dict() or {}, id=s.id, conversation_id=conversation_id)
            async for s in query.stream()
        ]

    async def record_tool_call(self, call: ToolCall) -> None:
        ref = self.db.collection("tool_calls").document()
        call.id = ref.id
        call.created_at = utcnow()
        await ref.set(to_doc(call))

    # =========================================================================
    # Settings & audit (§66, §67)
    # =========================================================================

    async def list_settings(self) -> list[Setting]:
        query = self.db.collection("settings").order_by("key")
        return [
            from_doc(Setting, s.id, s.to_dict() or {}, key=s.id) async for s in query.stream()
        ]

    async def get_setting(self, key: str) -> Setting | None:
        snap = await self.db.collection("settings").document(doc_id_safe(key)).get()
        if not snap.exists:
            return None
        return from_doc(Setting, snap.id, snap.to_dict() or {}, key=snap.id)

    async def upsert_setting(
        self, key: str, value: Any, *, description: str | None = None, actor: str = "operator",
        audit: bool = True,
    ) -> Setting:
        """``audit=False`` skips the audit_log write below — for routine,
        high-frequency bookkeeping (the scheduler's own last_run_at/
        last_result heartbeat, written on every tick of jobs that can fire
        every 10 minutes) rather than a meaningful operator- or Annie-driven
        change. Confirmed as a real, avoidable cost contributor 2026-08-28:
        audit logging exists for changes worth reviewing, not "a job ticked
        on schedule" — every scheduler heartbeat was writing a full
        audit_log document that nobody was ever going to read.
        """
        ref = self.db.collection("settings").document(doc_id_safe(key))
        before_snap = await ref.get()
        before_doc = before_snap.to_dict() or {}
        before = before_doc.get("value")
        # A plain value-only save (the Settings page's PATCH, or a job's
        # last_run_date update) must never blank out a description someone
        # set earlier — only overwrite it when the caller actually passed one.
        resolved_description = description if description is not None else before_doc.get("description")

        setting = Setting(
            key=key, value=value, description=resolved_description, updated_by=actor, updated_at=utcnow()
        )
        doc = to_doc(setting)
        # Field-path merge, not a bare `merge=True`: Firestore's boolean merge
        # recursively merges nested maps, so a smaller `value` dict (e.g. a
        # job's error result, {"error": "..."}) gets its missing keys
        # silently backfilled from whatever the *previous* write's `value`
        # happened to contain — confirmed 2026-08-25 against a real scheduled
        # job's last_result, which showed stale evaluated/qualified counts
        # from an earlier successful run stitched onto a later failed run's
        # error message. Listing the top-level paths explicitly makes each
        # one (value, description, ...) a full replacement instead.
        await ref.set(doc, merge=list(doc.keys()))

        if audit:
            await self.db.collection("audit_log").document().set(
                to_doc(
                    AuditLog(
                        actor=actor,
                        action="setting.update",
                        subject_type="setting",
                        subject_id=key,
                        before={"value": before},
                        after={"value": value},
                        created_at=utcnow(),
                    )
                )
            )
        return setting

    # =========================================================================
    # Provider health (§50) — live rollup, not an event log; see module docstring
    # =========================================================================

    async def record_provider_event(
        self,
        *,
        provider: str,
        event_type: str,
        latency_ms: int | None,
        estimated_cost_usd: float | None,
        error_message: str | None = None,
    ) -> None:
        ref = self.db.collection("provider_health").document(doc_id_safe(provider))
        now = utcnow()
        snap = await ref.get()
        existing = snap.to_dict() or {}

        window_started = existing.get("window_started_at")
        if window_started is None or (now - window_started) > _HEALTH_WINDOW:
            requests_24h = errors_24h = rate_limited_24h = 0
            latency_samples: list[int] = []
            window_started = now
        else:
            requests_24h = int(existing.get("requests_24h") or 0)
            errors_24h = int(existing.get("errors_24h") or 0)
            rate_limited_24h = int(existing.get("rate_limited_24h") or 0)
            latency_samples = list(existing.get("latency_samples_ms") or [])

        requests_24h += 1
        if event_type in ("error", "timeout"):
            errors_24h += 1
        if event_type == "rate_limited":
            rate_limited_24h += 1
        if latency_ms is not None:
            latency_samples.append(int(latency_ms))
            latency_samples = latency_samples[-_LATENCY_SAMPLE_CAP:]

        p50 = int(statistics.median(latency_samples)) if latency_samples else None
        p95 = int(_percentile(latency_samples, 0.95)) if latency_samples else None
        existing_cost = money_from_doc(existing.get("estimated_cost_24h_usd")) or Decimal(0)
        if estimated_cost_usd:
            existing_cost += Decimal(str(estimated_cost_usd))

        updates: dict[str, Any] = {
            "provider": provider,
            "status": "ok" if event_type == "success" else existing.get("status", "unknown"),
            "window_started_at": window_started,
            "requests_24h": requests_24h,
            "errors_24h": errors_24h,
            "rate_limited_24h": rate_limited_24h,
            "error_rate_24h": (errors_24h / requests_24h) if requests_24h else None,
            "latency_samples_ms": latency_samples,
            "p50_latency_ms": p50,
            "p95_latency_ms": p95,
            "estimated_cost_24h_usd": money_to_doc(existing_cost) if estimated_cost_usd else existing.get("estimated_cost_24h_usd"),
            "computed_at": now,
        }
        if event_type == "success":
            updates["last_success_at"] = now
        if event_type in ("error", "timeout", "rate_limited"):
            updates["last_error_at"] = now
            if error_message:
                updates["last_error_message"] = error_message[:500]

        await ref.set(updates, merge=True)

    async def list_provider_health(self) -> list[ProviderHealth]:
        query = self.db.collection("provider_health")
        return [
            from_doc(ProviderHealth, s.id, s.to_dict() or {}, provider=s.id)
            async for s in query.stream()
        ]

    # =========================================================================
    # Bot integrations (§62) — Telegram/Discord share this, keyed by provider
    # so the same shapes serve both without a third copy of this logic.
    # =========================================================================

    async def get_bot_offset(self, provider: str) -> int:
        """Telegram's long-poll cursor. Persisted so a redeploy doesn't
        reprocess (or miss) messages that arrived while the process was down."""
        snap = await self.db.collection("bot_state").document(provider).get()
        return int((snap.to_dict() or {}).get("offset") or 0)

    async def set_bot_offset(self, provider: str, offset: int) -> None:
        await self.db.collection("bot_state").document(provider).set(
            {"offset": offset}, merge=True
        )

    async def get_bot_session(
        self, provider: str, external_id: str, *, max_age: timedelta | None = None
    ) -> str | None:
        """The Annie conversation a Telegram chat / Discord channel maps to.

        ``max_age`` (§62, 2026-08-25 — "even when the conversation has
        shifted it repeats it"): a session persisted indefinitely with no
        expiry, so up to MAX_CONTEXT_MESSAGES of old history kept feeding
        into every new question no matter how much time had passed since
        the topic actually moved on. Passing a max_age treats a session
        that's gone quiet longer than that as expired — returns None (a
        fresh conversation) rather than the stale conversation_id, same
        effect as if the caller had never had one.
        """
        doc_id = doc_id_safe(f"{provider}_{external_id}")
        snap = await self.db.collection("bot_sessions").document(doc_id).get()
        data = snap.to_dict() or {}
        if max_age is not None:
            updated_at = data.get("updated_at")
            if updated_at is None or (utcnow() - updated_at) > max_age:
                return None
        return data.get("conversation_id")

    async def set_bot_session(self, provider: str, external_id: str, conversation_id: str) -> None:
        doc_id = doc_id_safe(f"{provider}_{external_id}")
        await self.db.collection("bot_sessions").document(doc_id).set(
            {
                "provider": provider, "external_id": external_id,
                "conversation_id": conversation_id, "updated_at": utcnow(),
            },
            merge=True,
        )

    async def clear_bot_session(self, provider: str, external_id: str) -> None:
        """Explicit reset (a `/new` command) — the reliable complement to
        max_age above for "the topic just shifted, right now" rather than
        "enough time has passed that it probably did"."""
        doc_id = doc_id_safe(f"{provider}_{external_id}")
        await self.db.collection("bot_sessions").document(doc_id).delete()



    # -- Discord workspace channels ------------------------------------------

    async def create_discord_channel(self, channel: DiscordChannel) -> DiscordChannel:
        channel.created_at = utcnow()
        channel.updated_at = utcnow()
        await self.db.collection("discord_channels").document(channel.channel_id).set(to_doc(channel))
        return channel

    async def get_discord_channel(self, channel_id: str) -> DiscordChannel | None:
        snap = await self.db.collection("discord_channels").document(channel_id).get()
        if not snap.exists:
            return None
        return from_doc(DiscordChannel, snap.id, snap.to_dict() or {}, channel_id=snap.id)

    async def get_discord_channel_by_purpose(self, purpose: str, *, guild_id: str | None = None) -> DiscordChannel | None:
        """First enabled channel configured for a purpose — used to find
        where the Morning Brief (or any other purpose-routed content)
        should be delivered without hardcoding a channel ID anywhere."""
        query = self.db.collection("discord_channels").where(
            filter=FieldFilter("purpose", "==", purpose)
        ).where(filter=FieldFilter("enabled", "==", True))
        async for snap in query.stream():
            data = snap.to_dict() or {}
            if guild_id and data.get("guild_id") != guild_id:
                continue
            return from_doc(DiscordChannel, snap.id, data, channel_id=snap.id)
        return None

    async def list_discord_channels(self, *, guild_id: str | None = None) -> list[DiscordChannel]:
        query = self.db.collection("discord_channels")
        if guild_id:
            query = query.where(filter=FieldFilter("guild_id", "==", guild_id))
        return [
            from_doc(DiscordChannel, s.id, s.to_dict() or {}, channel_id=s.id)
            async for s in query.stream()
        ]

    async def update_discord_channel(self, channel_id: str, **updates: Any) -> None:
        updates["updated_at"] = utcnow()
        await self.db.collection("discord_channels").document(channel_id).set(updates, merge=True)

    # -- platform users (§62) --------------------------------------------------

    async def get_platform_user(self, platform: str, user_id: str) -> PlatformUser | None:
        doc_id = f"{platform}_{user_id}"
        snap = await self.db.collection("platform_users").document(doc_id).get()
        if not snap.exists:
            return None
        return from_doc(PlatformUser, snap.id, snap.to_dict() or {})

    async def touch_platform_user(
        self, platform: str, user_id: str, *, display_name: str | None, is_bot: bool
    ) -> PlatformUser:
        """Called on every incoming message — creates the profile on first
        contact, bumps last_seen_at/message_count otherwise. Never touches
        ``preferred_name``: that only ever comes from
        :func:`app.annie.agent._tool_remember_person`, once Annie has
        actually asked and been told, not from a platform's own display name.
        """
        doc_id = f"{platform}_{user_id}"
        ref = self.db.collection("platform_users").document(doc_id)
        snap = await ref.get()
        now = utcnow()
        if not snap.exists:
            user = PlatformUser(
                platform=platform, user_id=str(user_id), platform_display_name=display_name,
                is_bot=is_bot, first_seen_at=now, last_seen_at=now, message_count=1,
            )
            await ref.set(to_doc(user))
            return user

        existing = snap.to_dict() or {}
        updates: dict[str, Any] = {
            "last_seen_at": now,
            "message_count": int(existing.get("message_count") or 0) + 1,
        }
        if display_name:
            updates["platform_display_name"] = display_name
        await ref.set(updates, merge=list(updates.keys()))
        existing.update(updates)
        return from_doc(PlatformUser, doc_id, existing)

    async def set_preferred_name(self, platform: str, user_id: str, name: str) -> None:
        doc_id = f"{platform}_{user_id}"
        await self.db.collection("platform_users").document(doc_id).set(
            {"preferred_name": name}, merge=["preferred_name"]
        )

    # -- pipeline runs (§20, §2026-08-25) --------------------------------------

    async def create_pipeline_run(self, stage: str, *, trigger: str = "manual") -> PipelineRun:
        run = PipelineRun(stage=stage, trigger=trigger, status="running", started_at=utcnow())
        ref = self.db.collection("pipeline_runs").document()
        run.id = ref.id
        await ref.set(to_doc(run))
        return run

    async def finish_pipeline_run(
        self, run_id: str, *, status: str, result: dict[str, Any] | None = None, error: str | None = None
    ) -> None:
        updates = {"status": status, "result": result or {}, "error": error, "finished_at": utcnow()}
        await self.db.collection("pipeline_runs").document(run_id).set(updates, merge=list(updates.keys()))

    async def get_pipeline_run(self, run_id: str) -> PipelineRun | None:
        snap = await self.db.collection("pipeline_runs").document(run_id).get()
        if not snap.exists:
            return None
        return from_doc(PipelineRun, snap.id, snap.to_dict() or {}, id=snap.id)

    async def list_pipeline_runs(self, *, stage: str | None = None, limit: int = 20) -> list[PipelineRun]:
        query = self.db.collection("pipeline_runs")
        if stage:
            query = query.where(filter=FieldFilter("stage", "==", stage))
        query = query.order_by("started_at", direction=Query.DESCENDING).limit(limit)
        return [from_doc(PipelineRun, s.id, s.to_dict() or {}, id=s.id) async for s in query.stream()]


async def get_repo() -> "FirestoreRepo":
    """FastAPI dependency — ``Depends(get_repo)`` in place of the old
    ``Depends(get_session)``."""
    from app.db.firestore import get_client

    return FirestoreRepo(get_client())


def _percentile(values: list[int], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * p
    f, c = int(k), min(int(k) + 1, len(ordered) - 1)
    if f == c:
        return float(ordered[f])
    return ordered[f] + (ordered[c] - ordered[f]) * (k - f)
