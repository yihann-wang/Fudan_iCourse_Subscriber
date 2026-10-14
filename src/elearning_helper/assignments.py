"""Submission and content availability are independent assignment states."""

from datetime import datetime, timezone

COMPLETED = {"submitted", "pending_review", "graded", "excused"}
AVAILABILITY_LABELS = {"upcoming": "尚未开放", "closed": "已关闭", "locked": "暂不可用"}


def availability(item, now=None):
    """Classify only explicit locks; a passed deadline is not a closed assignment."""
    if not item.get("locked_for_user"):
        return "open"
    now = now or datetime.now(timezone.utc)

    def date(key):
        try:
            value = datetime.fromisoformat(item[key].replace("Z", "+00:00"))
            return value if value.tzinfo is not None else None
        except (KeyError, ValueError, AttributeError, TypeError):
            return None

    closing = date("lock_at")
    opening = date("unlock_at")
    if closing is not None and closing <= now:
        return "closed"
    if opening is not None and opening > now:
        return "upcoming"
    return "locked"  # Prerequisites or an unknown restriction; do not guess.
