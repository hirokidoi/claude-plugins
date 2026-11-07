"""Strip harness-attached blocks from recorded user prompts."""

import re

SYSTEM_TAGS = ('task-notification', 'system-reminder')
NON_AUTHORED_TAGS = SYSTEM_TAGS + ('ide_opened_file', 'ide_selection', 'pasted_content')


def strip_tags(prompt: str, tags: tuple) -> str:
    """Remove <tag ...>...</tag ...> blocks; an unclosed block is removed to the end."""
    names = '|'.join(re.escape(t) for t in tags)
    closed = re.compile(rf'<({names})\b[^>]*>.*?</\1\b[^>]*>', re.DOTALL)
    unclosed = re.compile(rf'<({names})\b[^>]*>.*\Z', re.DOTALL)
    return unclosed.sub('', closed.sub('', prompt)).strip()


def is_system_only(prompt: str) -> bool:
    return strip_tags(prompt, SYSTEM_TAGS) == ''


def authored_text(prompt: str) -> str:
    """The part of the prompt the user typed themselves."""
    return strip_tags(prompt, NON_AUTHORED_TAGS)
