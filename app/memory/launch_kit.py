"""The full launch kit for an idea the operator picked.

Ideas and kits are deliberately two different things. An idea is a decision —
name, ticker, hook, why now, what kills it — and it is short because the
operator is scanning three of them to choose one. A kit is everything needed
to actually ship: art direction, the site, the X and Telegram accounts, and
the posts for the first day. Producing a kit for every idea would bury the
decision in material for two launches that will never happen, and it was also
what truncated the daily ideas call.

So this runs on request, for one idea, and it can afford to be long.

**The art prompts are the part that matters most.** They go straight into an
image model with no editing, so they carry the operator's own design method —
the visual-design and web3-branding skills are loaded into this call for
exactly that reason. A prompt written without them produces the generic
crypto art the method spends several pages telling you to avoid.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

from app.annie import voice
from app.config import Settings
from app.providers.registry import ProviderRegistry

log = structlog.get_logger(__name__)

KIT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "description",
        "visual_identity",
        "logo_prompt",
        "pfp_prompt",
        "banner_prompt",
        "meme_prompts",
        "website",
        "x_account",
        "telegram",
        "launch_posts",
        "first_hour",
    ],
    "properties": {
        "description": {
            "type": "string",
            "description": (
                "The launchpad description field, to be pasted in as-is. One or "
                "two lines, in the register the token is aiming at. Not an "
                "explanation of the strategy — the actual copy."
            ),
        },
        "visual_identity": {
            "type": "string",
            "description": (
                "The look, in two or three sentences: the palette and where it "
                "comes from, the type, the level of finish. This is the brief "
                "everything visual below has to agree with."
            ),
        },
        "logo_prompt": {
            "type": "string",
            "description": (
                "A complete image-model prompt for the logo/mark, pasted in with "
                "no editing. Subject, what it is doing, framing, background, "
                "lighting, art style, square. 60-100 words. It is judged at 32 "
                "pixels in a list first, so: one subject, strong silhouette, high "
                "contrast, no small text, nothing important at the edges."
            ),
        },
        "pfp_prompt": {
            "type": "string",
            "description": (
                "The same character as a profile picture — closer crop, readable "
                "as a circle, works at 48 pixels. A complete prompt, not a note "
                "saying 'as above but cropped'."
            ),
        },
        "banner_prompt": {
            "type": "string",
            "description": (
                "A complete prompt for the X/Telegram banner. Wide 3:1, subject "
                "off-centre so a centred profile picture does not cover it, and "
                "say explicitly where the empty space is."
            ),
        },
        "meme_prompts": {
            "type": "array",
            "minItems": 2,
            "maxItems": 4,
            "items": {"type": "string"},
            "description": (
                "Complete prompts for launch-day graphics — the posts people "
                "actually repost. Each one a different situation for the same "
                "character, not a restyling of the logo."
            ),
        },
        "website": {
            "type": "object",
            "additionalProperties": False,
            "required": ["concept", "sections", "build_notes"],
            "properties": {
                "concept": {
                    "type": "string",
                    "description": "What a visitor understands in two seconds, and "
                    "what they do next.",
                },
                "sections": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 6,
                    "items": {"type": "string"},
                    "description": "Top to bottom, one phrase each, with what goes "
                    "in it. Most sites that work are one scroll — three good "
                    "sections beat six padded ones.",
                },
                "build_notes": {
                    "type": "string",
                    "description": "The handoff to a build agent: tone, palette, "
                    "motion or none, what to reuse from the art, and the one "
                    "mistake most likely to make it look like a scam.",
                },
            },
        },
        "x_account": {
            "type": "object",
            "additionalProperties": False,
            "required": ["handle", "display_name", "bio", "pinned_post"],
            "properties": {
                "handle": {"type": "string", "description": "No @, under 15 chars."},
                "display_name": {"type": "string"},
                "bio": {"type": "string", "description": "Under 160 characters."},
                "pinned_post": {
                    "type": "string",
                    "description": "What stays at the top of the profile.",
                },
            },
        },
        "telegram": {
            "type": "object",
            "additionalProperties": False,
            "required": ["group_name", "description", "pinned_message"],
            "properties": {
                "group_name": {"type": "string"},
                "description": {"type": "string"},
                "pinned_message": {
                    "type": "string",
                    "description": "Contract address goes here, plus the one thing "
                    "a new arrival needs.",
                },
            },
        },
        "launch_posts": {
            "type": "array",
            "minItems": 3,
            "maxItems": 6,
            "items": {"type": "string"},
            "description": (
                "The first day on X, in order, each ready to post verbatim. Under "
                "240 characters, no hashtags unless the joke needs one, no "
                "contract address in the first one. They must work for somebody "
                "who has never heard of the token — copy that only lands for "
                "existing holders is not launch copy."
            ),
        },
        "first_hour": {
            "type": "string",
            "description": (
                "What to do in the first hour, in order, plainly. Where the "
                "contract address goes and when, what to reply to, what to leave "
                "alone. This is the part that is usually improvised badly."
            ),
        },
    },
}

SYSTEM_PROMPT = """The operator has chosen one of your ideas and is going to
launch it. Produce everything they need to ship it today.

This is production work, not exploration. Every field is something they will
paste somewhere — a launchpad form, an image model, a profile, a post. Write
the artefact, never a description of the artefact. "A punchy bio about the
cat" is useless; the bio itself is the deliverable.

The art prompts are the highest-value thing here. They go into an image model
unedited, so they must be complete: subject, action, expression, framing,
background, lighting, style, aspect. Follow the design method you have been
given rather than defaulting to what crypto art usually looks like — the
method exists because the default is what makes a token look like every other
token.

Be consistent across the whole kit. The character in the logo is the same
character in the memes, the palette in the visual identity is the palette on
the site, and the voice in the bio is the voice in the posts. A kit that
disagrees with itself is worse than a thinner one that holds together.

Where the idea is weak, say so in `first_hour` rather than papering over it.
The operator can still choose to launch; they should not be surprised."""


async def generate(
    idea: dict[str, Any],
    registry: ProviderRegistry,
    settings: Settings,
    *,
    context: str = "",
) -> dict[str, Any]:
    """Everything needed to launch one idea.

    ``context`` is anything the operator added when asking — "make it darker",
    "we are launching at 2am UTC" — passed through as a steer rather than as
    an instruction that can override the method.
    """
    if not settings.is_available("ai"):
        return {"error": "OpenAI is not configured in this deployment."}

    from app.memory import skills

    craft = skills.for_task("launch")
    visual = skills.load("visual-design")
    system = voice.prefix(SYSTEM_PROMPT)
    if craft:
        system = f"{system}\n\n---\n\n{craft}"
    if visual:
        system = f"{system}\n\n---\n\n# Art direction method\n\n{visual}"

    brief = _render_brief(idea, context)

    client = await registry.reasoning.raw_client()
    try:
        response = await client.chat.completions.create(
            model=settings.openai_reasoning_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": brief},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "launch_kit", "strict": True, "schema": KIT_SCHEMA},
            },
            # Generous on purpose. This is one call, on request, producing
            # roughly twenty artefacts — several of them 60-100 word image
            # prompts. Truncation here is how a day's ideas silently failed
            # before, and the failure mode is identical.
            max_completion_tokens=6000,
            temperature=0.4,
            reasoning_effort="none",
        )
    except Exception as exc:
        log.error("launch_kit_failed", ticker=idea.get("ticker"), exc_info=True)
        return {"error": f"Could not build the kit: {exc}"[:300]}

    try:
        kit = json.loads(response.choices[0].message.content or "{}")
    except json.JSONDecodeError:
        finish = getattr(response.choices[0], "finish_reason", None)
        log.error("launch_kit_unparseable", finish_reason=finish)
        return {
            "error": (
                "The response was cut off before it finished."
                if finish == "length"
                else "The model returned something unparseable."
            )
        }

    usage = getattr(response, "usage", None)
    kit["name"] = idea.get("name")
    kit["ticker"] = idea.get("ticker")
    kit["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    kit["input_tokens"] = getattr(usage, "prompt_tokens", None)
    kit["output_tokens"] = getattr(usage, "completion_tokens", None)
    log.info("launch_kit_built", ticker=idea.get("ticker"))
    return kit


def _render_brief(idea: dict[str, Any], context: str) -> str:
    lines = [
        "# The idea they chose",
        "",
        f"Name: {idea.get('name')}",
        f"Ticker: ${idea.get('ticker')}",
        f"What it is: {idea.get('angle') or idea.get('description') or '?'}",
        f"Why it spreads: {idea.get('hook') or '?'}",
        f"Why now: {idea.get('why_now') or '?'}",
        f"What it rests on: {idea.get('evidence') or '?'}",
        f"Grounding: {idea.get('grounding') or '?'}",
        f"The risk you flagged: {idea.get('risk') or '?'}",
    ]
    if context.strip():
        lines += ["", f"**What the operator added:** {context.strip()}"]
    return "\n".join(lines)


def format_for_delivery(kit: dict[str, Any]) -> str:
    """The kit as a chat message. Long, and meant to be scrolled."""
    if kit.get("error"):
        return f"Could not build the kit — {kit['error']}"

    site = kit.get("website") or {}
    x = kit.get("x_account") or {}
    tg = kit.get("telegram") or {}

    lines = [
        f"**{kit.get('name')}**  `${kit.get('ticker')}` — launch kit",
        "",
        f"**Description**\n{kit.get('description')}",
        "",
        f"**Look**\n{kit.get('visual_identity')}",
        "",
        "**Logo prompt**",
        f"```\n{kit.get('logo_prompt')}\n```",
        "**PFP prompt**",
        f"```\n{kit.get('pfp_prompt')}\n```",
        "**Banner prompt**",
        f"```\n{kit.get('banner_prompt')}\n```",
    ]

    memes = kit.get("meme_prompts") or []
    if memes:
        lines.append("**Launch graphics**")
        for i, prompt in enumerate(memes, 1):
            lines.append(f"```\n{i}. {prompt}\n```")

    lines += [
        "",
        f"**Website** — {site.get('concept', '')}",
        "· " + " → ".join(str(x_) for x_ in (site.get("sections") or [])),
        f"· Build: {site.get('build_notes', '')}",
        "",
        f"**X** @{x.get('handle')} · {x.get('display_name')}",
        f"· Bio: {x.get('bio')}",
        f"· Pinned: {x.get('pinned_post')}",
        "",
        f"**Telegram** {tg.get('group_name')}",
        f"· {tg.get('description')}",
        f"· Pinned: {tg.get('pinned_message')}",
        "",
        "**First day on X**",
    ]
    for i, post in enumerate(kit.get("launch_posts") or [], 1):
        lines.append(f"{i}. {post}")

    lines += ["", f"**First hour**\n{kit.get('first_hour')}"]
    return "\n".join(lines)
