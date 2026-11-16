#!/usr/bin/env python3
"""SessionStart hook: Initialize session for checklist-gate plugin.

- Copies the default policy-source.md into the data directory
- Creates/opens SQLite DB (WAL mode) and marks the session active
- Cleans up old data (housekeeping)
- Resets the session acks whose reset_on lists the start source
- Outputs additionalContext with plugin usage instructions, the policy.json status and the reset

Never blocks: every step reports to stderr and the output is always produced.
"""

import json
import os
import re
import shutil
import sys
from typing import List, Optional, Tuple

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from policy import (
    PolicyError, format_errors, gates_requiring, items_reset_on, load_policy, policy_path,
)
from state import State

TEMPLATE_PATH = os.path.join(PLUGIN_DIR, 'templates', 'session-context.txt')

_NOTICE_MISSING = (
    '[checklist-gate] policy.json が未作成です。ゲートは開いたままです。'
    'edit-config → apply-config で作成してください。'
)
_NOTICE_INVALID = (
    '[checklist-gate] policy.json が不正なため、読み取り専用のツール以外はすべて止まっています。'
    'apply-config で直すか、policy-source.md を直してください。\n不正の箇所:\n{errors}'
)
_NOTICE_RESET = (
    '[checklist-gate] セッションの開始の理由（{source}）により、次の ack を取り消しました: {items}。'
    'ゲートに止められたら、各項目のヒントに沿って確認をやり直してから ack してください。'
)


def _init_data_files(data_dir: str) -> None:
    """Create the data directory and copy default config files on first run."""
    os.makedirs(data_dir, exist_ok=True)
    source_default = os.path.join(PLUGIN_DIR, 'config', 'policy-source.md')
    source_dest = os.path.join(data_dir, 'policy-source.md')
    if not os.path.isfile(source_dest) and os.path.isfile(source_default):
        shutil.copy2(source_default, source_dest)
        print(
            f'[session_init] Copied default policy-source.md to {source_dest}',
            file=sys.stderr,
        )


def _step(name: str, action) -> None:
    try:
        action()
    except Exception as e:
        print(f'[session_init] {name} failed: {type(e).__name__}: {e}', file=sys.stderr)


def _database_steps(
    session_id: str, cwd: str, reset_items: List[str], reset_gates: List[str], reset: List[str]
) -> None:
    state = State()
    try:
        state.init_schema()
        _step('touch_session', lambda: state.touch_session(session_id, cwd))
        _step('cleanup', state.cleanup)
        if reset_items:
            _step('reset acks', lambda: reset.extend(
                state.reset_session_checks(session_id, reset_items, reset_gates)
            ))
    finally:
        state.close()


def _load(data_dir: str) -> Tuple[Optional[dict], str]:
    """(policy or None, notice for a missing or invalid policy.json)."""
    try:
        policy = load_policy(policy_path(data_dir))
    except PolicyError as e:
        return None, _NOTICE_INVALID.format(errors=format_errors(e.errors))
    except Exception as e:
        return None, _NOTICE_INVALID.format(errors=f'  - {type(e).__name__}: {e}')
    return policy, (_NOTICE_MISSING if policy is None else '')


def _reset_targets(policy: Optional[dict], session_id: str, source) -> Tuple[List[str], List[str]]:
    """(items to reset, gates whose deny is cleared with them)."""
    if policy is None or not session_id or not isinstance(source, str):
        return [], []
    items = items_reset_on(policy, source)
    gates = sorted({gate for item in items for gate in gates_requiring(policy, item)})
    return items, gates


def _context_text(data_dir: str, notice: str, reset_notice: str) -> str:
    try:
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            template = f.read()
    except OSError:
        template = '[checklist-gate plugin enabled]\n{policy_notice}\n{reset_notice}'
    values = {
        'plugin_data_dir': data_dir, 'plugin_root': PLUGIN_DIR,
        'policy_notice': notice, 'reset_notice': reset_notice,
    }
    text = re.sub(r'\{(\w+)\}', lambda m: values.get(m.group(1), m.group(0)), template)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
    except ValueError:
        sys.exit(0)
    if not isinstance(hook_input, dict):
        sys.exit(0)

    session_id = hook_input.get('session_id', '')
    cwd = hook_input.get('cwd', '')
    source = hook_input.get('source')
    data_dir = os.environ['CLAUDE_PLUGIN_DATA']

    _step('init data files', lambda: _init_data_files(data_dir))
    policy, notice = _load(data_dir)
    reset_items, reset_gates = _reset_targets(policy, session_id, source)
    reset: List[str] = []
    _step('database', lambda: _database_steps(session_id, cwd, reset_items, reset_gates, reset))
    reset_notice = _NOTICE_RESET.format(source=source, items=', '.join(reset)) if reset else ''

    response = {
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': _context_text(data_dir, notice, reset_notice),
        }
    }
    if notice:
        response['systemMessage'] = notice
    json.dump(response, sys.stdout)
    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f'[session_init] Error: {e}', file=sys.stderr)
        sys.exit(0)
