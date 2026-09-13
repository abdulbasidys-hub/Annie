"""Taught craft — how to name a coin, brief art, and write the post.

Everything else Annie knows she worked out from data. This is the other kind
of knowledge: a method somebody already has, handed over as instruction. The
six files came from an operator who launches tokens for a living, and they
describe a process rather than a style — start from the event and not the
coin, find the canonical subject, test the name against what Know Your Meme
would have called it tomorrow.

**Why they are memory files rather than constants.** The operator has to be
able to read what she was taught, disagree with it, and change it, in the
same place they read everything else she knows. A prompt fragment buried in
Python is not reviewable by the person whose method it is. They live in
``skills/``, they appear in the Memory page, and editing one changes her
behaviour on the next cycle with no deploy.

**Why routing rather than loading everything.** The pack's own README says to
combine 02+03+04+06 for a meme launch and 01+02+04+06 for protocol branding,
which is a real distinction: the rules for a three-hour meme coin and for a
protocol that wants to look trustworthy contradict each other in places.
Sending both sets would ask the model to hold two opposed briefs at once, and
it would also spend twelve thousand tokens to do it.
"""

from __future__ import annotations

from pathlib import Path

import structlog

from app.memory import service
from app.memory.paths import memory_root

log = structlog.get_logger(__name__)

#: Where the pack is shipped in the repo, for first-boot seeding.
SOURCE_DIR = Path(__file__).resolve().parents[2] / "MD Files"

#: slug -> (filename in the pack, what it is for)
SKILLS: dict[str, tuple[str, str]] = {
    "visual-design": (
        "01_brand_identity_visual_design.md",
        "Logo, colour, type, banners, PFPs — and the critique standard.",
    ),
    "web3-branding": (
        "02_web3_crypto_branding.md",
        "Branding a crypto product: mechanism into visual language, launch positioning.",
    ),
    "meme-naming": (
        "03_meme_branding_naming.md",
        "How to name a meme coin: start from the event, find the canonical subject.",
    ),
    "social-copy": (
        "04_social_media_creative.md",
        "Launch posts and announcements that read observed rather than manufactured.",
    ),
    "community": (
        "05_community_moderation_work.md",
        "Running and moderating a token community.",
    ),
    "market-culture": (
        "06_trading_crypto_market_culture.md",
        "How traders actually talk and what reads as a scam to them.",
    ),
}

#: Which skills a task loads, from the pack's own README.
ROUTES: dict[str, tuple[str, ...]] = {
    # A meme launch. The operator's main use.
    "launch": ("web3-branding", "meme-naming", "social-copy", "market-culture"),
    # Naming alone, when she is asked to iterate on a name or ticker.
    "naming": ("meme-naming", "market-culture"),
    # Art direction — briefing an image model or critiquing what came back.
    "visual": ("visual-design", "web3-branding"),
    # Writing the post.
    "copy": ("social-copy", "market-culture"),
    # A serious protocol rather than a meme.
    "protocol": ("visual-design", "web3-branding", "social-copy", "market-culture"),
}


def path_for(slug: str) -> str:
    return f"skills/{slug}.md"


def seed() -> list[str]:
    """Copy the pack into memory on first boot. Never overwrites.

    Not overwriting is the point: once these are memory files the operator
    owns them, and a redeploy silently reverting their edits would be the
    worst possible behaviour for a file whose whole purpose is to carry
    somebody's own method.
    """
    if not SOURCE_DIR.is_dir():
        return []

    written: list[str] = []
    root = memory_root() / "skills"
    root.mkdir(parents=True, exist_ok=True)

    for slug, (filename, description) in SKILLS.items():
        target = root / f"{slug}.md"
        if target.exists():
            continue
        source = SOURCE_DIR / filename
        if not source.is_file():
            log.info("skill_source_missing", slug=slug, expected=str(source))
            continue
        body = source.read_text(encoding="utf-8")
        target.write_text(
            "---\n"
            f"title: {description}\n"
            f"kind: skill\n"
            "source: operator\n"
            "confidence: high\n"
            "importance: 0.9\n"
            f"tags: [skill, {slug}]\n"
            "---\n\n" + body,
            encoding="utf-8",
        )
        written.append(path_for(slug))

    if written:
        log.info("skills_seeded", files=written)
    return written


def load(slug: str) -> str:
    """One skill's text, or empty if it is not there."""
    memory = service.read(path_for(slug))
    return memory.body if memory else ""


def for_task(task: str = "launch") -> str:
    """The skills that apply to a task, as one block for a prompt.

    Returns empty when nothing is installed, and every caller treats that as
    "carry on without them" — a missing pack must degrade her ideas, never
    stop them.
    """
    slugs = ROUTES.get(task, ROUTES["launch"])
    parts: list[str] = []
    for slug in slugs:
        body = load(slug).strip()
        if body:
            parts.append(body)

    if not parts:
        return ""

    return (
        "# How to do this well\n\n"
        "The operator's own method, given to you directly. It is not a style "
        "suggestion and it outranks your instincts about what makes a good "
        "name or a good post — where it contradicts you, it wins.\n\n"
        + "\n\n---\n\n".join(parts)
    )


def available() -> list[dict[str, str]]:
    """What is installed, for the chat tool and the Memory page."""
    out = []
    for slug, (_, description) in SKILLS.items():
        if service.exists(path_for(slug)):
            out.append({"slug": slug, "path": path_for(slug), "about": description})
    return out
