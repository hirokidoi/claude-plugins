"""policy.json loading, validation and defaults for checklist-gate."""

import copy
import json
import os
from typing import Any, List, Optional, Tuple

POLICY_FILENAME = 'policy.json'
ACK_TYPES = ('session', 'consumable', 'user-prompt-match')
DEFAULT_TYPE = 'session'
DEFAULT_MIN_REASON_LENGTH = 20
DEFAULT_MAX_PROMPT_DISTANCE = 3

MAX_LISTED_ERRORS = 10

_TOP_KEYS = ('affirm_keywords', 'ack_items', 'gates')
_ACK_KEYS = (
    'type', 'min_reason_length', 'hint', 'match_keywords', 'except_keywords',
    'max_prompt_distance', 'auto_authorize_after_gate',
)
_PROMPT_MATCH_ONLY_KEYS = (
    'match_keywords', 'except_keywords', 'max_prompt_distance', 'auto_authorize_after_gate',
)
_GATE_KEYS = ('name', 'description', 'trigger', 'require', 'enabled', 'message', 'block')
_TRIGGER_KEYS = ('patterns', 'except_patterns')


class PolicyError(Exception):
    def __init__(self, errors: List[str]) -> None:
        super().__init__('; '.join(errors))
        self.errors = list(errors)


def format_errors(errors: List[str]) -> str:
    """Bulleted error list, truncated to MAX_LISTED_ERRORS lines plus a count of the rest."""
    lines = [f'  - {e}' for e in errors[:MAX_LISTED_ERRORS]]
    if len(errors) > MAX_LISTED_ERRORS:
        lines.append(f'  ... and {len(errors) - MAX_LISTED_ERRORS} more')
    return '\n'.join(lines)


def policy_path(data_dir: str) -> str:
    return os.path.join(data_dir, POLICY_FILENAME)


def parse_tool_pattern(pattern: str) -> Tuple[str, str]:
    """'Tool(glob)' -> (tool, glob); bare 'Tool' -> (tool, '*'). Raises ValueError."""
    if not isinstance(pattern, str):
        raise ValueError('not a string')
    paren_idx = pattern.find('(')
    tool = pattern if paren_idx == -1 else pattern[:paren_idx]
    if not tool:
        raise ValueError('empty tool name')
    if any(ch.isspace() or ch in '()' for ch in tool):
        raise ValueError('tool name contains whitespace or parenthesis')
    if paren_idx == -1:
        return (tool, '*')
    if not pattern.endswith(')'):
        raise ValueError('missing closing parenthesis')
    glob = pattern[paren_idx + 1:-1]
    if glob == '':
        raise ValueError('empty pattern between parentheses')
    return (tool, glob)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ''


def _check_str_list(value: Any, where: str, errors: List[str], allow_empty: bool) -> None:
    if not isinstance(value, list):
        errors.append(f'{where}: must be an array')
        return
    if not value and not allow_empty:
        errors.append(f'{where}: must not be empty')
    for i, element in enumerate(value):
        if not _is_nonempty_str(element):
            errors.append(f'{where}[{i}]: must be a non-empty string')


def _check_keys(obj: dict, allowed: tuple, where: str, errors: List[str]) -> None:
    for key in obj:
        if key not in allowed:
            errors.append(f'{where}: unknown key "{key}"')


def _check_patterns(value: Any, where: str, errors: List[str], allow_empty: bool) -> None:
    if not isinstance(value, list):
        errors.append(f'{where}: must be an array')
        return
    if not value and not allow_empty:
        errors.append(f'{where}: must not be empty')
    for i, pattern in enumerate(value):
        loc = f'{where}[{i}]'
        if not isinstance(pattern, str):
            errors.append(f'{loc}: must be a string')
            continue
        if pattern != pattern.strip():
            errors.append(f'{loc} "{pattern}": leading or trailing whitespace')
            continue
        try:
            parse_tool_pattern(pattern)
        except ValueError as e:
            errors.append(f'{loc} "{pattern}": {e}')


def _validate_ack_item(name: str, cfg: Any, gate_names: set, errors: List[str]) -> None:
    where = f'ack_items.{name}'
    if not isinstance(cfg, dict):
        errors.append(f'{where}: must be an object')
        return
    _check_keys(cfg, _ACK_KEYS, where, errors)
    item_type = cfg.get('type', DEFAULT_TYPE)
    if item_type not in ACK_TYPES:
        errors.append(f'{where}.type: must be one of {", ".join(ACK_TYPES)}')
    if 'min_reason_length' in cfg:
        value = cfg['min_reason_length']
        if not _is_int(value) or value < 0:
            errors.append(f'{where}.min_reason_length: must be an integer >= 0')
    if 'hint' in cfg and not isinstance(cfg['hint'], str):
        errors.append(f'{where}.hint: must be a string')
    if item_type != 'user-prompt-match':
        for key in _PROMPT_MATCH_ONLY_KEYS:
            if key in cfg:
                errors.append(f'{where}.{key}: only valid for type user-prompt-match')
        return
    if 'match_keywords' not in cfg:
        errors.append(f'{where}.match_keywords: required for type user-prompt-match')
    else:
        _check_str_list(cfg['match_keywords'], f'{where}.match_keywords', errors, allow_empty=False)
    if 'except_keywords' in cfg:
        _check_str_list(cfg['except_keywords'], f'{where}.except_keywords', errors, allow_empty=True)
    if 'max_prompt_distance' in cfg:
        value = cfg['max_prompt_distance']
        if not _is_int(value) or value < 1:
            errors.append(f'{where}.max_prompt_distance: must be an integer >= 1')
    if 'auto_authorize_after_gate' in cfg:
        value = cfg['auto_authorize_after_gate']
        if not isinstance(value, str) or value not in gate_names:
            errors.append(f'{where}.auto_authorize_after_gate: must name an existing gate')


def _validate_gate(index: int, gate: Any, ack_names: set, seen: set, errors: List[str]) -> None:
    where = f'gates[{index}]'
    if not isinstance(gate, dict):
        errors.append(f'{where}: must be an object')
        return
    name = gate.get('name')
    if _is_nonempty_str(name):
        where = f'{where} ({name})'
        if name in seen:
            errors.append(f'{where}.name: duplicate gate name')
        seen.add(name)
    else:
        errors.append(f'{where}.name: required, must be a non-empty string')
    _check_keys(gate, _GATE_KEYS, where, errors)
    for key in ('description', 'message'):
        if key in gate and not isinstance(gate[key], str):
            errors.append(f'{where}.{key}: must be a string')
    for key in ('enabled', 'block'):
        if key in gate and not isinstance(gate[key], bool):
            errors.append(f'{where}.{key}: must be a boolean')
    if gate.get('block') is True:
        if gate.get('require'):
            errors.append(f'{where}.require: must be empty when block is true')
        if not _is_nonempty_str(gate.get('message')):
            errors.append(f'{where}.message: required when block is true (it is the deny reason)')
    if 'require' in gate:
        require = gate['require']
        if not isinstance(require, list):
            errors.append(f'{where}.require: must be an array')
        else:
            for i, item in enumerate(require):
                if not isinstance(item, str) or item not in ack_names:
                    errors.append(f'{where}.require[{i}]: must name an item in ack_items')
    trigger = gate.get('trigger')
    if not isinstance(trigger, dict):
        errors.append(f'{where}.trigger: required, must be an object')
        return
    _check_keys(trigger, _TRIGGER_KEYS, f'{where}.trigger', errors)
    if 'patterns' not in trigger:
        errors.append(f'{where}.trigger.patterns: required')
    else:
        _check_patterns(trigger['patterns'], f'{where}.trigger.patterns', errors, allow_empty=False)
    if 'except_patterns' in trigger:
        _check_patterns(
            trigger['except_patterns'], f'{where}.trigger.except_patterns', errors, allow_empty=True
        )


def validate(raw: Any) -> List[str]:
    """Problems found in a parsed policy.json (empty when valid)."""
    if not isinstance(raw, dict):
        return ['policy: must be a JSON object']
    errors: List[str] = []
    _check_keys(raw, _TOP_KEYS, 'policy', errors)
    if 'affirm_keywords' in raw:
        _check_str_list(raw['affirm_keywords'], 'affirm_keywords', errors, allow_empty=False)
    gates = raw.get('gates', [])
    ack_items = raw.get('ack_items', {})
    gate_names = set()
    if isinstance(gates, list):
        gate_names = {
            g['name'] for g in gates if isinstance(g, dict) and _is_nonempty_str(g.get('name'))
        }
    ack_names = set()
    if not isinstance(ack_items, dict):
        errors.append('ack_items: must be an object')
    else:
        for name, cfg in ack_items.items():
            if not _is_nonempty_str(name):
                errors.append('ack_items: item names must be non-empty strings')
                continue
            ack_names.add(name)
            _validate_ack_item(name, cfg, gate_names, errors)
    if not isinstance(gates, list):
        errors.append('gates: must be an array')
    else:
        seen: set = set()
        for i, gate in enumerate(gates):
            _validate_gate(i, gate, ack_names, seen, errors)
    return errors


def _with_defaults(raw: dict) -> dict:
    policy = copy.deepcopy(raw)
    policy.setdefault('affirm_keywords', [])
    policy.setdefault('ack_items', {})
    policy.setdefault('gates', [])
    for cfg in policy['ack_items'].values():
        cfg.setdefault('type', DEFAULT_TYPE)
        cfg.setdefault('min_reason_length', DEFAULT_MIN_REASON_LENGTH)
        cfg.setdefault('hint', '')
        if cfg['type'] == 'user-prompt-match':
            cfg.setdefault('except_keywords', [])
            cfg.setdefault('max_prompt_distance', DEFAULT_MAX_PROMPT_DISTANCE)
            cfg.setdefault('auto_authorize_after_gate', None)
    for gate in policy['gates']:
        gate.setdefault('description', '')
        gate['trigger'].setdefault('except_patterns', [])
        gate.setdefault('require', [])
        gate.setdefault('enabled', True)
        gate.setdefault('message', '')
        gate.setdefault('block', False)
    return policy


def parse_policy(text: str) -> dict:
    """Parse and validate policy.json text; returns a new dict with defaults filled in."""
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise PolicyError([f'policy: invalid JSON ({e})'])
    errors = validate(raw)
    if errors:
        raise PolicyError(errors)
    return _with_defaults(raw)


def load_policy(path: str) -> Optional[dict]:
    """None when the file does not exist; PolicyError when unreadable or invalid."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        raise PolicyError([f'policy: cannot read file ({e})'])
    return parse_policy(text)


def gates_requiring(policy: dict, item: str) -> List[str]:
    """Names of the gates that list item in require."""
    return [g['name'] for g in policy.get('gates', []) if item in g.get('require', [])]
