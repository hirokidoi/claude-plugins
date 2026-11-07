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
from typing import Optional

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from prompt_text import authored_text
from state import State


def _normalize_keyword_text(text: str) -> str:
    return text.replace(' ', '').lower()


def _contains_any(normalized_text: str, keywords: list) -> bool:
    return any(_normalize_keyword_text(kw) in normalized_text for kw in keywords)


_WORD_CHAR = r'[A-Za-z0-9_\-]'


def _keyword_matches(text: str, keywords: list):
    """Yield matches of keywords as standalone words ('web_push' does not mention 'push')."""
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        body = r'\s*'.join(re.escape(part) for part in kw.split())
        prefix = rf'(?<!{_WORD_CHAR})' if re.match(_WORD_CHAR, kw[0]) else ''
        suffix = rf'(?!{_WORD_CHAR})' if re.match(_WORD_CHAR, kw[-1]) else ''
        yield from re.finditer(prefix + body + suffix, text, re.IGNORECASE)


def _mentions_any(text: str, keywords: list) -> bool:
    return any(True for _ in _keyword_matches(text, keywords))


_NEGATION_JA = (
    'しない', 'しなく', 'ないで', 'せず', '待', '不要', 'いらない', 'やめ', 'まだ', '後で', '保留', '控え',
)
_NEGATION_JA_WINDOW = 8
_NEGATION_EN = re.compile(r"\b(don't|not|no|never)\b", re.IGNORECASE)
_NEGATION_EN_WINDOW = 12


def _negated_mention(text: str, keywords: list) -> bool:
    """True if any mention of a keyword is negated ('push しないで', "don't push", 'push is not needed')."""
    for m in _keyword_matches(text, keywords):
        after_ja = text[m.end():m.end() + _NEGATION_JA_WINDOW]
        around_en = (
            text[max(0, m.start() - _NEGATION_EN_WINDOW):m.start()]
            + ' ' + text[m.end():m.end() + _NEGATION_EN_WINDOW]
        )
        if any(word in after_ja for word in _NEGATION_JA) or _NEGATION_EN.search(around_en):
            return True
    return False


def _authorizes(text: str, cfg: dict) -> bool:
    """True if text grants a user-prompt-match item under cfg's keywords."""
    match_keywords = cfg.get('match_keywords', [])
    if match_keywords and not _mentions_any(text, match_keywords):
        return False
    if _contains_any(_normalize_keyword_text(text), cfg.get('except_keywords', [])):
        return False
    return not _negated_mention(text, match_keywords)


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
        policy = json.load(f)

    return policy


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


def _shell_message(message: str, error: bool = False) -> str:
    """A shell command that prints message verbatim, with no expansion."""
    command = f"printf '%s\\n' {shlex.quote(message)}"
    return f'{command} >&2; false' if error else command


def _handle_gate_command(command: str, session_id: str, state: State) -> str:
    """Parse and handle gate-ack / gate-toggle commands internally."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _shell_message('[gate-ack] Error: failed to parse command', error=True)

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
        return _shell_message('[gate-ack] Error: could not parse command', error=True)

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
        return _shell_message('[gate-ack] Error: item is required', error=True)
    if reason is None:
        return _shell_message('[gate-ack] Error: --reason is required', error=True)

    # Load policy and validate
    try:
        policy = _load_policy()
    except Exception:
        return _shell_message('[gate-ack] Error: failed to load policy.json', error=True)

    ack_items = policy.get('ack_items', {})
    if item not in ack_items:
        available = ', '.join(sorted(ack_items.keys()))
        return _shell_message(
            f'[gate-ack] Error: unknown item "{item}". Available: {available}', error=True
        )

    cfg = ack_items[item]
    min_len = cfg.get('min_reason_length', 20)
    if len(reason) < min_len:
        return _shell_message(
            f'[gate-ack] Error: reason too short ({len(reason)} chars). Minimum: {min_len}',
            error=True,
        )

    # --- deny-first check (all ack types) ---
    gates = policy.get('gates', [])
    requiring_gates = state.find_gates_requiring_item(item, gates)
    has_prior_deny = any(
        state.has_deny(session_id, gn) for gn in requiring_gates
    )
    if not has_prior_deny:
        gate_list = ', '.join(f"'{g}'" for g in requiring_gates) if requiring_gates else '(none)'
        return _shell_message(
            f'[gate-ack] Rejected: no prior deny for gate(s) {gate_list}. '
            f'This is an opportunity to reconsider. '
            f'Retry the operation and follow the deny message to reflect on your action.',
            error=True,
        )

    item_type = cfg.get('type', 'session')
    if item_type not in ('session', 'consumable', 'user-prompt-match'):
        return _shell_message(f'[gate-ack] Error: unknown type "{item_type}"', error=True)

    stacked_reject = _shell_message(
        f'[gate-ack] Rejected: {item} is already acknowledged and not yet used. '
        f'Retry the operation instead of acknowledging again.',
        error=True,
    )
    if item_type == 'user-prompt-match':
        _expire_stale_prompt_acks(state, session_id, item, cfg)
    if item_type != 'session' and state.has_unconsumed_ack(session_id, item):
        return stacked_reject

    if item_type == 'session':
        state.add_session_check(session_id, item, reason)
    elif item_type == 'consumable':
        if state.add_ack_if_allowed(session_id, item, reason) != 'added':
            return stacked_reject
    else:
        # --- user-prompt-match validation ---
        if prompt_id_str is None:
            return _shell_message(
                '[gate-ack] Error: --prompt-id is required for user-prompt-match items',
                error=True,
            )
        try:
            prompt_id = int(prompt_id_str)
        except ValueError:
            return _shell_message(
                f'[gate-ack] Error: --prompt-id must be an integer, got "{prompt_id_str}"',
                error=True,
            )

        # 1. Prompt existence check
        user_prompt = state.get_user_prompt(prompt_id, session_id)
        if user_prompt is None:
            return _shell_message(
                f'[gate-ack] Error: prompt_id {prompt_id} not found in user prompts for this session.',
                error=True,
            )

        # 2. Prompt distance check
        max_distance = cfg.get('max_prompt_distance', 3)
        if not state.is_prompt_within_distance(prompt_id, session_id, max_distance):
            oldest_valid = state.get_oldest_valid_prompt_id(session_id, max_distance)
            oldest_msg = f' (oldest valid: {oldest_valid})' if oldest_valid is not None else ''
            return _shell_message(
                f'[gate-ack] Error: prompt_id {prompt_id} is too old. '
                f'Only the last {max_distance} prompts are valid{oldest_msg}.',
                error=True,
            )

        # 3. One prompt authorizes one operation
        used_reject = _shell_message(
            f'[gate-ack] Rejected: prompt_id {prompt_id} has already been used for {item}. '
            f'Ask the user again.',
            error=True,
        )
        if state.has_ack_for_prompt(session_id, item, prompt_id):
            return used_reject

        # 4. Keyword validation (--affirm aware)
        unified_reject = _shell_message(
            '[gate-ack] Rejected: insufficient grounds for authorization. '
            'Re-confirm with the user and gather the necessary context before retrying.',
            error=True,
        )
        prompt_text = authored_text(user_prompt.prompt)

        if affirm_text is not None:
            # --- Affirm flow ---
            affirm_keywords = policy.get('affirm_keywords', [])
            if affirm_keywords and not _mentions_any(prompt_text, affirm_keywords):
                return unified_reject
            checked_text = affirm_text
        else:
            checked_text = prompt_text

        if not _authorizes(checked_text, cfg):
            return unified_reject

        added = state.add_ack_if_allowed(session_id, item, reason, prompt_id=prompt_id)
        if added == 'stacked':
            return stacked_reject
        if added == 'prompt_used':
            return used_reject

    # NOTE: deny is NOT cleared here. It is cleared when the gate actually
    # passes (all required items satisfied). This allows gates with multiple
    # required items to accept all acks after a single deny.

    return _shell_message(f'[gate-ack] {item} acknowledged (type: {item_type})')


def _handle_gate_toggle(tokens: list, command: str, session_id: str, state: State) -> str:
    """Handle gate-toggle command internally."""
    # Find gate-toggle position
    toggle_idx = None
    for i, t in enumerate(tokens):
        if t.endswith('gate-toggle') or t == 'gate-toggle':
            toggle_idx = i
            break
    if toggle_idx is None:
        return _shell_message('[gate-toggle] Error: could not parse command', error=True)

    remaining = tokens[toggle_idx + 1:]

    # Filter out --session-id; pick up --prompt-id
    filtered = []
    prompt_id_str = None
    i = 0
    while i < len(remaining):
        if remaining[i] == '--session-id' and i + 1 < len(remaining):
            i += 2
        elif remaining[i] == '--prompt-id' and i + 1 < len(remaining):
            prompt_id_str = remaining[i + 1]
            i += 2
        else:
            filtered.append(remaining[i])
            i += 1
    remaining = filtered

    if not remaining:
        return _shell_message('[gate-toggle] Error: subcommand required (on/off/list)', error=True)

    subcmd = remaining[0]

    try:
        policy = _load_policy()
    except Exception:
        return _shell_message('[gate-toggle] Error: failed to load policy.json', error=True)

    gates = policy.get('gates', [])
    gate_names = [g.get('name') for g in gates]

    if subcmd in ('on', 'off'):
        if len(remaining) < 2:
            return _shell_message('[gate-toggle] Error: gate name required', error=True)
        gate_name = remaining[1]
        if gate_name not in gate_names:
            available = ', '.join(sorted(gate_names))
            return _shell_message(
                f'[gate-toggle] Error: unknown gate "{gate_name}". Available: {available}',
                error=True,
            )
        enabled = subcmd == 'on'
        if not enabled:
            rejection = _verify_toggle_off(state, session_id, gate_name, prompt_id_str)
            if rejection:
                return _shell_message(rejection, error=True)
        state.set_gate_toggle(session_id, gate_name, enabled=enabled)
        action = 'enabled' if enabled else 'disabled'
        return _shell_message(f'[gate-toggle] {gate_name} {action} for this session')

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
        return _shell_message('\n'.join(lines))

    else:
        return _shell_message(f'[gate-toggle] Error: unknown subcommand "{subcmd}"', error=True)


_TOGGLE_OFF_KEYWORDS = ['off', 'オフ', '外して', '外す', '無効', 'disable', 'gate-off']
_GATE_WORDS = ['ゲート', 'gate', 'gate-off']
_TOGGLE_PROMPT_DISTANCE = 3


def _verify_toggle_off(
    state: State, session_id: str, gate_name: str, prompt_id_str: Optional[str]
) -> Optional[str]:
    """Return a rejection message unless a recent user prompt asks to turn the gate off."""
    usage = f'gate-toggle off {gate_name} --prompt-id <id>'
    if prompt_id_str is None:
        return (
            f'[gate-toggle] Rejected: turning a gate off requires the user\'s instruction. '
            f'Run "{usage}" citing the prompt in which the user asked for it.'
        )
    try:
        prompt_id = int(prompt_id_str)
    except ValueError:
        return f'[gate-toggle] Error: --prompt-id must be an integer, got "{prompt_id_str}"'
    user_prompt = state.get_user_prompt(prompt_id, session_id)
    if user_prompt is None or not state.is_prompt_within_distance(
        prompt_id, session_id, _TOGGLE_PROMPT_DISTANCE
    ):
        return (
            f'[gate-toggle] Rejected: prompt_id {prompt_id} is not one of the last '
            f'{_TOGGLE_PROMPT_DISTANCE} user prompts.'
        )
    text = authored_text(user_prompt.prompt)
    if not (
        _authorizes(text, {'match_keywords': _TOGGLE_OFF_KEYWORDS})
        and _mentions_any(text, [gate_name] + _GATE_WORDS)
    ):
        return (
            '[gate-toggle] Rejected: the cited prompt does not ask to turn the gate off. '
            'Ask the user before disabling a gate.'
        )
    return None


def _handle_help_gates() -> str:
    """Handle gate-ack --help-gates internally."""
    try:
        policy = _load_policy()
    except Exception:
        return _shell_message('[gate-ack] Error: failed to load policy.json', error=True)

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

    return _shell_message('\n'.join(lines))


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


def _nudge_response(message: str) -> None:
    """Output an additionalContext response for nudge injection and exit."""
    response = {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'additionalContext': message,
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


_ENV_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_GIT_OPTIONS_WITH_ARG = {
    '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env',
    '--attr-source',
}


def _canonical_command(sub: str) -> str:
    """Normalize a sub-command so patterns match on the command and subcommand.

    'FOO=1 /usr/bin/git -C ~/repo --no-pager push origin' -> 'git push origin'
    """
    try:
        tokens = shlex.split(sub)
    except ValueError:
        tokens = sub.split()
    while tokens and _ENV_ASSIGNMENT.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return ''
    head = os.path.basename(tokens[0])
    rest = tokens[1:]
    if head == 'git':
        while rest and rest[0].startswith('-'):
            opt = rest.pop(0)
            if opt in _GIT_OPTIONS_WITH_ARG and rest:
                rest.pop(0)
    return ' '.join([head] + rest)


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


def _matches_bash_pattern(pat_glob: str, sub: str) -> bool:
    return _glob_match(sub, pat_glob) or _glob_match(_canonical_command(sub), pat_glob)


def _matches_tool_pattern(pattern: str, tool_name: str, tool_input: dict) -> bool:
    """Check if a tool call matches a Tool(パターン) pattern."""
    pat_tool, pat_glob = _parse_tool_pattern(pattern)
    if pat_tool != tool_name:
        return False
    if pat_glob == '*':
        return True
    # Bash: match against each sub-command (handles chained commands like git add && git commit)
    if tool_name == 'Bash':
        subs = _split_bash_commands(tool_input.get('command', ''))
        return any(_matches_bash_pattern(pat_glob, sub) for sub in subs)
    # Other tools: match against file_path, pattern, path
    for key in ('file_path', 'pattern', 'path'):
        value = tool_input.get(key)
        if isinstance(value, str) and _glob_match(value, pat_glob):
            return True
    return False


def _matches_any_pattern(patterns: list, tool_name: str, tool_input: dict) -> bool:
    """Check if any pattern in the list matches."""
    return any(_matches_tool_pattern(p, tool_name, tool_input) for p in patterns)


def _gate_applies(trigger: dict, tool_name: str, tool_input: dict) -> bool:
    """For Bash, the gate applies if any one sub-command matches and is not excepted."""
    patterns = trigger.get('patterns', [])
    except_patterns = trigger.get('except_patterns', [])
    if tool_name != 'Bash':
        return (
            _matches_any_pattern(patterns, tool_name, tool_input)
            and not _matches_any_pattern(except_patterns, tool_name, tool_input)
        )
    subs = _split_bash_commands(tool_input.get('command', '')) or ['']
    return any(
        _matches_any_pattern(patterns, 'Bash', {'command': sub})
        and not _matches_any_pattern(except_patterns, 'Bash', {'command': sub})
        for sub in subs
    )


def _expire_stale_prompt_acks(state: State, session_id: str, item: str, cfg: dict) -> None:
    """Mark unconsumed acks whose prompt fell out of max_prompt_distance as used."""
    max_distance = cfg.get('max_prompt_distance', 3)
    while True:
        ack = state.get_oldest_unconsumed_ack(session_id, item)
        if ack is None or ack.prompt_id is None or state.is_prompt_within_distance(
            ack.prompt_id, session_id, max_distance
        ):
            return
        state.consume_oldest_unconsumed_ack(session_id, item)


def _check_ack(
    state: State,
    session_id: str,
    item: str,
    ack_items_cfg: dict,
) -> bool:
    """Check if an ack item is satisfied."""
    cfg = ack_items_cfg.get(item, {})
    item_type = cfg.get('type', 'session')
    if item_type == 'session':
        return state.has_session_check(session_id, item)
    elif item_type == 'consumable':
        return state.has_unconsumed_ack(session_id, item)
    elif item_type == 'user-prompt-match':
        _expire_stale_prompt_acks(state, session_id, item, cfg)
        return state.has_unconsumed_ack(session_id, item)
    return False


def _find_auto_authorization(
    state: State, session_id: str, item: str, cfg: dict,
    passing_now: set, latest_prompt_id: Optional[int],
) -> Optional[int]:
    """The prompt_id that authorizes item without gate-ack, or None. Writes nothing."""
    after_gate = cfg.get('auto_authorize_after_gate')
    if not after_gate:
        return None
    if after_gate in passing_now:
        pass_prompt_id = latest_prompt_id
    else:
        pass_prompt_id = state.get_gate_pass_prompt_id(session_id, after_gate)
    if pass_prompt_id is None:
        return None
    match_keywords = cfg.get('match_keywords', [])
    max_distance = cfg.get('max_prompt_distance', 3)
    for prompt in state.get_recent_prompts(session_id, max_distance):
        text = authored_text(prompt.prompt)
        if not _mentions_any(text, match_keywords):
            continue
        if (
            not _authorizes(text, cfg)
            or state.has_ack_for_prompt(session_id, item, prompt.id)
            or pass_prompt_id < prompt.id
        ):
            return None
        return prompt.id
    return None


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
                result_cmd = _shell_message(f'[checklist-gate] Error: {e}', error=True)
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
        pass_tracked_gates = {
            cfg.get('auto_authorize_after_gate')
            for cfg in ack_items_cfg.values()
            if cfg.get('auto_authorize_after_gate')
        }

        nudge_messages = []
        nudge_firings = []
        passing_gates = []
        latest_prompt_id = state.get_latest_prompt_id(session_id)

        # Evaluate each gate
        for gate in gates:
            if not gate.get('enabled', True):
                continue

            gate_name = gate.get('name', '')
            if not state.is_gate_enabled(session_id, gate_name):
                continue

            require = gate.get('require', [])
            if not _gate_applies(gate.get('trigger', {}), tool_name, tool_input):
                continue

            # Nudge gate: no require acks, has message → inject context once per turn.
            # Recorded only after every gate has passed, so a denied call does not use it up.
            message = gate.get('message', '')
            if not require and message:
                # prompt_id=None cannot be stored in nudge_firings, so we pass silently.
                if latest_prompt_id is not None and not state.was_nudge_fired_this_turn(
                    session_id, gate_name, latest_prompt_id
                ):
                    nudge_firings.append(gate_name)
                    nudge_messages.append(message)
                continue

            missing = [
                item for item in require
                if not _check_ack(state, session_id, item, ack_items_cfg)
            ]

            still_missing = []
            granted = []
            passing_now = {name for name, _, _ in passing_gates}
            for item in missing:
                cfg = ack_items_cfg.get(item, {})
                auto_prompt_id = None
                if cfg.get('type') == 'user-prompt-match':
                    auto_prompt_id = _find_auto_authorization(
                        state, session_id, item, cfg, passing_now, latest_prompt_id
                    )
                if auto_prompt_id is None:
                    still_missing.append(item)
                else:
                    granted.append((item, auto_prompt_id, cfg['auto_authorize_after_gate']))

            if still_missing:
                # Record deny for deny-first enforcement
                state.record_deny(session_id, gate_name)
                reason = _build_deny_reason(gate_name, still_missing, ack_items_cfg)
                _deny_response(reason)

            passing_gates.append((gate_name, require, granted))

        # Consume only after every gate has passed, and atomically against parallel calls
        to_consume = [
            (gate_name, item)
            for gate_name, require, granted in passing_gates
            for item in require
            if ack_items_cfg.get(item, {}).get('type') in ('consumable', 'user-prompt-match')
            and item not in {g[0] for g in granted}
        ]
        grants = [
            (item, prompt_id, f'auto-authorized: prompt_id={prompt_id}, {after_gate} passed afterwards')
            for _, _, granted in passing_gates
            for item, prompt_id, after_gate in granted
        ]
        if (to_consume or grants) and not state.consume_acks(
            session_id, [item for _, item in to_consume], grants
        ):
            # A parallel call used an ack first: deny the gate that now lacks it
            for gate_name, require, granted in passing_gates:
                granted_items = {item for item, _, _ in granted}
                lacking = [
                    item for item in require
                    if item not in granted_items and not _check_ack(state, session_id, item, ack_items_cfg)
                ] + [
                    item for item, prompt_id, _ in granted
                    if state.has_ack_for_prompt(session_id, item, prompt_id)
                ]
                if lacking:
                    state.record_deny(session_id, gate_name)
                    _deny_response(_build_deny_reason(gate_name, lacking, ack_items_cfg))
            gate_name, require, _ = passing_gates[0]
            state.record_deny(session_id, gate_name)
            _deny_response(_build_deny_reason(gate_name, require, ack_items_cfg))

        for gate_name, _, granted in passing_gates:
            for item, prompt_id, _ in granted:
                nudge_messages.append(
                    f'[checklist-gate: {gate_name}] {item} auto-authorized by prompt_id={prompt_id}'
                )

        for gate_name, _, _ in passing_gates:
            state.clear_deny(session_id, gate_name)
            if gate_name in pass_tracked_gates and latest_prompt_id is not None:
                state.record_gate_pass(session_id, gate_name, latest_prompt_id)
        for gate_name in nudge_firings:
            state.record_nudge_firing(session_id, gate_name, latest_prompt_id)

        if nudge_messages:
            _nudge_response('\n'.join(nudge_messages))
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
