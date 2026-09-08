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
from datetime import datetime, timedelta, timezone

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
    """Group tokens that cleared a tier into seeded and emergent narratives.

    Reads the local ledger, not Firestore. Two things changed with that, and
    both were cost:

    * The cohort is fetched once from SQLite instead of a paged Firestore
      query over up to a thousand token documents.
    * Themes are derived from each token's name and ticker by the same pure
      functions the signals engine uses, rather than through
      ``features_with_value``, which was a collection-group query per seeded
      theme against per-token feature subcollections that no longer exist.

    Narratives themselves stay in Firestore — there are a few dozen, they
    change slowly, and the website reads them directly. That is exactly the
    profile Firestore is still the right tool for.
    """
    from app.analysis.features import extract_all
    from app.memory import ledger

    run = ClusteringRun(started_at=datetime.now(timezone.utc))
    now = datetime.now(timezone.utc)

    # The whole qualified cohort — small by construction, since a token only
    # gets here by actually clearing a tier.
    tokens = ledger.qualified_in_window(now - timedelta(days=90), now)
    total_qualified = len(tokens)
    run.qualified_tokens_scanned = total_qualified
    if not total_qualified:
        run.finished_at = now
        return run

    # -- Seeded themes: derived in memory, one pass over the cohort. --
    by_theme: dict[str, list[str]] = {}
    for token in tokens:
        for feature in extract_all(token.name, token.symbol, None):
            if feature.namespace == "token" and feature.key == "theme" and feature.value:
                by_theme.setdefault(feature.value, []).append(token.mint)

    for theme, keywords in SEED_THEMES.items():
        mints = by_theme.get(theme) or []
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

    # -- Emergent: n-gram discovery over the cohort's names, tokenized the
    #    same way features.py tags a token's own themes, so a short gram like
    #    "cat" cannot match inside an unrelated word like "concatenate". --
    token_words: dict[str, tuple[list[str], list[str]]] = {}
    for token in tokens:
        words = tokenize(f"{token.name or ''} {token.symbol or ''}")
        bigrams = [" ".join(words[i : i + 2]) for i in range(len(words) - 1)]
        token_words[token.mint] = (words, bigrams)

    texts = [f"{t.name or ''} {t.symbol or ''}" for t in tokens]
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
