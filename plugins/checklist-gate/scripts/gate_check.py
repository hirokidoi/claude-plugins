#!/usr/bin/env python3
"""PreToolUse hook: Gate check for checklist-gate plugin.

Reads hook input from stdin (JSON), evaluates gates defined in policy.json,
and outputs a structured JSON response (permissionDecision deny, additionalContext
or updatedInput). Silence with exit code 0 means the call passes.

Also intercepts gate-ack / gate-toggle commands and handles them internally
via updatedInput (no external CLI binary required).

Anything abnormal blocks the call when a gate that has require may apply.
"""

import functools
import json
import os
import re
import shlex
import sys
from typing import List, Optional

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from policy import (
    PolicyError, format_errors, gates_requiring, load_policy, parse_policy, parse_tool_pattern,
    policy_path,
)
from prompt_text import authored_text
from shell_parse import OPAQUE, command_forms
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
    match_keywords = cfg['match_keywords']
    if not _mentions_any(text, match_keywords):
        return False
    if _contains_any(_normalize_keyword_text(text), cfg.get('except_keywords', [])):
        return False
    return not _negated_mention(text, match_keywords)


# --- Policy access ---

_READ_ONLY_TOOLS = ('Read', 'Glob', 'Grep', 'Skill', 'ToolSearch')
_POLICY_WRITE_TOOLS = ('Edit', 'Write', 'MultiEdit')


def _data_dir() -> str:
    return os.environ['CLAUDE_PLUGIN_DATA']


def _policy_for_command(label: str):
    """(policy, None) or (None, shell message) for gate-ack / gate-toggle."""
    try:
        policy = load_policy(policy_path(_data_dir()))
    except PolicyError as e:
        return None, _shell_message(
            f'[{label}] Error: policy.json is invalid:\n{format_errors(e.errors)}',
            error=True,
        )
    if policy is None:
        return None, _shell_message(
            f'[{label}] Error: policy.json not found. '
            'Ask the user to create it with the edit-config and apply-config skills.',
            error=True,
        )
    return policy, None


# --- Deny message ---


def _load_template(name: str) -> str:
    with open(os.path.join(PLUGIN_DIR, 'templates', name), encoding='utf-8') as f:
        return f.read()


def _fill(template: str, values: dict) -> str:
    """Substitute {name} placeholders in a single pass."""
    return re.sub(r'\{(\w+)\}', lambda m: values.get(m.group(1), m.group(0)), template)


def _build_deny_reason(
    gate_name: str,
    missing_items: list,
    ack_items_cfg: dict,
) -> str:
    """Build deny reason text from template and missing ack items."""
    ack_bin = 'gate-ack'

    require_list = '\n'.join(f'  - {item}' for item in missing_items)

    ack_commands_lines = []
    exit_commands_lines = []
    hint_lines = []
    has_prompt_match = False
    for item in missing_items:
        cfg = ack_items_cfg[item]
        if cfg['type'] == 'user-prompt-match':
            has_prompt_match = True
            ack_commands_lines.append(
                f'  {ack_bin} {item} --prompt-id <id> --reason "<concrete justification>"'
            )
        else:
            ack_commands_lines.append(
                f'  {ack_bin} {item} --reason "<concrete justification>"'
            )
            exit_commands_lines.append(
                f'  {ack_bin} {item} --reason "Confirmed absent: <explain why>"'
            )
        if cfg['hint']:
            hint_lines.append(f'  {item}: {cfg["hint"]}')

    exit_section = ''
    if exit_commands_lines:
        exit_section = _fill(
            _load_template('deny-exit.txt'), {'exit_commands': '\n'.join(exit_commands_lines)}
        )
    prompt_match_section = _load_template('deny-prompt-match.txt') if has_prompt_match else ''

    result = _fill(_load_template('deny-reason.txt'), {
        'gate_name': gate_name,
        'require_list': require_list,
        'ack_commands': '\n'.join(ack_commands_lines),
        'ack_hint': '\n'.join(hint_lines),
        'exit_section': exit_section,
        'prompt_match_section': prompt_match_section,
    })
    return re.sub(r'\n{3,}', '\n\n', result).strip() + '\n'


# --- gate-ack / gate-toggle ---


def _shell_message(message: str, error: bool = False) -> str:
    """A shell command that prints message verbatim, with no expansion."""
    command = f"printf '%s\\n' {shlex.quote(message)}"
    return f'{command} >&2; false' if error else command


def _parse_gate_args(tokens: list, value_options: tuple, flag_options: tuple):
    """Split tokens into (positionals, options); an unknown option raises ValueError."""
    positionals: list = []
    options: dict = {}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in value_options:
            if i + 1 >= len(tokens):
                raise ValueError(f'option "{token}" requires a value')
            options[token] = tokens[i + 1]
            i += 2
        elif token in flag_options:
            options[token] = True
            i += 1
        elif token.startswith('-') and len(token) > 1:
            raise ValueError(f'unknown option "{token}"')
        else:
            positionals.append(token)
            i += 1
    return positionals, options


def _first_word(command: str) -> str:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    return os.path.basename(tokens[0]) if tokens else ''


def _handle_gate_command(
    command: str, head: str, session_id: str, state: State, tool_use_id: Optional[str]
) -> str:
    """Parse and handle gate-ack / gate-toggle commands internally."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _shell_message(f'[{head}] Error: failed to parse command', error=True)
    if head == 'gate-ack':
        return _handle_gate_ack(tokens[1:], session_id, state, tool_use_id)
    return _handle_gate_toggle(tokens[1:], session_id, state, tool_use_id)


_NO_TOOL_USE_ID = (
    'Error: the hook input has no tool_use_id, so this cannot be recorded. '
    'Tell the user; the Claude Code version may be unsupported.'
)


def _handle_gate_ack(args: list, session_id: str, state: State, tool_use_id: Optional[str]) -> str:
    """Handle gate-ack command internally."""
    try:
        positionals, options = _parse_gate_args(
            args, ('--reason', '--prompt-id', '--affirm'), ('--help-gates',)
        )
    except ValueError as e:
        return _shell_message(f'[gate-ack] Error: {e}', error=True)

    if '--help-gates' in options:
        if positionals:
            return _shell_message('[gate-ack] Error: --help-gates takes no item', error=True)
        return _handle_help_gates()

    if not positionals:
        return _shell_message('[gate-ack] Error: item is required', error=True)
    if len(positionals) > 1:
        return _shell_message(
            f'[gate-ack] Error: exactly one item is expected, got {len(positionals)}', error=True
        )
    item = positionals[0]
    reason = options.get('--reason')
    prompt_id_str = options.get('--prompt-id')
    affirm_text = options.get('--affirm')
    if reason is None:
        return _shell_message('[gate-ack] Error: --reason is required', error=True)

    policy, error = _policy_for_command('gate-ack')
    if error:
        return error

    ack_items = policy['ack_items']
    if item not in ack_items:
        available = ', '.join(sorted(ack_items.keys()))
        return _shell_message(
            f'[gate-ack] Error: unknown item "{item}". Available: {available}', error=True
        )

    cfg = ack_items[item]
    min_len = cfg['min_reason_length']
    if len(reason) < min_len:
        return _shell_message(
            f'[gate-ack] Error: reason too short ({len(reason)} chars). Minimum: {min_len}',
            error=True,
        )

    # --- deny-first check (all ack types) ---
    requiring_gates = gates_requiring(policy, item)
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

    item_type = cfg['type']
    stacked_reject = _shell_message(
        f'[gate-ack] Rejected: {item} is already acknowledged and not yet used. '
        f'Retry the operation instead of acknowledging again.',
        error=True,
    )
    if item_type == 'user-prompt-match':
        _expire_stale_prompt_acks(state, session_id, item, cfg)
    if item_type != 'session' and state.has_unconsumed_ack(session_id, item):
        return stacked_reject

    prompt_id = None
    if item_type == 'user-prompt-match':
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
        max_distance = cfg['max_prompt_distance']
        recent_ids = [p.id for p in state.get_recent_prompts(session_id, max_distance)]
        if prompt_id not in recent_ids:
            oldest_msg = f' (oldest valid: {min(recent_ids)})' if recent_ids else ''
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
            affirm_keywords = policy['affirm_keywords']
            if not affirm_keywords:
                return _shell_message(
                    '[gate-ack] Rejected: --affirm is unavailable because affirm_keywords '
                    'is empty in policy.json.',
                    error=True,
                )
            if not _mentions_any(prompt_text, affirm_keywords):
                return unified_reject
            checked_text = affirm_text
        else:
            checked_text = prompt_text

        if not _authorizes(checked_text, cfg):
            return unified_reject

    if not tool_use_id:
        return _shell_message(f'[gate-ack] {_NO_TOOL_USE_ID}', error=True)
    state.add_pending(
        session_id, tool_use_id, 'ack', item,
        {'type': item_type, 'reason': reason, 'prompt_id': prompt_id},
    )

    # NOTE: deny is NOT cleared here. It is cleared when the gate actually
    # passes (all required items satisfied). This allows gates with multiple
    # required items to accept all acks after a single deny.

    return _shell_message(f'[gate-ack] {item} acknowledged (type: {item_type})')


def _handle_gate_toggle(
    args: list, session_id: str, state: State, tool_use_id: Optional[str]
) -> str:
    """Handle gate-toggle command internally."""
    try:
        remaining, options = _parse_gate_args(args, ('--prompt-id',), ())
    except ValueError as e:
        return _shell_message(f'[gate-toggle] Error: {e}', error=True)
    prompt_id_str = options.get('--prompt-id')

    if not remaining:
        return _shell_message('[gate-toggle] Error: subcommand required (on/off/list)', error=True)
    if len(remaining) > 2:
        return _shell_message(
            f'[gate-toggle] Error: too many arguments ({len(remaining)})', error=True
        )

    subcmd = remaining[0]

    policy, error = _policy_for_command('gate-toggle')
    if error:
        return error

    gates = policy['gates']
    gate_names = [g['name'] for g in gates]

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
        gate = next(g for g in gates if g['name'] == gate_name)
        if not enabled and gate['block']:
            return _shell_message(
                f'[gate-toggle] Rejected: {gate_name} is a block gate, so it cannot be turned off '
                f'for a session. Only the user can change it, in policy-source.md.',
                error=True,
            )
        if enabled and not gate['enabled']:
            return _shell_message(
                f'[gate-toggle] Rejected: {gate_name} is disabled in policy.json, so it cannot be '
                f'turned on for a session. Enable it in policy-source.md and run apply-config.',
                error=True,
            )
        if enabled:
            state.set_gate_toggle(session_id, gate_name, enabled=True)
        else:
            rejection = _verify_toggle_off(state, session_id, gate_name, prompt_id_str)
            if rejection:
                return _shell_message(rejection, error=True)
            if not tool_use_id:
                return _shell_message(f'[gate-toggle] {_NO_TOOL_USE_ID}', error=True)
            state.add_pending(session_id, tool_use_id, 'toggle_off', gate_name, {})
        action = 'enabled' if enabled else 'disabled'
        return _shell_message(f'[gate-toggle] {gate_name} {action} for this session')

    elif subcmd == 'list':
        toggles = state.list_gate_toggles(session_id)
        toggle_map = {t.gate_name: t.enabled for t in toggles}
        lines = [f'[gate-toggle] Gate states for session {session_id}:']
        for gate in gates:
            name = gate['name']
            if gate['block'] and gate['enabled']:
                status = 'ON'
                label = 'block'
            elif name in toggle_map:
                status = 'ON' if toggle_map[name] else 'OFF'
                label = 'toggled'
            else:
                status = 'ON' if gate['enabled'] else 'OFF'
                label = 'default'
            lines.append(f'  {name}: {status} ({label})')
        return _shell_message('\n'.join(lines))

    else:
        return _shell_message(f'[gate-toggle] Error: unknown subcommand "{subcmd}"', error=True)


_TOGGLE_OFF_KEYWORDS = ['off', 'オフ', '外して', '外す', '無効', 'disable', 'gate-off']
_GATE_WORDS = ['ゲート', 'gate', 'gate-off']
_APPLY_CONFIG_KEYWORDS = ['設定', '反映', 'apply-config']
_INSTRUCTION_PROMPT_DISTANCE = 3


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
    recent_ids = [p.id for p in state.get_recent_prompts(session_id, _INSTRUCTION_PROMPT_DISTANCE)]
    if user_prompt is None or prompt_id not in recent_ids:
        return (
            f'[gate-toggle] Rejected: prompt_id {prompt_id} is not one of the last '
            f'{_INSTRUCTION_PROMPT_DISTANCE} user prompts.'
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
    policy, error = _policy_for_command('gate-ack')
    if error:
        return error

    lines = ['=== Ack Items ===']
    for name, cfg in policy['ack_items'].items():
        lines.append(f'  {name}')
        lines.append(f'    type: {cfg["type"]}')
        lines.append(f'    min_reason_length: {cfg["min_reason_length"]}')
        if cfg['hint']:
            lines.append(f'    hint: {cfg["hint"]}')
    lines.append('')
    lines.append('=== Gates ===')
    for gate in policy['gates']:
        status = 'enabled' if gate['enabled'] else 'disabled'
        lines.append(f'  {gate["name"]} ({status})')
        lines.append(f'    description: {gate["description"]}')
        if gate['require']:
            lines.append(f'    require: {", ".join(gate["require"])}')
        if gate['block']:
            lines.append('    block: cannot be acked or turned off')

    return _shell_message('\n'.join(lines))


# --- Hook responses ---


def _deny_output(reason: str) -> dict:
    return {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'permissionDecision': 'deny',
            'permissionDecisionReason': reason,
        }
    }


def _block_reason(blocking: list, unreadable: bool) -> str:
    lines = [f'[checklist-gate: {gate["name"]} blocked] {gate["message"]}' for gate in blocking]
    lines.append(
        'This gate blocks the operation unconditionally: gate-ack and gate-toggle cannot lift it. '
        'Follow the message above, and do not rewrite the command to get around the gate.'
    )
    if unreadable:
        lines.append(
            'The gate could not read the command (for example, an unclosed quote), so it was '
            'treated as matching. If the command does not do this, rewrite it in a plainer form.'
        )
    return '\n'.join(lines)


def _nudge_output(message: str) -> dict:
    return {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'additionalContext': message,
        }
    }


def _updated_input_output(tool_input: dict, new_command: str) -> dict:
    """Replace the Bash command with new_command."""
    updated = dict(tool_input)
    updated['command'] = new_command
    return {
        'hookSpecificOutput': {
            'hookEventName': 'PreToolUse',
            'updatedInput': updated,
        }
    }


# --- Matching ---

_TOOL_ALIASES = {'Monitor': 'Bash', 'NotebookEdit': 'Edit'}
_SHELL_TOOLS = ('Bash', 'Monitor')
_PATH_KEYS = ('file_path', 'notebook_path', 'path')


@functools.lru_cache(maxsize=None)
def _glob_regex(glob: str):
    """'*' is the only wildcard; case-insensitive; a trailing ' *' also matches no arguments."""
    tail = ''
    if glob.endswith(' *'):
        glob, tail = glob[:-2], '(?: .*)?'
    body = ''.join('.*' if ch == '*' else re.escape(ch) for ch in glob)
    return re.compile(body + tail, re.IGNORECASE | re.DOTALL)


def _glob_match(value: str, glob: str) -> bool:
    return _glob_regex(glob).fullmatch(value) is not None


def _value_groups(tool_name: str, tool_input: dict) -> List[List[str]]:
    """Candidate strings to match: one group per simple command for shell tools, else one group."""
    if tool_name in _SHELL_TOOLS:
        command = tool_input.get('command', '')
        forms = command_forms(command if isinstance(command, str) else '')
        return [list(form) for form in forms] or [['']]
    values = [
        os.path.normpath(tool_input[key]) for key in _PATH_KEYS
        if isinstance(tool_input.get(key), str)
    ]
    if isinstance(tool_input.get('pattern'), str):
        values.append(tool_input['pattern'])
    if tool_name == 'Workflow' and isinstance(tool_input.get('name'), str):
        values.append(tool_input['name'])
    return [values]


def _matches_pattern(pattern: str, tool_name: str, group: List[str]) -> bool:
    """Check a Tool(glob) pattern against the tool (or its alias) and one group of values."""
    pat_glob = parse_tool_pattern(pattern)[1]
    if not _tool_matches(pattern, tool_name):
        return False
    if pat_glob == '*':
        return True
    return any(_glob_match(value, pat_glob) for value in group)


def _matches_any(patterns: list, tool_name: str, group: List[str]) -> bool:
    return any(_matches_pattern(p, tool_name, group) for p in patterns)


def _tool_matches(pattern: str, tool_name: str) -> bool:
    pat_tool = parse_tool_pattern(pattern)[0]
    names = [tool_name] + ([_TOOL_ALIASES[tool_name]] if tool_name in _TOOL_ALIASES else [])
    return any(_glob_match(name, pat_tool) for name in names)


def _gate_applies(trigger: dict, tool_name: str, tool_input: dict) -> bool:
    """A shell call applies if any one simple command matches and is not excepted."""
    for group in _value_groups(tool_name, tool_input):
        if OPAQUE in group:
            if any(_tool_matches(p, tool_name) for p in trigger['patterns']):
                return True
            continue
        if _matches_any(trigger['patterns'], tool_name, group) and not _matches_any(
            trigger['except_patterns'], tool_name, group
        ):
            return True
    return False


# --- policy.json protection ---


def _targets_policy(tool_name: str, tool_input: dict, data_dir: str) -> bool:
    if tool_name not in _POLICY_WRITE_TOOLS:
        return False
    path = tool_input.get('file_path')
    if not isinstance(path, str) or not path:
        return False
    target = os.path.realpath(os.path.expanduser(path))
    return target.lower() == os.path.realpath(policy_path(data_dir)).lower()


def _policy_write_rejection(
    state: State, session_id: str, tool_name: str, tool_input: dict, data_dir: str
) -> Optional[str]:
    """Reason to block a write to policy.json, or None when the user asked for apply-config."""
    asked = False
    for prompt in state.get_recent_prompts(session_id, _INSTRUCTION_PROMPT_DISTANCE):
        text = authored_text(prompt.prompt)
        if _mentions_any(text, _APPLY_CONFIG_KEYWORDS):
            asked = _authorizes(text, {'match_keywords': _APPLY_CONFIG_KEYWORDS})
            break
    if not asked:
        return (
            '[checklist-gate] policy.json is protected. It may be written only by apply-config, '
            'at the user\'s request. Ask the user to request it.'
        )
    if tool_name == 'Write':
        content = tool_input.get('content')
        try:
            parse_policy(content if isinstance(content, str) else '')
        except PolicyError as e:
            return (
                '[checklist-gate] The content is not a valid policy.json:\n'
                f'{format_errors(e.errors)}\nFix it and write again.'
            )
    return None


# --- Gate evaluation ---


def _expire_stale_prompt_acks(state: State, session_id: str, item: str, cfg: dict) -> None:
    """Mark unconsumed acks whose prompt fell out of max_prompt_distance as used."""
    recent = state.get_recent_prompts(session_id, cfg['max_prompt_distance'])
    state.expire_prompt_acks_before(
        session_id, item, min(p.id for p in recent) if recent else None
    )


def _check_ack(
    state: State,
    session_id: str,
    item: str,
    ack_items_cfg: dict,
) -> bool:
    """Check if an ack item is satisfied."""
    cfg = ack_items_cfg[item]
    item_type = cfg['type']
    if item_type == 'session':
        return state.has_session_check(session_id, item)
    if item_type == 'user-prompt-match':
        _expire_stale_prompt_acks(state, session_id, item, cfg)
    return state.has_unconsumed_ack(session_id, item)


def _find_auto_authorization(
    state: State, session_id: str, item: str, cfg: dict,
    passing_now: set, latest_prompt_id: Optional[int],
) -> Optional[int]:
    """The prompt_id that authorizes item without gate-ack, or None. Writes nothing."""
    after_gate = cfg['auto_authorize_after_gate']
    if not after_gate:
        return None
    if after_gate in passing_now:
        pass_prompt_id = latest_prompt_id
    else:
        pass_prompt_id = state.get_gate_pass_prompt_id(session_id, after_gate)
    if pass_prompt_id is None:
        return None
    for prompt in state.get_recent_prompts(session_id, cfg['max_prompt_distance']):
        text = authored_text(prompt.prompt)
        if not _mentions_any(text, cfg['match_keywords']):
            continue
        if (
            not _authorizes(text, cfg)
            or state.has_ack_for_prompt(session_id, item, prompt.id)
            or pass_prompt_id < prompt.id
        ):
            return None
        return prompt.id
    return None


def _evaluate_gates(
    policy: dict, applicable: list, session_id: str, tool_use_id: Optional[str]
) -> Optional[dict]:
    """Evaluate the gates that apply against the DB; returns the hook response or None."""
    ack_items_cfg = policy['ack_items']
    pass_tracked_gates = {
        cfg['auto_authorize_after_gate']
        for cfg in ack_items_cfg.values()
        if cfg.get('auto_authorize_after_gate')
    }
    state = State()
    try:
        state.init_schema()
        nudge_messages = []
        nudge_firings = []
        passing_gates = []
        latest_prompt_id = state.get_latest_prompt_id(session_id)

        for gate in applicable:
            gate_name = gate['name']
            if not state.is_gate_enabled(session_id, gate_name):
                continue

            require = gate['require']

            # Nudge gate: no require acks, has message → inject context once per turn.
            # Recorded only after every gate has passed, so a denied call does not use it up.
            message = gate['message']
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
                cfg = ack_items_cfg[item]
                auto_prompt_id = None
                if cfg['type'] == 'user-prompt-match':
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
                return _deny_output(_build_deny_reason(gate_name, still_missing, ack_items_cfg))

            passing_gates.append((gate_name, require, granted))

        # Consume only after every gate has passed, and atomically against parallel calls
        to_consume = [
            (gate_name, item)
            for gate_name, require, granted in passing_gates
            for item in require
            if ack_items_cfg[item]['type'] in ('consumable', 'user-prompt-match')
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
                    return _deny_output(_build_deny_reason(gate_name, lacking, ack_items_cfg))
            gate_name, require, _ = passing_gates[0]
            state.record_deny(session_id, gate_name)
            return _deny_output(_build_deny_reason(gate_name, require, ack_items_cfg))

        for gate_name, _, granted in passing_gates:
            for item, prompt_id, _ in granted:
                nudge_messages.append(
                    f'[checklist-gate: {gate_name}] {item} auto-authorized by prompt_id={prompt_id}'
                )

        for gate_name, _, _ in passing_gates:
            state.clear_deny(session_id, gate_name)
            if gate_name in pass_tracked_gates and latest_prompt_id is not None and tool_use_id:
                state.add_pending(
                    session_id, tool_use_id, 'gate_pass', gate_name, {'prompt_id': latest_prompt_id}
                )
        for gate_name in nudge_firings:
            state.record_nudge_firing(session_id, gate_name, latest_prompt_id)

        if nudge_messages:
            return _nudge_output('\n'.join(nudge_messages))
        return None
    finally:
        state.close()


def _handle_gate_command_call(
    tool_input: dict, head: str, session_id: str, tool_use_id: Optional[str]
) -> dict:
    """Run gate-ack / gate-toggle; any failure becomes an error message for the agent."""
    state = None
    try:
        state = State()
        state.init_schema()
        result = _handle_gate_command(tool_input['command'], head, session_id, state, tool_use_id)
    except Exception as e:
        result = _shell_message(
            f'[checklist-gate] Error: {type(e).__name__}: {e}. Retry the command.', error=True
        )
    finally:
        if state is not None:
            state.close()
    return _updated_input_output(tool_input, result)


def _decide(ctx: dict) -> Optional[dict]:
    """The hook response for this call; ctx['must_block'] tells main how to treat an exception."""
    hook_input = json.loads(sys.stdin.read())
    if not isinstance(hook_input, dict):
        raise ValueError('hook input is not a JSON object')
    session_id = hook_input.get('session_id', '')
    tool_name = hook_input.get('tool_name', '')
    tool_input = hook_input.get('tool_input', {})
    if not isinstance(tool_input, dict):
        raise ValueError('tool_input is not a JSON object')
    tool_use_id = hook_input.get('tool_use_id')
    if not isinstance(tool_use_id, str):
        tool_use_id = None

    # --- 1. gate-ack / gate-toggle command interception (handled internally) ---
    if tool_name == 'Bash' and isinstance(tool_input.get('command'), str):
        head = _first_word(tool_input['command'])
        if head in ('gate-ack', 'gate-toggle'):
            return _handle_gate_command_call(tool_input, head, session_id, tool_use_id)

    # --- 2. policy.json protection ---
    data_dir = _data_dir()
    protected_write = False
    if _targets_policy(tool_name, tool_input, data_dir):
        state = State()
        try:
            state.init_schema()
            rejection = _policy_write_rejection(state, session_id, tool_name, tool_input, data_dir)
        finally:
            state.close()
        if rejection:
            return _deny_output(rejection)
        protected_write = True

    # --- 3. Load policy ---
    try:
        policy = load_policy(policy_path(data_dir))
    except PolicyError as e:
        if protected_write or tool_name in _READ_ONLY_TOOLS:
            return None
        return _deny_output(
            '[checklist-gate] policy.json is invalid, so every tool except read-only ones is '
            'blocked until it is fixed:\n'
            f'{format_errors(e.errors)}\n'
            'Ask the user to run apply-config, or to fix policy-source.md / policy.json.'
        )
    if policy is None:
        return None

    # --- 4. Which gates apply (no DB needed) ---
    applicable = [
        gate for gate in policy['gates']
        if gate['enabled'] and _gate_applies(gate['trigger'], tool_name, tool_input)
    ]
    if not applicable:
        return None
    blocking = [gate for gate in applicable if gate['block']]
    if blocking:
        unreadable = any(OPAQUE in group for group in _value_groups(tool_name, tool_input))
        return _deny_output(_block_reason(blocking, unreadable))
    ctx['must_block'] = any(gate['require'] for gate in applicable)

    # --- 5. Evaluate against the DB ---
    return _evaluate_gates(policy, applicable, session_id, tool_use_id)


def main() -> None:
    ctx = {'must_block': True}
    try:
        try:
            response = _decide(ctx)
        except Exception as e:
            if not ctx['must_block']:
                return
            response = _deny_output(
                f'[checklist-gate] Internal error ({type(e).__name__}: {e}). '
                'This call was blocked because a gate may apply. '
                'Please retry the same operation; if it keeps failing, tell the user.'
            )
        if response is not None:
            print(json.dumps(response))
            sys.stdout.flush()
    except BaseException as e:
        print(f'[gate_check] Error: could not report the decision: {e}', file=sys.stderr)
        sys.stderr.flush()
        # Skip the interpreter's final stdout flush, which would fail again and exit 120
        os._exit(2)


if __name__ == '__main__':
    main()
