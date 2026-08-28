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

import asyncio
import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from google.cloud.firestore import AsyncClient, DocumentSnapshot, FieldFilter, Query

from app.db.base import doc_id_safe, from_doc, money_from_doc, money_to_doc, slugify, to_doc, utcnow
from app.db.enums import MilestoneKind, PipelineStage
from app.db.models.discord import DiscordChannel
from app.db.models.people import PlatformUser
from app.db.models.pipeline import PipelineRun
from app.db.models.entities import Creator, Dex, Launchpad, Narrative
from app.db.models.intelligence import Anomaly, Trend, TrendHistory, TrendObservation
from app.db.models.ops import AuditLog, DataQuality, ProviderHealth, Setting, ToolCall
from app.db.models.research import (
    ConsolidationRun,
    Conversation,
    Memory,
    Message,
    PersonalityConfig,
    Report,
    ResearchHypothesis,
    ResearchNote,
    ResearchTask,
)
from app.db.models.tokens import Token, TokenFeature, TokenMilestone

#: Latency samples kept per provider for p50/p95. A ring buffer, not a log —
#: bounded so the document never grows unbounded under high call volume.
_LATENCY_SAMPLE_CAP = 200
_HEALTH_WINDOW = timedelta(hours=24)


class FirestoreRepo:
    def __init__(self, db: AsyncClient) -> None:
        self.db = db

    # =========================================================================
    # Tokens
    # =========================================================================

    def _token_ref(self, mint: str):
        return self.db.collection("tokens").document(doc_id_safe(mint))

    async def get_token(self, mint: str) -> Token | None:
        snap = await self._token_ref(mint).get()
        if not snap.exists:
            return None
        return from_doc(Token, snap.id, snap.to_dict() or {}, mint=snap.id)

    async def token_exists(self, mint: str) -> bool:
        snap = await self._token_ref(mint).get()
        return snap.exists

    async def create_discovered_token(self, token: Token) -> bool:
        """Insert a Stage-1 discovery record if this mint is not already known.

        Returns ``False`` without writing when the token exists — discovery
        must never clobber enrichment or qualification state that arrived
        since the last scan.
        """
        ref = self._token_ref(token.mint)
        snap = await ref.get()
        if snap.exists:
            return False
        token.created_at = token.created_at or utcnow()
        token.updated_at = utcnow()
        await ref.set(to_doc(token))
        return True

    async def apply_qualification(self, mint: str, verdict: Any) -> None:
        """Persist a :class:`~app.pipeline.qualification.QualificationVerdict`."""
        ref = self._token_ref(mint)
        snap = await ref.get()
        existing = snap.to_dict() or {}

        updates: dict[str, Any] = {
            "verification_status": verdict.verification_status,
            "qualification_evidence": verdict.as_evidence(),
            "pipeline_stage": existing.get("pipeline_stage") or PipelineStage.QUALIFICATION,
            "updated_at": utcnow(),
        }
        if verdict.market_cap is not None:
            updates["latest_market_cap"] = money_to_doc(verdict.market_cap)
            updates["market_data_at"] = verdict.evaluated_at

        if verdict.qualified:
            if not existing.get("is_qualified"):
                updates.update(
                    {
                        "is_qualified": True,
                        "qualified_at": verdict.evaluated_at,
                        "qualified_market_cap": money_to_doc(verdict.market_cap),
                        "qualified_threshold": money_to_doc(verdict.tier_reached),
                        "qualification_rule": verdict.rule_version,
                        "pipeline_stage": PipelineStage.QUALIFICATION,
                    }
                )
            peak = money_from_doc(existing.get("peak_market_cap"))
            if peak is None or (verdict.market_cap is not None and verdict.market_cap > peak):
                updates["peak_market_cap"] = money_to_doc(verdict.market_cap)
                updates["peak_at"] = verdict.evaluated_at
                updates["peak_tier"] = money_to_doc(verdict.tier_reached)

        await ref.set(updates, merge=True)

        if verdict.qualified and verdict.tier_reached is not None:
            milestone = TokenMilestone(
                token_mint=mint,
                kind=MilestoneKind.MARKET_CAP,
                threshold_usd=verdict.tier_reached,
                reached_at=verdict.evaluated_at,
                market_cap=verdict.market_cap,
                verification_status=verdict.verification_status,
                created_at=utcnow(),
            )
            await ref.collection("milestones").document(milestone.doc_id).set(
                to_doc(milestone), merge=True
            )

    async def apply_enrichment(
        self,
        mint: str,
        *,
        metadata: Any | None,
        creator_wallet: str | None,
        features: list[Any],
    ) -> None:
        """Persist Stage-3 enrichment: metadata, creator link, deterministic features."""
        ref = self._token_ref(mint)
        updates: dict[str, Any] = {
            "pipeline_stage": PipelineStage.ANALYSIS,
            "enriched_at": utcnow(),
            "analyzed_at": utcnow(),
            "updated_at": utcnow(),
        }
        if metadata is not None:
            updates.update(
                {
                    "name": metadata.name,
                    "symbol": metadata.symbol,
                    "description": metadata.description,
                    "image_url": metadata.image_url,
                    "decimals": metadata.decimals,
                    "total_supply": money_to_doc(metadata.total_supply),
                    "website": metadata.website,
                    "twitter": metadata.twitter,
                    "telegram": metadata.telegram,
                    "other_links": metadata.other_links,
                }
            )
        if creator_wallet:
            updates["creator_wallet"] = creator_wallet
        await ref.set(updates, merge=True)

        if features:
            batch = self.db.batch()
            features_col = ref.collection("features")
            for feat in features:
                tf = TokenFeature(
                    token_mint=mint,
                    namespace=feat.namespace,
                    key=feat.key,
                    value=feat.value,
                    numeric_value=feat.numeric_value,
                    source=feat.source,
                    subject=f"{feat.namespace}.{feat.key}",
                    created_at=utcnow(),
                )
                batch.set(features_col.document(tf.doc_id), to_doc(tf), merge=True)
            await batch.commit()

        if creator_wallet:
            await self.record_creator_launch(
                wallet=creator_wallet, mint=mint, launchpad_slug=None, launched_at=None
            )

    async def list_tokens_for_qualification(
        self, limit: int = 50, *, start_after: Any | None = None
    ) -> tuple[list[Token], Any | None]:
        """Newest-discovered tokens first.

        A brand-new token is far more likely to still be mid-pump than one
        that has sat unqualified for days — the previous unordered query left
        scan order to Firestore's discretion, so a backlog drain could burn
        its entire budget on old, already-dead tokens while the day's actual
        pumps went unchecked (confirmed empirically 2026-08-25: 16,602 of
        16,774 discovered tokens had *never* been evaluated at all). Returns
        the last document alongside the page so a multi-batch drain
        (:func:`app.pipeline.enrichment.run_enrichment_all`) can page forward
        with ``start_after`` instead of re-fetching the same newest N every
        call.
        """
        query = (
            self.db.collection("tokens")
            .where(filter=FieldFilter("pipeline_stage", "==", PipelineStage.DISCOVERY))
            .order_by("created_at", direction=Query.DESCENDING)
            .limit(limit)
        )
        if start_after is not None:
            query = query.start_after(start_after)
        docs = [s async for s in query.stream()]
        tokens = [from_doc(Token, s.id, s.to_dict() or {}, mint=s.id) for s in docs]
        return tokens, (docs[-1] if docs else None)

    async def list_tokens(
        self,
        *,
        qualified_only: bool = False,
        launchpad_slug: str | None = None,
        creator_wallet: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Token], int]:
        """List one page of tokens, plus the total matching count.

        Pushes ``limit``/``offset`` into the Firestore query itself and uses
        a server-side ``count()`` aggregation for the total, rather than
        streaming every matching document just to slice it in Python and
        call ``len()`` — the previous approach downloaded the *entire*
        collection on every call regardless of ``limit``, including every
        unfiltered call (``qualified_only=False``, the default — used by the
        Dashboard endpoint on every page load and Annie's own
        ``dashboard_summary`` tool). Confirmed as a real, worsening
        performance bug 2026-08-25: with ~17,300 tokens and climbing by
        roughly 16,000/day, every such call was transferring the full
        collection (qualification evidence, metadata, everything) over the
        wire just to report a single number.
        """
        base = self.db.collection("tokens")
        if qualified_only:
            base = base.where(filter=FieldFilter("is_qualified", "==", True))
        if launchpad_slug:
            base = base.where(filter=FieldFilter("launchpad_slug", "==", launchpad_slug))
        if creator_wallet:
            base = base.where(filter=FieldFilter("creator_wallet", "==", creator_wallet))
        base = base.order_by("created_at", direction=Query.DESCENDING)

        count_result = await base.count().get()
        total = int(count_result[0][0].value) if count_result and count_result[0] else 0

        page_query = base.limit(limit)
        if offset:
            page_query = page_query.offset(offset)
        docs = [s async for s in page_query.stream()]
        tokens = [from_doc(Token, s.id, s.to_dict() or {}, mint=s.id) for s in docs]
        return tokens, total

    async def qualified_tokens_in_window(
        self, *, start: datetime, end: datetime, min_peak: Decimal | None = None
    ) -> list[Token]:
        """Cohort membership for the trend engine (§27).

        A single range query on ``qualified_at``; the tier floor is applied in
        Python rather than as a second Firestore inequality filter, which
        keeps this query needing only the automatic single-field index instead
        of a hand-declared composite one.
        """
        query = (
            self.db.collection("tokens")
            .where(filter=FieldFilter("is_qualified", "==", True))
            .where(filter=FieldFilter("qualified_at", ">=", start))
            .where(filter=FieldFilter("qualified_at", "<", end))
        )
        tokens = [
            from_doc(Token, s.id, s.to_dict() or {}, mint=s.id) async for s in query.stream()
        ]
        if min_peak is not None:
            tokens = [t for t in tokens if t.peak_market_cap is not None and t.peak_market_cap >= min_peak]
        return tokens

    async def token_features(self, mint: str) -> list[TokenFeature]:
        col = self._token_ref(mint).collection("features")
        return [
            from_doc(TokenFeature, s.id, s.to_dict() or {}, token_mint=mint)
            async for s in col.stream()
        ]

    async def token_features_for_subjects(self, mint: str, subjects: list[str]) -> list[TokenFeature]:
        """Only the feature docs matching one of ``subjects`` (each a
        ``"namespace.key"`` string) — used by the trend engine, which only
        ever needs the handful of TRENDABLE fields, not a token's entire
        features subcollection (13-47 docs, most of it structural fields
        like word_count/has_emoji trends never look at, plus one document
        per individual word before that was removed 2026-08-28). Firestore's
        `in` filter caps at 30 values; TRENDABLE has far fewer than that."""
        if not subjects:
            return []
        col = self._token_ref(mint).collection("features")
        query = col.where(filter=FieldFilter("subject", "in", subjects))
        return [
            from_doc(TokenFeature, s.id, s.to_dict() or {}, token_mint=mint)
            async for s in query.stream()
        ]

    async def token_milestones(self, mint: str) -> list[TokenMilestone]:
        col = self._token_ref(mint).collection("milestones")
        return [
            from_doc(TokenMilestone, s.id, s.to_dict() or {}, token_mint=mint)
            async for s in col.stream()
        ]

    # =========================================================================
    # Creators
    # =========================================================================

    def _creator_ref(self, wallet: str):
        return self.db.collection("creators").document(doc_id_safe(wallet))

    async def get_creator(self, wallet: str) -> Creator | None:
        snap = await self._creator_ref(wallet).get()
        if not snap.exists:
            return None
        return from_doc(Creator, snap.id, snap.to_dict() or {}, wallet=snap.id)

    async def record_creator_launch(
        self,
        *,
        wallet: str,
        mint: str,
        launchpad_slug: str | None,
        launched_at: datetime | None,
    ) -> None:
        """Get-or-create a creator and log one launch against them (§17).

        A read-modify-write rather than an atomic increment because
        ``is_repeat_winner``/``success_rate`` depend on the qualification
        state of *other* tokens by this wallet, which this call does not have
        — recomputing those is the trend/stat job's responsibility, not
        discovery's.
        """
        ref = self._creator_ref(wallet)
        snap = await ref.get()
        now = utcnow()
        if not snap.exists:
            creator = Creator(
                wallet=wallet,
                first_launch_at=launched_at or now,
                last_launch_at=launched_at or now,
                total_launches=1,
                created_at=now,
                updated_at=now,
            )
            await ref.set(to_doc(creator))
        else:
            existing = snap.to_dict() or {}
            await ref.set(
                {
                    "total_launches": int(existing.get("total_launches") or 0) + 1,
                    "last_launch_at": launched_at or now,
                    "updated_at": now,
                },
                merge=True,
            )

    async def list_creators(self, *, limit: int = 50, offset: int = 0) -> tuple[list[Creator], int]:
        query = self.db.collection("creators").order_by(
            "wins_1m", direction=Query.DESCENDING
        )
        all_docs = [s async for s in query.stream()]
        page = all_docs[offset : offset + limit]
        return [
            from_doc(Creator, s.id, s.to_dict() or {}, wallet=s.id) for s in page
        ], len(all_docs)

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
    # Trends
    # =========================================================================

    def _trend_ref(self, slug: str):
        return self.db.collection("trends").document(doc_id_safe(slug))

    async def get_trend(self, slug: str) -> Trend | None:
        snap = await self._trend_ref(slug).get()
        if not snap.exists:
            return None
        return from_doc(Trend, snap.id, snap.to_dict() or {}, slug=snap.id)

    async def upsert_trend(self, trend: Trend) -> None:
        trend.updated_at = utcnow()
        await self._trend_ref(trend.slug).set(to_doc(trend), merge=True)

    async def list_trends(
        self, *, status: str | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[Trend], int]:
        query = self.db.collection("trends")
        if status:
            query = query.where(filter=FieldFilter("status", "==", status))
        query = query.order_by("last_observed_at", direction=Query.DESCENDING)
        all_docs = [s async for s in query.stream()]
        page = all_docs[offset : offset + limit]
        return [
            from_doc(Trend, s.id, s.to_dict() or {}, slug=s.id) for s in page
        ], len(all_docs)

    async def trends_for_tier(self, tier: Decimal) -> list[Trend]:
        """All trends measured over one cohort threshold, any status.

        Used by the decay pass, which needs every non-dead trend for a tier
        regardless of whether it occurred this window — that is precisely
        what tells the engine a characteristic *stopped* occurring.
        """
        query = self.db.collection("trends").where(
            filter=FieldFilter("cohort_threshold_usd", "==", money_to_doc(tier))
        )
        return [
            from_doc(Trend, s.id, s.to_dict() or {}, slug=s.id) async for s in query.stream()
        ]

    async def record_trend_observation(self, obs: TrendObservation) -> None:
        ref = self._trend_ref(obs.trend_slug).collection("observations").document(obs.doc_id)
        await ref.set(to_doc(obs), merge=True)

    async def trend_observations(self, slug: str, limit: int = 30) -> list[TrendObservation]:
        query = (
            self._trend_ref(slug)
            .collection("observations")
            .order_by("observed_on", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [
            from_doc(TrendObservation, s.id, s.to_dict() or {}, trend_slug=slug)
            async for s in query.stream()
        ]

    async def all_trends(self) -> list[Trend]:
        """Every trend, unpaginated — for dashboard-scale aggregates.

        At this project's scale (a research dataset's trend count, not a
        firehose) fetching everything and aggregating in Python is simpler
        and cheaper than maintaining separate counter documents that could
        drift from the underlying trend rows.
        """
        return [
            from_doc(Trend, s.id, s.to_dict() or {}, slug=s.id)
            async for s in self.db.collection("trends").stream()
        ]

    async def list_anomalies(
        self, *, unacknowledged_only: bool = False, limit: int = 50
    ) -> list[Anomaly]:
        query = self.db.collection("anomalies")
        if unacknowledged_only:
            query = query.where(filter=FieldFilter("acknowledged", "==", False))
        query = query.order_by("detected_at", direction=Query.DESCENDING).limit(limit)
        return [
            from_doc(Anomaly, s.id, s.to_dict() or {}, id=s.id) async for s in query.stream()
        ]

    async def record_trend_history(self, entry: TrendHistory) -> None:
        await self._trend_ref(entry.trend_slug).collection("history").document().set(
            to_doc(entry)
        )

    async def trend_history(self, slug: str, limit: int = 50) -> list[TrendHistory]:
        query = (
            self._trend_ref(slug)
            .collection("history")
            .order_by("changed_at", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [
            from_doc(TrendHistory, s.id, s.to_dict() or {}, trend_slug=slug)
            async for s in query.stream()
        ]

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

    async def create_memory(self, memory: Memory) -> Memory:
        ref = self.db.collection("memories").document()
        memory.id = ref.id
        memory.created_at = utcnow()
        memory.updated_at = utcnow()
        await ref.set(to_doc(memory))
        return memory

    async def get_memory(self, memory_id: str) -> Memory | None:
        snap = await self.db.collection("memories").document(memory_id).get()
        if not snap.exists:
            return None
        return from_doc(Memory, snap.id, snap.to_dict() or {}, id=snap.id)

    async def update_memory(self, memory_id: str, **updates: Any) -> None:
        updates["updated_at"] = utcnow()
        await self.db.collection("memories").document(memory_id).set(updates, merge=True)

    async def delete_memory(self, memory_id: str) -> None:
        """Genuine deletion — see app/api/routes/memory.py's DELETE route
        docstring for why this differs from research notes, which are
        never deleted, only marked superseded."""
        await self.db.collection("memories").document(memory_id).delete()

    async def touch_memory_used(self, memory_id: str) -> None:
        """Record that a memory was actually surfaced to Annie — feeds
        consolidation's sense of which long-term memories are still earning
        their place versus quietly going stale."""
        await self.db.collection("memories").document(memory_id).set(
            {"last_used_at": utcnow()}, merge=True
        )

    async def list_memories(
        self,
        *,
        type_: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Memory], int]:
        query = self.db.collection("memories")
        if type_:
            query = query.where(filter=FieldFilter("type", "==", type_))
        if status:
            query = query.where(filter=FieldFilter("status", "==", status))
        if tag:
            query = query.where(filter=FieldFilter("tags", "array_contains", tag))
        query = query.order_by("created_at", direction=Query.DESCENDING)
        all_docs = [s async for s in query.stream()]
        page = all_docs[offset : offset + limit]
        return [
            from_doc(Memory, s.id, s.to_dict() or {}, id=s.id) for s in page
        ], len(all_docs)

    async def create_consolidation_run(self, run: ConsolidationRun) -> ConsolidationRun:
        ref = self.db.collection("consolidation_runs").document()
        run.id = ref.id
        run.created_at = utcnow()
        await ref.set(to_doc(run))
        return run

    async def list_consolidation_runs(self, *, limit: int = 20) -> list[ConsolidationRun]:
        query = (
            self.db.collection("consolidation_runs")
            .order_by("created_at", direction=Query.DESCENDING)
            .limit(limit)
        )
        return [
            from_doc(ConsolidationRun, s.id, s.to_dict() or {}, id=s.id) async for s in query.stream()
        ]

    async def active_memories_by_importance(self, *, type_: str | None = None, limit: int = 20) -> list[Memory]:
        """Retrieval-oriented read: active memories ranked by importance,
        used by Annie's memory-search tool and consolidation — not the
        chronological listing `list_memories` gives the frontend."""
        query = self.db.collection("memories").where(
            filter=FieldFilter("status", "==", "active")
        )
        if type_:
            query = query.where(filter=FieldFilter("type", "==", type_))
        docs = [s async for s in query.stream()]
        memories = [from_doc(Memory, s.id, s.to_dict() or {}, id=s.id) for s in docs]
        memories.sort(key=lambda m: m.importance if m.importance is not None else 0.0, reverse=True)
        return memories[:limit]

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

    # -- personality (operator-editable voice knobs, one singleton doc) ------

    async def get_personality_config(self) -> PersonalityConfig | None:
        snap = await self.db.collection("personality").document("config").get()
        if not snap.exists:
            return None
        return from_doc(PersonalityConfig, snap.id, snap.to_dict() or {})

    async def upsert_personality_config(
        self, config: PersonalityConfig, *, actor: str = "operator"
    ) -> PersonalityConfig:
        config.updated_at = utcnow()
        config.updated_by = actor
        await self.db.collection("personality").document("config").set(to_doc(config), merge=True)
        return config

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
