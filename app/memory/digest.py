"""What Annie actually looks at each cycle — built entirely without a model.

This module is the reason the rewrite does not just move cost from Firestore
into OpenAI tokens. Every cycle sees roughly 16,000 launches. None of them
reach the model. Instead, plain Python does the counting, ranking and
filtering for free, and produces a digest of a few thousand characters: the
tokens that actually moved, who launched them, which characteristics are
over-represented, and the handful of existing memories that bear on any of
it.

The budget discipline is explicit and enforced by construction rather than by
hoping the prompt stays small:

======================  =======  ===================================
Section                 Cap      Chosen by
======================  =======  ===================================
Movers                  20       peak market cap in window
Newly qualified         15       tier reached
Busy creators           10       launches in window
Meaningful signals      10       statistical bars, then lift
Emerging words          8        frequency across winners' names
Recalled memories       8        key match, then FTS relevance
======================  =======  ===================================

That lands around 2,500-4,000 input tokens including the core files —
roughly a cent per cycle at four cycles a day. If a section ever needs to
grow, it should displace another, not extend the total: the discipline is
what keeps this affordable as memory grows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import structlog

from app.memory import bootstrap, index, ledger, signals
from app.memory.index import Hit

log = structlog.get_logger(__name__)

MAX_MOVERS = 20
MAX_QUALIFIED = 15
MAX_CREATORS = 10
MAX_SIGNALS = 10
MAX_WORDS = 8
MAX_RECALL = 8


def _usd(value: float | None) -> str:
    if not value:
        return "?"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}k"
    return f"${value:.0f}"


@dataclass(slots=True)
class CycleDigest:
    """One cycle's compressed view of the market, plus what Annie already knows."""

    window_hours: int
    started_at: datetime
    stats: dict[str, Any] = field(default_factory=dict)
    movers: list[ledger.Sighting] = field(default_factory=list)
    newly_qualified: list[ledger.Sighting] = field(default_factory=list)
    busy_creators: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    words: list[dict[str, Any]] = field(default_factory=list)
    recalled: list[Hit] = field(default_factory=list)
    core: list[Hit] = field(default_factory=list)
    #: Raw text of ``core/instructions.md`` — what the operator has told her
    #: to keep doing. Carried separately from ``core`` so it can be placed
    #: last in the prompt, where an instruction is least likely to be lost
    #: behind a wall of market data.
    instructions: str = ""

    @property
    def is_empty(self) -> bool:
        """Nothing happened worth a model call.

        Checked before spending anything: a quiet window (an outage, a dead
        night, a fresh deployment with no history) should cost zero, not a
        call that produces a memory saying "nothing happened".
        """
        return not (self.movers or self.newly_qualified or self.signals)

    def facts(self) -> dict[str, Any]:
        """The structured half, for the API and for tests to assert against."""
        return {
            "window_hours": self.window_hours,
            "stats": self.stats,
            "movers": [
                {
                    "mint": m.mint, "symbol": m.symbol, "name": m.name,
                    "creator": m.creator, "launchpad": m.launchpad,
                    "peak": m.peak_market_cap, "now": m.market_cap,
                }
                for m in self.movers
            ],
            "newly_qualified": [
                {"mint": m.mint, "symbol": m.symbol, "tier": m.tier, "peak": m.peak_market_cap}
                for m in self.newly_qualified
            ],
            "busy_creators": self.busy_creators,
            "signals": self.signals,
            "words": self.words,
            "recalled": [h.to_dict() for h in self.recalled],
        }

    def render(self) -> str:
        """The prompt-facing text. Compact by design — every line earns its place."""
        lines: list[str] = [
            f"# Market window: last {self.window_hours}h "
            f"(as of {self.started_at.isoformat(timespec='minutes')})",
            "",
            f"Launches seen: {self.stats.get('sightings_24h', 0)} in 24h "
            f"({self.stats.get('sightings_total', 0)} currently held). "
            f"Reached a tier: {self.stats.get('qualified_24h', 0)}. "
            f"Creators tracked: {self.stats.get('creators_tracked', 0)} "
            f"of {self.stats.get('creators_total', 0)} seen.",
        ]

        if self.stats.get("launchpads"):
            share = ", ".join(
                f"{lp['launchpad']} {lp['n']}" for lp in self.stats["launchpads"][:5]
            )
            lines.append(f"By launchpad: {share}")

        if self.movers:
            lines += ["", "## Tokens that moved"]
            for m in self.movers:
                label = m.symbol or m.name or m.mint[:8]
                retrace = ""
                if m.peak_market_cap and m.market_cap and m.market_cap < m.peak_market_cap * 0.5:
                    retrace = f" (now {_usd(m.market_cap)}, round-tripped)"
                lines.append(
                    f"- {label} peaked {_usd(m.peak_market_cap)}{retrace} "
                    f"| {m.launchpad or 'unknown pad'} | CA {m.mint} "
                    f"| creator {m.creator or 'unknown'}"
                )

        if self.newly_qualified:
            lines += ["", "## Newly qualified this window"]
            for m in self.newly_qualified:
                lines.append(
                    f"- {m.symbol or m.mint[:8]} crossed {_usd(m.tier)}, peak {_usd(m.peak_market_cap)}"
                )

        if self.busy_creators:
            lines += ["", "## Creators launching most"]
            for c in self.busy_creators:
                lines.append(
                    f"- {c['wallet']}: {c.get('recent_launches', c.get('launches'))} launches in window, "
                    f"{c.get('winners', 0)} winners lifetime, best {_usd(c.get('best_market_cap'))}"
                    + (" [tracked]" if c.get("tracked") else "")
                )

        if self.signals:
            lines += ["", "## Characteristics over-represented among winners"]
            for s in self.signals:
                lift = f"{s['lift']:.1f}x" if s.get("lift") else "no baseline"
                lines.append(
                    f"- {s['name']} [{s['status']}] {s['recent_count']}/{s['recent_total']} "
                    f"of ${int(s['tier']):,}+ tokens, {lift} vs baseline"
                    + (" (thin sample)" if s.get("thin_sample") else "")
                )

        if self.words:
            lines += ["", "## Words recurring in winners' names (not in seed vocabulary)"]
            lines.append(", ".join(f"{w['phrase']} ({w['count']})" for w in self.words))

        if self.core:
            lines += ["", "## What you currently believe (core memory)"]
            for hit in self.core:
                lines.append(f"\n### {hit.path} — {hit.title}\n{hit.snippet.strip()}")

        if self.recalled:
            lines += ["", "## Related things you already wrote"]
            for hit in self.recalled:
                lines.append(f"- `{hit.path}` — {hit.title}: {hit.snippet.strip()[:300]}")

        # Last, deliberately. An instruction placed above several hundred
        # lines of market data competes with them for attention; placed at
        # the end it is the most recent thing read before answering.
        if self.instructions:
            lines += [
                "",
                "## Standing instructions from the operator",
                "",
                "These were given to you directly. They outrank your own judgement "
                "about what is worth writing down — follow them this cycle.",
                "",
                self.instructions,
            ]

        return "\n".join(lines)


def build(*, window_hours: int = 6, now: datetime | None = None) -> CycleDigest:
    """Assemble the cycle's view. No network, no model, no Firestore."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    movers = ledger.movers(since_hours=window_hours, limit=MAX_MOVERS)
    newly_qualified = ledger.qualified_in_window(since, now)[:MAX_QUALIFIED]
    busy = ledger.top_creators(limit=MAX_CREATORS, window_hours=window_hours)
    meaningful = signals.meaningful(limit=MAX_SIGNALS)
    words = signals.emerging_words(movers + newly_qualified, limit=MAX_WORDS)

    # Retrieval keys are the concrete handles this cycle is holding: the
    # mints that moved and the wallets behind them. Topics are the fuzzy
    # side. Keys are tried first (see app/memory/index.py) so the common
    # case is index probes, not a text scan.
    keys = [m.mint for m in movers[:8]]
    keys += [m.creator for m in movers[:8] if m.creator]
    keys += [c["wallet"] for c in busy[:5]]
    topics = [s["name"] for s in meaningful[:4]] + [w["phrase"] for w in words[:3]]

    recalled = index.recall(keys=keys, topics=topics, budget=MAX_RECALL)

    digest = CycleDigest(
        window_hours=window_hours,
        started_at=now,
        stats=ledger.stats(),
        movers=movers,
        newly_qualified=newly_qualified,
        busy_creators=busy,
        signals=meaningful,
        words=words,
        recalled=recalled,
        core=index.core_context(limit=4),
        instructions=bootstrap.standing_instructions(),
    )
    log.info(
        "digest_built",
        movers=len(movers),
        qualified=len(newly_qualified),
        signals=len(meaningful),
        recalled=len(recalled),
        chars=len(digest.render()),
    )
    return digest
