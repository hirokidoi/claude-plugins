#!/usr/bin/env python3
"""Stop hook: End-of-session gate check for checklist-gate plugin.

Evaluates stop-time gates defined in policy.json and blocks session
termination if conditions are not met.
"""

import json
import os
import subprocess
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from state import State


def _load_policy() -> dict:
    """Load policy.json from $CLAUDE_PLUGIN_DATA."""
    policy_path = os.path.join(
        os.environ['CLAUDE_PLUGIN_DATA'], 'policy.json'
    )
    if not os.path.isfile(policy_path):
        return {}
    with open(policy_path, encoding='utf-8') as f:
        return json.load(f)



def _check_git_uncommitted() -> tuple:
    """Check for uncommitted git changes (M/A/R/D only, ignoring untracked).

    Returns:
        (blocked: bool, reason: str)
    """
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--is-inside-work-tree'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, ''
    except (subprocess.TimeoutExpired, OSError):
        return False, ''

    try:
        result = subprocess.run(
            ['git', 'status', '--porcelain'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False, ''
    except (subprocess.TimeoutExpired, OSError):
        return False, ''

    tracked_changes = []
    for line in result.stdout.splitlines():
        if not line or len(line) < 2:
            continue
        status = line[:2].strip()
        if not status:
            continue
        first_char = status[0]
        if first_char in ('M', 'A', 'R', 'D'):
            tracked_changes.append(line)

    if not tracked_changes:
        return False, ''

    count = len(tracked_changes)
    reason = (
        f'There are uncommitted changes ({count} files modified/added/deleted). '
        'Please commit or stash before ending.'
    )
    return True, reason


def _check_unconsumed_acks(state: State, session_id: str) -> tuple:
    """Check for unconsumed acks.

    Returns:
        (blocked: bool, reason: str)
    """
    acks = state.list_unconsumed_acks(session_id)
    if not acks:
        return False, ''

    count = len(acks)
    reason = (
        f'There are {count} unconsumed ack(s) remaining. '
        'This may indicate incomplete operations.'
    )
    return True, reason


def _block_response(reason: str) -> None:
    """Output a block JSON response and exit."""
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'Stop',
            'decision': 'block',
            'reason': reason,
        }
    }
    print(json.dumps(response))
    sys.exit(0)


def main() -> None:
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = hook_input.get('session_id', '')

    # --- 1. Load policy ---
    try:
        policy = _load_policy()
    except (json.JSONDecodeError, OSError):
        sys.exit(0)

    if not policy:
        sys.exit(0)

    gates = policy.get('gates', [])

    # --- 2. Initialize state ---
    state = State()
    state.init_schema()

    # --- 3. Evaluate stop-time gates ---
    for gate in gates:
        trigger = gate.get('trigger', {})
        if trigger.get('type') != 'stop-time':
            continue

        if not gate.get('enabled', True):
            continue

        gate_name = gate.get('name', '')
        if not state.is_gate_enabled(session_id, gate_name):
            continue

        check = trigger.get('check', '')

        if check == 'git-uncommitted':
            blocked, reason = _check_git_uncommitted()
            if blocked:
                state.close()
                _block_response(reason)

        elif check == 'unconsumed-acks':
            blocked, reason = _check_unconsumed_acks(state, session_id)
            if blocked:
                state.close()
                _block_response(reason)

        # Unknown check values are silently skipped

    # --- 4. No block — pass through ---
    state.close()
    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f'[stop_gate] Error: {e}', file=sys.stderr)
        sys.exit(0)
