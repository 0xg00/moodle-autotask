"""Safe, read-only Moodle mobile-service connector."""

from .config import MoodleConnectionConfig
from .service import MoodleService

__all__ = ["MoodleConnectionConfig", "MoodleService"]
