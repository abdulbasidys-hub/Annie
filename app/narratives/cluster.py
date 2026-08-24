"""Narrative clustering (§16) — the stage that populates the `narratives`
collection, previously empty (see Narrative's own docstring in
app/db/models/entities.py, and README's former "Narrative clustering: Not
written" row).

§16 asks for two things, both already half-built and unused before this:

* **Seeded themes** (animal, ai, politics, ...) — `app/analysis/features.py`'s
  `SEED_THEMES` already tags every qualified token with a `token.theme`
  TokenFeature. This stage turns each theme into a first-class `Narrative`
  record with real counts, instead of that tag only ever being readable by
  re-deriving it from raw feature rows.
* **Emergent discovery** — "the system should discover categories rather
  than relying exclusively on hardcoded categories". `discover_ngrams`
  already existed in features.py, deliberately excluding seeded vocabulary,
  and was never called from anywhere. This stage is that call site.

Deterministic, like the daily log and report generator — no LLM call, same
reasoning: reproducible, and §48's cost discipline doesn't need a model to
count word frequencies. Where an LLM-driven narrative feature exists later
it should be tagged `source="llm"` per features.py's own convention, not
folded into this pass.

`token_count` and `qualified_count` are always equal here on purpose: both
counting paths (the seeded collection-group query, the emergent name/
description scan) only ever look at data that exists *because* a token
qualified — deterministic features are written by Stage-3 enrichment, which
only runs post-qualification. There's no "matched a narrative but never
qualified" case for this stage to distinguish. `baseline_share` (a
recent-vs-historical comparison) is deliberately left unset — that
statistical machinery already exists and is tested in
`app/trends/engine.py`/`app/analysis/stats.py`, working directly off the
same `token.theme` feature; duplicating it here would be a second,
divergent implementation of the same comparison, not new coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.analysis.features import SEED_THEMES, discover_ngrams, tokenize
from app.db.base import slugify
from app.db.models.entities import Narrative
from app.db.repo import FirestoreRepo


@dataclass(slots=True)
class ClusteringRun:
    started_at: datetime
    finished_at: datetime | None = None
    qualified_tokens_scanned: int = 0
    seeded_narratives_updated: int = 0
    emergent_narratives_found: int = 0


async def run_narrative_clustering(
    repo: FirestoreRepo, *, min_emergent_count: int = 3
) -> ClusteringRun:
    run = ClusteringRun(started_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)

    tokens, total_qualified = await repo.list_tokens(qualified_only=True, limit=1000)
    run.qualified_tokens_scanned = len(tokens)
    if not tokens or not total_qualified:
        run.finished_at = now
        return run

    # -- Seeded themes: one collection-group query per theme, not one read
    #    per qualified token — see features_with_value's own docstring. --
    for theme, keywords in SEED_THEMES.items():
        mints = await repo.features_with_value(namespace="token", key="theme", value=theme)
        if not mints:
            continue
        await repo.upsert_narrative(
            Narrative(
                slug=theme,
                label=theme.replace("_", " ").title(),
                category=theme,
                keywords=list(keywords),
                is_emergent=False,
                last_seen_at=now,
                token_count=len(mints),
                qualified_count=len(mints),
                share_of_qualified=len(mints) / total_qualified,
                stats_computed_at=now,
            )
        )
        run.seeded_narratives_updated += 1

    # -- Emergent: n-gram discovery over qualified tokens' name+description,
    #    tokenized the same way features.py tags a token's own themes, so a
    #    short gram like "cat" can't match inside an unrelated word like
    #    "concatenate". --
    token_words: dict[str, tuple[list[str], list[str]]] = {}
    for t in tokens:
        words = tokenize(f"{t.name or ''} {t.description or ''}")
        bigrams = [" ".join(words[i : i + 2]) for i in range(len(words) - 1)]
        token_words[t.mint] = (words, bigrams)

    texts = [f"{t.name or ''} {t.description or ''}" for t in tokens]
    discovered = discover_ngrams(texts, n=1, min_count=min_emergent_count, top_k=30)
    discovered += discover_ngrams(texts, n=2, min_count=max(2, min_emergent_count - 1), top_k=15)

    for gram, _seen_count in discovered:
        is_bigram = " " in gram
        matching_mints = [
            mint
            for mint, (words, bigrams) in token_words.items()
            if (gram in bigrams if is_bigram else gram in words)
        ]
        if not matching_mints:
            continue
        slug = slugify(gram, max_length=60)
        if not slug or slug == "unknown":
            continue
        await repo.upsert_narrative(
            Narrative(
                slug=slug,
                label=gram,
                category=None,
                keywords=[gram],
                is_emergent=True,
                last_seen_at=now,
                token_count=len(matching_mints),
                qualified_count=len(matching_mints),
                share_of_qualified=len(matching_mints) / total_qualified,
                stats_computed_at=now,
            )
        )
        run.emergent_narratives_found += 1

    run.finished_at = datetime.now(timezone.utc)
    return run
