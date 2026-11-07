#!/usr/bin/env python3
"""SessionStart hook: Initialize session for checklist-gate plugin.

- Copies the default policy-source.md into the data directory
- Creates/opens SQLite DB (WAL mode) and marks the session active
- Cleans up old data (housekeeping)
- Outputs additionalContext with plugin usage instructions and the policy.json status

Never blocks: every step reports to stderr and the output is always produced.
"""

import json
import os
import re
import shutil
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from policy import PolicyError, format_errors, load_policy, policy_path
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


def _database_steps(session_id: str, cwd: str) -> None:
    state = State()
    try:
        state.init_schema()
        _step('touch_session', lambda: state.touch_session(session_id, cwd))
        _step('cleanup', state.cleanup)
    finally:
        state.close()


def _policy_notice(data_dir: str) -> str:
    try:
        policy = load_policy(policy_path(data_dir))
    except PolicyError as e:
        return _NOTICE_INVALID.format(errors=format_errors(e.errors))
    except Exception as e:
        return _NOTICE_INVALID.format(errors=f'  - {type(e).__name__}: {e}')
    return _NOTICE_MISSING if policy is None else ''


def _context_text(data_dir: str, notice: str) -> str:
    try:
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            template = f.read()
    except OSError:
        template = '[checklist-gate plugin enabled]\n{policy_notice}'
    values = {'plugin_data_dir': data_dir, 'plugin_root': PLUGIN_DIR, 'policy_notice': notice}
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
    data_dir = os.environ['CLAUDE_PLUGIN_DATA']

    _step('init data files', lambda: _init_data_files(data_dir))
    _step('database', lambda: _database_steps(session_id, cwd))
    notice = _policy_notice(data_dir)

    response = {
        'hookSpecificOutput': {
            'hookEventName': 'SessionStart',
            'additionalContext': _context_text(data_dir, notice),
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
