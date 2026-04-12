#!/usr/bin/env python3
"""PreToolUse hook: Gate check for checklist-gate plugin.

Reads hook input from stdin (JSON), evaluates gates defined in policy.json,
and outputs a structured JSON response (permissionDecision: deny/allow/defer).

Also intercepts gate-ack / gate-toggle commands and handles them internally
via updatedInput (no external CLI binary required).
"""

import json
import os
import fnmatch
import re
import shlex
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
        print(
            '[gate_check] Warning: policy.json not found. '
            'No gates are active. Use "edit-config" and "apply-config" '
            'skills to set up your policy.',
            file=sys.stderr,
        )
        return {}
    with open(policy_path, encoding='utf-8') as f:
        return json.load(f)


def _load_deny_template() -> str:
    """Load the deny-reason template."""
    path = os.path.join(PLUGIN_DIR, 'templates', 'deny-reason.txt')
    with open(path, encoding='utf-8') as f:
        return f.read()


def _build_deny_reason(
    gate_name: str,
    missing_items: list,
    ack_items_cfg: dict,
) -> str:
    """Build deny reason text from template and missing ack items."""
    template = _load_deny_template()
    ack_bin = 'gate-ack'

    require_list = '\n'.join(f'  - {item}' for item in missing_items)

    ack_commands_lines = []
    exit_commands_lines = []
    hint_lines = []
    for item in missing_items:
        cfg = ack_items_cfg.get(item, {})
        hint = cfg.get('hint', '')
        ack_commands_lines.append(
            f'  {ack_bin} {item} --reason "<concrete justification>"'
        )
        exit_commands_lines.append(
            f'  {ack_bin} {item} --reason "Confirmed absent: <explain why>"'
        )
        if hint:
            hint_lines.append(f'  {item}: {hint}')

    ack_commands = '\n'.join(ack_commands_lines)
    exit_commands = '\n'.join(exit_commands_lines)
    ack_hint = '\n'.join(hint_lines)

    result = template.replace('{gate_name}', gate_name)
    result = result.replace('{require_list}', require_list)
    result = result.replace('{ack_commands}', ack_commands)
    result = result.replace('{ack_hint}', ack_hint)
    result = result.replace('{exit_commands}', exit_commands)
    return result


def _handle_gate_command(command: str, session_id: str, state: State) -> str:
    """Parse and handle gate-ack / gate-toggle commands internally."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return 'echo "[gate-ack] Error: failed to parse command" >&2; false'

    # Find the gate-ack or gate-toggle token
    has_ack = any(t == 'gate-ack' or t.endswith('/gate-ack') for t in tokens)
    if has_ack:
        return _handle_gate_ack(tokens, command, session_id, state)
    else:
        return _handle_gate_toggle(tokens, command, session_id, state)


def _handle_gate_ack(tokens: list, command: str, session_id: str, state: State) -> str:
    """Handle gate-ack command internally."""
    # Find gate-ack position in tokens
    ack_idx = None
    for i, t in enumerate(tokens):
        if t.endswith('gate-ack') or t == 'gate-ack':
            ack_idx = i
            break
    if ack_idx is None:
        return 'echo "[gate-ack] Error: could not parse command" >&2; false'

    remaining = tokens[ack_idx + 1:]

    # Check for --help-gates
    if '--help-gates' in remaining:
        return _handle_help_gates()

    # Extract item (first non-flag argument after gate-ack)
    item = None
    reason = None
    prompt_id_str = None
    affirm_text = None
    i = 0
    while i < len(remaining):
        if remaining[i] == '--reason' and i + 1 < len(remaining):
            reason = remaining[i + 1]
            i += 2
        elif remaining[i] == '--prompt-id' and i + 1 < len(remaining):
            prompt_id_str = remaining[i + 1]
            i += 2
        elif remaining[i] == '--affirm' and i + 1 < len(remaining):
            affirm_text = remaining[i + 1]
            i += 2
        elif remaining[i] == '--session-id' and i + 1 < len(remaining):
            # Ignore --session-id if present (we use hook's session_id)
            i += 2
        elif not remaining[i].startswith('-'):
            item = remaining[i]
            i += 1
        else:
            i += 1

    if item is None:
        return 'echo "[gate-ack] Error: item is required" >&2; false'
    if reason is None:
        return 'echo "[gate-ack] Error: --reason is required" >&2; false'

    # Load policy and validate
    try:
        policy = _load_policy()
    except Exception:
        return 'echo "[gate-ack] Error: failed to load policy.json" >&2; false'

    ack_items = policy.get('ack_items', {})
    if item not in ack_items:
        available = ', '.join(sorted(ack_items.keys()))
        return f'echo "[gate-ack] Error: unknown item \\"{item}\\". Available: {available}" >&2; false'

    cfg = ack_items[item]
    min_len = cfg.get('min_reason_length', 20)
    if len(reason) < min_len:
        return f'echo "[gate-ack] Error: reason too short ({len(reason)} chars). Minimum: {min_len}" >&2; false'

    # --- deny-first check (all ack types) ---
    gates = policy.get('gates', [])
    requiring_gates = state.find_gates_requiring_item(item, gates)
    has_prior_deny = any(
        state.has_deny(session_id, gn) for gn in requiring_gates
    )
    if not has_prior_deny:
        gate_list = ', '.join(f"'{g}'" for g in requiring_gates) if requiring_gates else '(none)'
        return (
            f'echo "[gate-ack] Rejected: no prior deny for gate(s) {gate_list}. '
            f'This is an opportunity to reconsider. '
            f'Retry the operation and follow the deny message to reflect on your action." >&2; false'
        )

    # Record ack
    item_type = cfg.get('type', 'session')
    if item_type == 'session':
        state.add_session_check(session_id, item, reason)
    elif item_type == 'consumable':
        state.add_ack(session_id, item, reason)
    elif item_type == 'user-prompt-match':
        # --- user-prompt-match validation ---
        if prompt_id_str is None:
            return 'echo "[gate-ack] Error: --prompt-id is required for user-prompt-match items" >&2; false'
        try:
            prompt_id = int(prompt_id_str)
        except ValueError:
            return f'echo "[gate-ack] Error: --prompt-id must be an integer, got \\"{prompt_id_str}\\"" >&2; false'

        # 1. Prompt existence check
        user_prompt = state.get_user_prompt(prompt_id, session_id)
        if user_prompt is None:
            return f'echo "[gate-ack] Error: prompt_id {prompt_id} not found in user prompts for this session." >&2; false'

        # 2. Prompt distance check
        max_distance = cfg.get('max_prompt_distance', 3)
        if not state.is_prompt_within_distance(prompt_id, session_id, max_distance):
            oldest_valid = state.get_oldest_valid_prompt_id(session_id, max_distance)
            oldest_msg = f' (oldest valid: {oldest_valid})' if oldest_valid is not None else ''
            return (
                f'echo "[gate-ack] Error: prompt_id {prompt_id} is too old. '
                f'Only the last {max_distance} prompts are valid{oldest_msg}." >&2; false'
            )

        # 3. Keyword validation (--affirm aware)
        unified_reject = (
            'echo "[gate-ack] Rejected: insufficient grounds for authorization. '
            'Re-confirm with the user and gather the necessary context before retrying." >&2; false'
        )
        match_keywords = cfg.get('match_keywords', [])
        except_keywords = cfg.get('except_keywords', [])

        if affirm_text is not None:
            # --- Affirm flow ---
            # 3a. Check that the user prompt is an affirmative response
            affirm_keywords = policy.get('affirm_keywords', [])
            prompt_normalized = user_prompt.prompt.replace(' ', '').lower()
            if affirm_keywords:
                is_affirm = any(
                    kw.replace(' ', '').lower() in prompt_normalized
                    for kw in affirm_keywords
                )
                if not is_affirm:
                    return unified_reject

            # 3b. match_keywords check against --affirm value
            affirm_normalized = affirm_text.replace(' ', '').lower()
            if match_keywords:
                matched = any(
                    kw.replace(' ', '').lower() in affirm_normalized
                    for kw in match_keywords
                )
                if not matched:
                    return unified_reject

            # 3c. except_keywords check against --affirm value
            if except_keywords:
                for kw in except_keywords:
                    if kw.replace(' ', '').lower() in affirm_normalized:
                        return unified_reject
        else:
            # --- Normal flow ---
            # 3a. match_keywords check against user prompt
            prompt_normalized = user_prompt.prompt.replace(' ', '').lower()
            if match_keywords:
                matched = any(
                    kw.replace(' ', '').lower() in prompt_normalized
                    for kw in match_keywords
                )
                if not matched:
                    return unified_reject

            # 3b. except_keywords check against user prompt
            if except_keywords:
                for kw in except_keywords:
                    if kw.replace(' ', '').lower() in prompt_normalized:
                        return unified_reject

        # All checks passed — record as consumable ack with prompt_id
        state.add_ack(session_id, item, reason, prompt_id=prompt_id)
    else:
        return f'echo "[gate-ack] Error: unknown type \\"{item_type}\\"" >&2; false'

    # NOTE: deny is NOT cleared here. It is cleared when the gate actually
    # passes (all required items satisfied) in _consume_acks. This allows
    # gates with multiple required items to accept all acks after a single deny.

    return f'echo "[gate-ack] {item} acknowledged (type: {item_type})"'


def _handle_gate_toggle(tokens: list, command: str, session_id: str, state: State) -> str:
    """Handle gate-toggle command internally."""
    # Find gate-toggle position
    toggle_idx = None
    for i, t in enumerate(tokens):
        if t.endswith('gate-toggle') or t == 'gate-toggle':
            toggle_idx = i
            break
    if toggle_idx is None:
        return 'echo "[gate-toggle] Error: could not parse command" >&2; false'

    remaining = tokens[toggle_idx + 1:]

    # Filter out --session-id if present
    filtered = []
    i = 0
    while i < len(remaining):
        if remaining[i] == '--session-id' and i + 1 < len(remaining):
            i += 2
        else:
            filtered.append(remaining[i])
            i += 1
    remaining = filtered

    if not remaining:
        return 'echo "[gate-toggle] Error: subcommand required (on/off/list)" >&2; false'

    subcmd = remaining[0]

    try:
        policy = _load_policy()
    except Exception:
        return 'echo "[gate-toggle] Error: failed to load policy.json" >&2; false'

    gates = policy.get('gates', [])
    gate_names = [g.get('name') for g in gates]

    if subcmd in ('on', 'off'):
        if len(remaining) < 2:
            return f'echo "[gate-toggle] Error: gate name required" >&2; false'
        gate_name = remaining[1]
        if gate_name not in gate_names:
            available = ', '.join(sorted(gate_names))
            return f'echo "[gate-toggle] Error: unknown gate \\"{gate_name}\\". Available: {available}" >&2; false'
        enabled = subcmd == 'on'
        state.set_gate_toggle(session_id, gate_name, enabled=enabled)
        action = 'enabled' if enabled else 'disabled'
        return f'echo "[gate-toggle] {gate_name} {action} for this session"'

    elif subcmd == 'list':
        toggles = state.list_gate_toggles(session_id)
        toggle_map = {t.gate_name: t.enabled for t in toggles}
        lines = [f'[gate-toggle] Gate states for session {session_id}:']
        for gate in gates:
            name = gate.get('name', '?')
            policy_enabled = gate.get('enabled', True)
            if name in toggle_map:
                status = 'ON' if toggle_map[name] else 'OFF'
                label = 'toggled'
            else:
                status = 'ON' if policy_enabled else 'OFF'
                label = 'default'
            lines.append(f'  {name}: {status} ({label})')
        msg = '\\n'.join(lines)
        return f'echo "{msg}"'

    else:
        return f'echo "[gate-toggle] Error: unknown subcommand \\"{subcmd}\\"" >&2; false'


def _handle_help_gates() -> str:
    """Handle gate-ack --help-gates internally."""
    try:
        policy = _load_policy()
    except Exception:
        return 'echo "[gate-ack] Error: failed to load policy.json" >&2; false'

    ack_items = policy.get('ack_items', {})
    gates = policy.get('gates', [])

    lines = ['=== Ack Items ===']
    for name, cfg in ack_items.items():
        lines.append(f'  {name}')
        lines.append(f'    type: {cfg.get("type", "unknown")}')
        lines.append(f'    min_reason_length: {cfg.get("min_reason_length", 20)}')
        hint = cfg.get('hint', '')
        if hint:
            lines.append(f'    hint: {hint}')
    lines.append('')
    lines.append('=== Gates ===')
    for gate in gates:
        enabled = gate.get('enabled', True)
        status = 'enabled' if enabled else 'disabled'
        lines.append(f'  {gate.get("name", "?")} ({status})')
        lines.append(f'    description: {gate.get("description", "")}')
        requires = gate.get('require', [])
        if requires:
            lines.append(f'    require: {", ".join(requires)}')

    msg = '\\n'.join(lines)
    return f'echo "{msg}"'


def _deny_response(reason: str) -> None:
    """Output a deny JSON response and exit."""
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        }
    }
    print(json.dumps(response))
    sys.exit(0)


def _updated_input_response(tool_input: dict, new_command: str) -> None:
    """Output an updatedInput JSON response for session-id injection."""
    updated = dict(tool_input)
    updated['command'] = new_command
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'updatedInput': updated,
        }
    }
    print(json.dumps(response))
    sys.exit(0)


def _parse_tool_pattern(pattern: str) -> tuple:
    """Parse 'Tool(パターン)' format. Returns (tool_name, glob_pattern)."""
    paren_idx = pattern.find('(')
    if paren_idx == -1 or not pattern.endswith(')'):
        return (pattern, '*')
    tool_name = pattern[:paren_idx]
    glob_pattern = pattern[paren_idx + 1:-1]
    return (tool_name, glob_pattern)


def _split_bash_commands(command: str) -> list:
    """Split a chained bash command into individual sub-commands.

    Splits on &&, ||, ;, and newlines to handle patterns like:
    'git add foo && git commit -m "bar"'
    """
    parts = re.split(r'&&|\|\||;|\n', command)
    return [p.strip() for p in parts if p.strip()]


def _glob_match(value: str, pattern: str) -> bool:
    """Match value against a glob pattern. Also matches if value starts with pattern prefix.

    This handles cases like fnmatch('git push', 'git push *') which returns False
    because there's no trailing content to match '*'. We treat this as a match
    since 'git push' is a prefix of what 'git push *' intends to capture.
    """
    if fnmatch.fnmatch(value, pattern):
        return True
    if pattern.endswith(' *') and value == pattern[:-2]:
        return True
    return False


def _matches_tool_pattern(pattern: str, tool_name: str, tool_input: dict) -> bool:
    """Check if a tool call matches a Tool(パターン) pattern."""
    pat_tool, pat_glob = _parse_tool_pattern(pattern)
    if pat_tool != tool_name:
        return False
    if pat_glob == '*':
        return True
    # Bash: match against each sub-command (handles chained commands like git add && git commit)
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        return any(_glob_match(sub, pat_glob) for sub in _split_bash_commands(command))
    # Other tools: match against file_path, pattern, path
    for key in ('file_path', 'pattern', 'path'):
        value = tool_input.get(key)
        if isinstance(value, str) and _glob_match(value, pat_glob):
            return True
    return False


def _matches_any_pattern(patterns: list, tool_name: str, tool_input: dict) -> bool:
    """Check if any pattern in the list matches."""
    return any(_matches_tool_pattern(p, tool_name, tool_input) for p in patterns)


def _check_ack(
    state: State,
    session_id: str,
    item: str,
    ack_items_cfg: dict,
) -> bool:
    """Check if an ack item is satisfied.

    Returns True if satisfied, False if missing.
    For user-prompt-match type, also verifies that the ack's prompt_id
    is still within max_prompt_distance (prevents stale ack reuse).
    """
    cfg = ack_items_cfg.get(item, {})
    item_type = cfg.get('type', 'session')
    if item_type == 'session':
        return state.has_session_check(session_id, item)
    elif item_type == 'consumable':
        return state.has_unconsumed_ack(session_id, item)
    elif item_type == 'user-prompt-match':
        ack = state.get_oldest_unconsumed_ack(session_id, item)
        if ack is None:
            return False
        if ack.prompt_id is not None:
            max_distance = cfg.get('max_prompt_distance', 3)
            if not state.is_prompt_within_distance(
                ack.prompt_id, session_id, max_distance
            ):
                return False
        return True
    return False


def _consume_acks(
    state: State,
    session_id: str,
    gate_name: str,
    items: list,
    ack_items_cfg: dict,
) -> None:
    """Consume consumable/user-prompt-match acks that were used to pass the gate."""
    for item in items:
        cfg = ack_items_cfg.get(item, {})
        if cfg.get('type') in ('consumable', 'user-prompt-match'):
            state.consume_oldest_unconsumed_ack(session_id, item)
    # Clear deny after gate passes (all required items satisfied)
    state.clear_deny(session_id, gate_name)


def main() -> None:
    raw = sys.stdin.read()
    try:
        hook_input = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    session_id = hook_input.get('session_id', '')
    tool_name = hook_input.get('tool_name', '')
    tool_input = hook_input.get('tool_input', {})

    # --- 1. gate-ack / gate-toggle command interception (handled internally) ---
    if tool_name == 'Bash':
        command = tool_input.get('command', '')
        try:
            first_token = shlex.split(command)[0] if command.strip() else ''
        except (ValueError, IndexError):
            first_token = command.split()[0] if command.split() else ''
        is_gate_cmd = (
            first_token in ('gate-ack', 'gate-toggle')
            or first_token.endswith('/gate-ack')
            or first_token.endswith('/gate-toggle')
        )
        if is_gate_cmd:
            state = State()
            state.init_schema()
            try:
                result_cmd = _handle_gate_command(command, session_id, state)
            except Exception as e:
                safe_err = shlex.quote(str(e))
                result_cmd = f'echo "[checklist-gate] Error:" {safe_err} >&2; false'
            finally:
                state.close()
            _updated_input_response(tool_input, result_cmd)

    # --- 2. Evaluate gates ---
    state = State()
    state.init_schema()
    try:
        # Load policy
        try:
            policy = _load_policy()
        except (json.JSONDecodeError, OSError):
            sys.exit(0)

        if not policy:
            sys.exit(0)

        ack_items_cfg = policy.get('ack_items', {})
        gates = policy.get('gates', [])

        # Evaluate each gate
        for gate in gates:
            if not gate.get('enabled', True):
                continue

            gate_name = gate.get('name', '')
            if not state.is_gate_enabled(session_id, gate_name):
                continue

            trigger = gate.get('trigger', {})
            trigger_type = trigger.get('type', '')
            require = gate.get('require', [])

            if trigger_type == 'stop-time':
                continue

            if trigger_type == 'gate':
                patterns = trigger.get('patterns', [])
                except_patterns = trigger.get('except_patterns', [])

                if not _matches_any_pattern(patterns, tool_name, tool_input):
                    continue

                if except_patterns and _matches_any_pattern(except_patterns, tool_name, tool_input):
                    continue

                missing = [
                    item for item in require
                    if not _check_ack(state, session_id, item, ack_items_cfg)
                ]

                if missing:
                    # Record deny for deny-first enforcement
                    state.record_deny(session_id, gate_name)
                    reason = _build_deny_reason(gate_name, missing, ack_items_cfg)
                    _deny_response(reason)

                _consume_acks(state, session_id, gate_name, require, ack_items_cfg)
                continue
    finally:
        state.close()

    sys.exit(0)


if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f'[gate_check] Error: {e}', file=sys.stderr)
        sys.exit(0)
