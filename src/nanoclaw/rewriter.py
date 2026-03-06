import logging

logger = logging.getLogger(__name__)


async def rewrite_on_timeout(
    original_prompt: str,
    work_done: list[str],
    messages_sent: list[str],
    attempt: int,
) -> str:
    """Build a continuation prompt from previous attempt context.

    Combines already-delivered messages and agent work log into a focused
    prompt that tells the next agent what's done and what remains.
    """
    sections = []

    if messages_sent:
        delivered = "\n\n---\n\n".join(messages_sent)
        sections.append(f"<already_delivered>\n{delivered}\n</already_delivered>")

    # Include agent's last substantive reasoning (>100 chars), up to 3 items
    prev_texts = [t for t in work_done if len(t) > 100]
    if prev_texts:
        work_log = "\n\n---\n\n".join(prev_texts[-3:])
        sections.append(f"<previous_work>\n{work_log}\n</previous_work>")

    context_block = "\n\n".join(sections)

    # Progressive scope narrowing based on attempt number
    if attempt <= 2:
        instruction = "Continue from where the previous attempt left off."
    elif attempt <= 4:
        instruction = (
            "Previous attempts timed out repeatedly. "
            "Focus on the SINGLE most important remaining part. Be concise."
        )
    else:
        instruction = (
            "Multiple attempts have timed out. "
            "Deliver a brief summary of what you can find quickly. Skip deep analysis."
        )

    prompt = (
        f"[System: Attempt {attempt} — previous attempt timed out. "
        f"{instruction} "
        f"Do NOT repeat already-delivered results.]\n\n"
    )
    if context_block:
        prompt += f"{context_block}\n\n"
    prompt += original_prompt

    logger.info("Rewriter (attempt %d): instruction=%r", attempt, instruction[:80])
    return prompt
