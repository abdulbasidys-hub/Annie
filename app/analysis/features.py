"""Deterministic feature extraction from names, tickers and descriptions (§16).

Deliberately runs *before* any LLM touches a token. Two reasons:

* Cost (§48). Tokenising a ticker does not need a model.
* Reproducibility. A trend built on deterministic features can be recomputed
  identically next year; one built on model output cannot, because the model
  will have changed. Where an LLM does contribute a feature it is stored with
  ``source="llm"`` so a trend resting on model judgement is identifiable.

§16 requires categories to *emerge* rather than come from a fixed list, so the
keyword maps here are seeds for bootstrapping, not the vocabulary. The
n-gram extractor is the part that discovers new themes: it surfaces whatever is
actually recurring, including words no one thought to enumerate.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

# -----------------------------------------------------------------------------
# Seed vocabulary (§16). Starting points, not the universe.
# -----------------------------------------------------------------------------

SEED_THEMES: dict[str, tuple[str, ...]] = {
    "animal": (
        "dog", "cat", "shiba", "inu", "doge", "pepe", "frog", "bear", "bull",
        "monkey", "ape", "wolf", "cow", "duck", "hippo", "penguin", "goat",
        "rat", "mouse", "fox", "owl", "shark", "whale", "snake", "bird",
    ),
    "ai": (
        "ai", "gpt", "agent", "neural", "robot", "bot", "llm", "model",
        "singularity", "asi", "agi", "machine", "prompt", "token",
    ),
    "politics": (
        "trump", "biden", "maga", "election", "president", "senate", "vance",
        "kamala", "政治", "政策", "government", "congress", "vote",
    ),
    "celebrity": (
        "elon", "musk", "kanye", "swift", "bezos", "zuck", "drake", "rogan",
    ),
    "crypto_culture": (
        "wagmi", "ngmi", "hodl", "rekt", "moon", "lambo", "degen", "ser",
        "gm", "fren", "based", "cope", "jeet", "bags", "ape", "chad",
    ),
    "gaming": ("game", "play", "quest", "pixel", "arcade", "level", "boss", "loot"),
    "finance": ("bank", "fed", "rate", "yield", "bond", "dollar", "gold", "usd"),
    "internet_meme": (
        "wojak", "chad", "sigma", "skibidi", "rizz", "gyatt", "npc", "sus",
        "brainrot", "goon", "mog",
    ),
    "absurd": ("random", "nothing", "literally", "unemployed", "broke", "sad"),
}

#: Words too common to carry meaning in a memecoin name. Started life missing
#: ordinary filler words ("you", "still", ...) that aren't articles/prepositions
#: but are just as meaningless as a narrative signal — found 2026-08-25 when a
#: single token's name ("you are still early") produced two separate "trends"
#: (name.word=you, name.word=still) that outranked every real signal because
#: sorting by raw percentage change favours a brand-new word with zero baseline
#: (see app/annie/agent.py's _tool_list_trends for the other half of that fix).
STOPWORDS = frozenset(
    """
    the a an and or of on in to for with is it this that coin token solana sol
    official new real x2 v2 by at from as be are was were
    you your youre yours i im ive my mine we our ours us he she they them their
    still just now here there when where why how what who which
    will would can could should shall may might must not no yes yeah ok okay
    so up out on off over under again more most some any all one two
    very much many lot get got go going come do does did done have has had
    not no nor never always
    """.split()
)

_WORD_RE = re.compile(r"[a-z0-9']+")
_EMOJI_RE = re.compile(
    "[" "\U0001f300-\U0001faff" "\U00002700-\U000027bf" "\U0001f000-\U0001f0ff" "]",
    flags=re.UNICODE,
)
_URL_RE = re.compile(r"https?://\S+")


@dataclass(slots=True)
class Feature:
    """One extracted characteristic, ready to become a ``TokenFeature`` row."""

    namespace: str
    key: str
    value: str | None = None
    numeric_value: float | None = None
    source: str = "deterministic"


@dataclass(slots=True)
class ExtractedFeatures:
    features: list[Feature] = field(default_factory=list)
    tokens: list[str] = field(default_factory=list)
    themes: set[str] = field(default_factory=set)

    def add(self, namespace: str, key: str, value: str | None = None, numeric: float | None = None) -> None:
        self.features.append(Feature(namespace, key, value, numeric))


def normalise(text: str) -> str:
    """Lowercase, strip accents, drop URLs.

    Accent stripping matters more than it looks: memecoin names routinely use
    lookalike characters, and without normalisation "PEPÉ" and "PEPE" become
    two unrelated narratives that each fall below the sample threshold.
    """
    text = _URL_RE.sub(" ", text)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().strip()


def tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(normalise(text)) if w not in STOPWORDS]


def extract_name_features(name: str | None) -> ExtractedFeatures:
    out = ExtractedFeatures()
    if not name:
        return out

    words = tokenize(name)
    out.tokens = words
    out.add("name", "length", numeric=float(len(name)))
    out.add("name", "word_count", numeric=float(len(words)))
    out.add("name", "has_emoji", value=str(bool(_EMOJI_RE.search(name))).lower())
    out.add("name", "is_single_word", value=str(len(words) == 1).lower())

    for word in words:
        out.add("name", "word", value=word)

    for theme, keywords in SEED_THEMES.items():
        if any(w in keywords for w in words):
            out.themes.add(theme)
            out.add("name", "theme", value=theme)

    return out


def extract_ticker_features(symbol: str | None) -> ExtractedFeatures:
    """§16's ticker analysis: length, structure, repeats, digit mixing.

    Structure is worth capturing separately from content because ticker
    *shape* turns out to move in fashions independently of theme — a wave of
    four-letter all-caps tickers is a different phenomenon from a wave of
    animal names, and collapsing them would hide both.
    """
    out = ExtractedFeatures()
    if not symbol:
        return out

    raw = symbol.strip()
    cleaned = normalise(raw)
    out.add("ticker", "length", numeric=float(len(raw)))
    out.add("ticker", "value", value=cleaned)
    out.add("ticker", "is_upper", value=str(raw.isupper()).lower())
    out.add("ticker", "has_digits", value=str(any(c.isdigit() for c in raw)).lower())
    out.add("ticker", "has_repeated_chars", value=str(_has_repeat(cleaned, 3)).lower())

    if len(raw) <= 3:
        shape = "short"
    elif len(raw) <= 5:
        shape = "standard"
    elif len(raw) <= 8:
        shape = "long"
    else:
        shape = "very_long"
    out.add("ticker", "shape", value=shape)

    for theme, keywords in SEED_THEMES.items():
        if cleaned in keywords:
            out.themes.add(theme)
            out.add("ticker", "theme", value=theme)

    return out


def extract_description_features(description: str | None) -> ExtractedFeatures:
    out = ExtractedFeatures()
    if not description:
        # Absence is itself a feature — "no description" may correlate with
        # outcomes, and without this row it would be indistinguishable from a
        # token we simply never enriched.
        out.add("description", "present", value="false")
        return out

    words = tokenize(description)
    out.tokens = words
    out.add("description", "present", value="true")
    out.add("description", "length", numeric=float(len(description)))
    out.add("description", "word_count", numeric=float(len(words)))
    out.add("description", "has_url", value=str(bool(_URL_RE.search(description))).lower())
    out.add("description", "has_emoji", value=str(bool(_EMOJI_RE.search(description))).lower())

    for theme, keywords in SEED_THEMES.items():
        if any(w in keywords for w in words):
            out.themes.add(theme)
            out.add("description", "theme", value=theme)

    for word in words[:40]:  # cap: long descriptions must not dominate counts
        out.add("description", "word", value=word)

    return out


def extract_all(
    name: str | None, symbol: str | None, description: str | None
) -> list[Feature]:
    """Everything deterministic about a token's text, in one list."""
    parts = [
        extract_name_features(name),
        extract_ticker_features(symbol),
        extract_description_features(description),
    ]
    features = [f for part in parts for f in part.features]

    # A token-level theme rollup, so cohort queries do not have to union three
    # namespaces to answer "was this token AI-themed?".
    themes: set[str] = set()
    for part in parts:
        themes |= part.themes
    for theme in sorted(themes):
        features.append(Feature("token", "theme", theme))
    if not themes:
        features.append(Feature("token", "theme", "uncategorised"))

    return features


# -----------------------------------------------------------------------------
# Emergent theme discovery (§16 — "discover categories rather than relying
# exclusively on hardcoded categories")
# -----------------------------------------------------------------------------


def discover_ngrams(
    texts: list[str], *, n: int = 1, min_count: int = 5, top_k: int = 50
) -> list[tuple[str, int]]:
    """Most frequent n-grams across a cohort, excluding seeded vocabulary.

    Seeded words are excluded so the output is specifically *what we did not
    already know to look for*. Returning "dog" as a discovery every week would
    bury the one genuinely new word that matters.
    """
    seeded = {w for words in SEED_THEMES.values() for w in words}
    counter: Counter[str] = Counter()

    for text in texts:
        words = tokenize(text)
        if n == 1:
            grams = words
        else:
            grams = [" ".join(words[i : i + n]) for i in range(len(words) - n + 1)]
        for gram in grams:
            if n == 1 and gram in seeded:
                continue
            if len(gram) < 3:
                continue
            counter[gram] += 1

    return [(gram, count) for gram, count in counter.most_common(top_k) if count >= min_count]


def _has_repeat(text: str, run: int) -> bool:
    if len(text) < run:
        return False
    count = 1
    for i in range(1, len(text)):
        count = count + 1 if text[i] == text[i - 1] else 1
        if count >= run:
            return True
    return False
