"""Conversation archiving for long-term memory."""

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nanoclaw.config import WORKSPACE_DIR

logger = logging.getLogger(__name__)

CONVERSATIONS_DIR = WORKSPACE_DIR / "conversations"


def ensure_conversations_dir() -> None:
    """Create conversations directory if it doesn't exist."""
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _get_today_file() -> Path:
    """Get the conversation file for today."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return CONVERSATIONS_DIR / f"{today}.md"


def _parse_exchanges(text: str) -> list[tuple[str, str]]:
    """Parse markdown conversation file into (user, assistant) pairs."""
    blocks = re.split(r"^---\s*$", text, flags=re.MULTILINE)
    exchanges = []
    for block in blocks:
        user_match = re.search(r"\*\*User\*\*:\s*(.+?)(?=\n\*\*Ape\*\*:|\Z)", block, re.DOTALL)
        ape_match = re.search(r"\*\*Ape\*\*:\s*(.+?)(?=\n---|\Z)", block, re.DOTALL)
        if user_match and ape_match:
            exchanges.append((user_match.group(1).strip(), ape_match.group(1).strip()))
    return exchanges


def get_recent_history(max_exchanges: int = 10) -> str:
    """Load recent exchanges from conversation files and return as formatted text."""
    if not CONVERSATIONS_DIR.exists():
        return ""

    now = datetime.now(timezone.utc)
    exchanges: list[tuple[str, str]] = []

    # Check today and yesterday
    for days_ago in range(2):
        date_str = (now - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        filepath = CONVERSATIONS_DIR / f"{date_str}.md"
        if filepath.exists():
            try:
                content = filepath.read_text(encoding="utf-8")
                parsed = _parse_exchanges(content)
                exchanges = parsed + exchanges if days_ago > 0 else parsed
            except Exception:
                logger.exception(f"Failed to read {filepath}")

    if not exchanges:
        return ""

    # Take the last N exchanges
    recent = exchanges[-max_exchanges:]
    lines = []
    for user_msg, ape_msg in recent:
        lines.append(f"User: {user_msg}")
        lines.append(f"Assistant: {ape_msg}")
        lines.append("")
    return "\n".join(lines).strip()


async def archive_exchange(user_message: str, assistant_response: str, chat_id: int) -> None:
    """Archive a single user-assistant exchange to today's conversation file.

    Format:
    ## HH:MM:SS UTC

    **User**: <message>

    **Ape**: <response>

    ---
    """
    ensure_conversations_dir()

    filepath = _get_today_file()
    timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

    # Build the exchange entry
    entry = f"""## {timestamp}

**User**: {user_message}

**Ape**: {assistant_response}

---

"""

    # Append to file (create if doesn't exist)
    try:
        if filepath.exists():
            content = filepath.read_text(encoding="utf-8")
        else:
            # Create file with header
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            content = f"# Conversations - {date_str}\n\n"

        content += entry
        filepath.write_text(content, encoding="utf-8")
        logger.debug(f"Archived exchange to {filepath}")
    except Exception:
        logger.exception(f"Failed to archive exchange to {filepath}")
