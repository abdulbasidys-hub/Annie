"""All Firestore document models — plain dataclasses, no ORM.

Unlike the SQLAlchemy version this replaces, importing this module has no
side effect (no metadata registry to populate) — it exists purely so callers
can write ``from app.db.models import Token`` instead of reaching into each
submodule.
"""

from app.db.models.discord import DiscordChannel
from app.db.models.entities import Creator, Dex, Launchpad, Narrative
from app.db.models.intelligence import Anomaly, Trend, TrendHistory, TrendObservation
from app.db.models.ops import (
    AuditLog,
    DataConflict,
    DataQuality,
    ProviderHealth,
    Setting,
    ToolCall,
)
from app.db.models.research import (
    Conversation,
    Memory,
    Message,
    PersonalityConfig,
    Report,
    ResearchHypothesis,
    ResearchNote,
    ResearchTask,
)
from app.db.models.tokens import (
    ImageFeature,
    MarketSnapshot,
    SocialData,
    Token,
    TokenFeature,
    TokenMilestone,
)

__all__ = [
    # discord
    "DiscordChannel",
    # entities
    "Creator",
    "Dex",
    "Launchpad",
    "Narrative",
    # tokens
    "ImageFeature",
    "MarketSnapshot",
    "SocialData",
    "Token",
    "TokenFeature",
    "TokenMilestone",
    # intelligence
    "Anomaly",
    "Trend",
    "TrendHistory",
    "TrendObservation",
    # research
    "Conversation",
    "Memory",
    "Message",
    "PersonalityConfig",
    "Report",
    "ResearchHypothesis",
    "ResearchNote",
    "ResearchTask",
    # ops
    "AuditLog",
    "DataConflict",
    "DataQuality",
    "ProviderHealth",
    "Setting",
    "ToolCall",
]
