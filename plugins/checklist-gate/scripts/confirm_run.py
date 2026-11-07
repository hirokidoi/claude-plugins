#!/usr/bin/env python3
"""PostToolUse hook: confirm the writes gate_check.py held for a tool call that actually ran."""

import json
import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from state import State

_NOT_RECORDED = {
    'stacked': (
        '[checklist-gate] {item} was not recorded: an unused ack for it was recorded first. '
        'Retry the operation with that ack.'
    ),
    'prompt_used': (
        '[checklist-gate] {item} was not recorded: prompt_id {prompt_id} has already been used '
        'for it. Ask the user again.'
    ),
}


def _apply(state: State, session_id: str, effect) -> str:
    """Perform one held write; returns a message for the agent when it could not be recorded."""
    payload = effect.payload
    if effect.kind == 'ack':
        if payload['type'] == 'session':
            state.add_session_check(session_id, effect.name, payload['reason'])
            return ''
        added = state.add_ack_if_allowed(
            session_id, effect.name, payload['reason'], prompt_id=payload['prompt_id']
        )
        if added == 'added':
            return ''
        return _NOT_RECORDED[added].format(item=effect.name, prompt_id=payload['prompt_id'])
    if effect.kind == 'toggle_off':
        state.set_gate_toggle(session_id, effect.name, enabled=False)
        return ''
    if effect.kind == 'gate_pass':
        state.record_gate_pass(session_id, effect.name, payload['prompt_id'])
        return ''
    raise ValueError(f'unknown held write: {effect.kind}')


def _confirm(hook_input: dict) -> list:
    session_id = hook_input.get('session_id')
    tool_use_id = hook_input.get('tool_use_id')
    if not isinstance(session_id, str) or not isinstance(tool_use_id, str) or not tool_use_id:
        return []
    state = State()
    try:
        state.init_schema()
        if not state.has_pending(session_id, tool_use_id):
            return []
        messages = []
        for effect in state.take_pending(session_id, tool_use_id):
            message = _apply(state, session_id, effect)
            if message:
                messages.append(message)
        return messages
    finally:
        state.close()


def main() -> None:
    try:
        hook_input = json.loads(sys.stdin.read())
        if not isinstance(hook_input, dict):
            raise ValueError('hook input is not a JSON object')
        messages = _confirm(hook_input)
        if messages:
            print(json.dumps({
                'hookSpecificOutput': {
                    'hookEventName': 'PostToolUse',
                    'additionalContext': '\n'.join(messages),
                }
            }))
    except Exception as e:
        print(f'[confirm_run] Error: {type(e).__name__}: {e}', file=sys.stderr)


if __name__ == '__main__':
    main()
