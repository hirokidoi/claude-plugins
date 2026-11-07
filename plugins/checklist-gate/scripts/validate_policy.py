#!/usr/bin/env python3
"""Validate a policy.json file: validate_policy.py <path|->  (exit 0 valid, 1 invalid, 2 usage)."""

import os
import sys

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))

from policy import PolicyError, parse_policy


def main(argv: list) -> int:
    if len(argv) != 2:
        print('usage: validate_policy.py <path|->', file=sys.stderr)
        return 2
    target = argv[1]
    try:
        if target == '-':
            text = sys.stdin.read()
        else:
            with open(target, encoding='utf-8') as f:
                text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        print(f'cannot read {target}: {e}', file=sys.stderr)
        return 2
    try:
        parse_policy(text)
    except PolicyError as e:
        for error in e.errors:
            print(error)
        return 1
    print('OK')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
