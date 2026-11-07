"""Strip harness-attached blocks from recorded user prompts."""

import re

SYSTEM_TAGS = ('task-notification', 'system-reminder')
NON_AUTHORED_TAGS = SYSTEM_TAGS + ('ide_opened_file', 'ide_selection', 'pasted_content')


def strip_tags(prompt: str, tags: tuple, to_end_if_unclosed: bool = True) -> str:
    """Remove <tag ...>...</tag ...> blocks; optionally an unclosed block to the end."""
    names = '|'.join(re.escape(t) for t in tags)
    closed = re.compile(rf'<({names})\b[^>]*>.*?</\1\b[^>]*>', re.DOTALL)
    stripped = closed.sub('', prompt)
    if to_end_if_unclosed:
        stripped = re.sub(rf'<({names})\b[^>]*>.*\Z', '', stripped, flags=re.DOTALL)
    return stripped.strip()


def is_system_only(prompt: str) -> bool:
    return strip_tags(prompt, SYSTEM_TAGS, to_end_if_unclosed=False) == ''


def authored_text(prompt: str) -> str:
    """The part of the prompt the user typed themselves."""
    return strip_tags(prompt, NON_AUTHORED_TAGS)
