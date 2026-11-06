"""Long-note handling before text goes into a Discord embed.

Summaries are design-only for now: `NoopSummarizer` is the only implementation, so long text
is truncated with a link to the full note. To turn on LLM summaries later, implement
`Summarizer` (or store a precomputed `summary` on the task/announcement, e.g. from a background
agent) and nothing else in the pipeline changes.
"""

from typing import Protocol

READ_MORE = "Read the full note"


class Summarizer(Protocol):
    def summarize(self, text: str, *, max_chars: int) -> str | None:
        """Return a summary of at most `max_chars`, or None to fall back to truncation."""


class NoopSummarizer:
    def summarize(self, text: str, *, max_chars: int) -> str | None:
        return None


def condense(
    text: str,
    *,
    link: str,
    threshold: int,
    cached_summary: str | None = None,
    summarizer: Summarizer | None = None,
) -> str:
    """Return `text` unchanged if short; otherwise a summary or a truncation, plus a link to the full note."""
    text = text.strip()
    if len(text) <= threshold:
        return text

    summary = cached_summary or (summarizer or NoopSummarizer()).summarize(text, max_chars=threshold)
    if summary:
        return f"{summary.strip()[:threshold]}\n\n[{READ_MORE}]({link})"

    cut = text[:threshold]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return f"{cut.rstrip()}…\n\n[{READ_MORE}]({link})"
