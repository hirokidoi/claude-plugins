"""policy.json validation, defaults and loading."""
import copy
import json
import os
import tempfile
import unittest

from tests.hook_harness import example_policy, gate_of, POLICY_EXAMPLE, USER_POLICY
from policy import (
    ACK_TYPES, DEFAULT_MAX_PROMPT_DISTANCE, DEFAULT_MIN_REASON_LENGTH, DEFAULT_TYPE,
    PolicyError, format_errors, gates_requiring, load_policy, MAX_LISTED_ERRORS, parse_policy,
    parse_tool_pattern, validate,
)


def broken(mutate) -> list:
    """Validation errors of the example policy after mutate(policy)."""
    policy = copy.deepcopy(example_policy())
    mutate(policy)
    return validate(policy)


class TestBundledAndUserPolicy(unittest.TestCase):

    def test_bundled_example_is_valid(self):
        with open(POLICY_EXAMPLE, encoding='utf-8') as f:
            policy = parse_policy(f.read())
        self.assertEqual(policy['ack_items']['docs_checked']['type'], 'session')

    @unittest.skipUnless(os.path.isfile(USER_POLICY), 'no user policy.json in this environment')
    def test_user_policy_is_valid(self):
        self.assertIsNotNone(load_policy(USER_POLICY))

    def test_example_validates_with_no_errors(self):
        self.assertEqual(validate(example_policy()), [])


class TestDefaults(unittest.TestCase):

    def test_empty_policy_gets_all_top_level_keys(self):
        self.assertEqual(
            parse_policy('{}'), {'affirm_keywords': [], 'ack_items': {}, 'gates': []}
        )

    def test_ack_item_defaults(self):
        policy = parse_policy(json.dumps({'ack_items': {'a': {}}}))
        self.assertEqual(
            policy['ack_items']['a'],
            {'type': DEFAULT_TYPE, 'min_reason_length': DEFAULT_MIN_REASON_LENGTH, 'hint': ''},
        )

    def test_prompt_match_item_defaults(self):
        policy = parse_policy(json.dumps(
            {'ack_items': {'a': {'type': 'user-prompt-match', 'match_keywords': ['x']}}}
        ))
        cfg = policy['ack_items']['a']
        self.assertEqual(cfg['except_keywords'], [])
        self.assertEqual(cfg['max_prompt_distance'], DEFAULT_MAX_PROMPT_DISTANCE)
        self.assertIsNone(cfg['auto_authorize_after_gate'])

    def test_gate_defaults(self):
        policy = parse_policy(json.dumps(
            {'gates': [{'name': 'g', 'trigger': {'patterns': ['Bash(ls *)']}}]}
        ))
        self.assertEqual(policy['gates'][0], {
            'name': 'g', 'description': '', 'require': [], 'enabled': True, 'message': '',
            'block': False, 'trigger': {'patterns': ['Bash(ls *)'], 'except_patterns': []},
        })

    def test_explicit_values_are_kept(self):
        policy = parse_policy(json.dumps({'ack_items': {'a': {'min_reason_length': 0}}}))
        self.assertEqual(policy['ack_items']['a']['min_reason_length'], 0)

    def test_parse_returns_a_new_object(self):
        raw = {'ack_items': {'a': {}}}
        parse_policy(json.dumps(raw))
        self.assertEqual(raw, {'ack_items': {'a': {}}})


class TestInvalidPolicies(unittest.TestCase):
    """Each broken policy reports a location, not just 'invalid'."""

    def assertReports(self, errors: list, *fragments: str) -> None:
        self.assertTrue(errors, 'expected validation errors')
        text = '\n'.join(errors)
        for fragment in fragments:
            self.assertIn(fragment, text)

    def test_json_syntax(self):
        with self.assertRaises(PolicyError) as ctx:
            parse_policy('{"gates": [')
        self.assertIn('JSON', ctx.exception.errors[0])

    def test_not_an_object(self):
        self.assertReports(validate([]), 'policy')
        self.assertReports(validate('x'), 'policy')

    def test_unknown_top_level_key(self):
        self.assertReports(broken(lambda p: p.update(extra=1)), 'extra')

    def test_unknown_keys_in_each_level(self):
        self.assertReports(
            broken(lambda p: p['ack_items']['docs_checked'].update(typo=1)),
            'ack_items.docs_checked', 'typo',
        )
        self.assertReports(
            broken(lambda p: p['gates'][0].update(typo=1)), 'gates[0]', 'typo',
        )
        self.assertReports(
            broken(lambda p: p['gates'][0]['trigger'].update(except_pattern=[])),
            'trigger', 'except_pattern',
        )

    def test_wrong_types(self):
        self.assertReports(broken(lambda p: p.update(gates={})), 'gates')
        self.assertReports(broken(lambda p: p.update(ack_items=[])), 'ack_items')
        self.assertReports(broken(lambda p: p.update(affirm_keywords='OK')), 'affirm_keywords')
        self.assertReports(
            broken(lambda p: p['ack_items']['docs_checked'].update(hint=1)), 'hint',
        )
        self.assertReports(
            broken(lambda p: gate_of(p, 'task_start').update(enabled='yes')), 'enabled',
        )
        self.assertReports(
            broken(lambda p: gate_of(p, 'task_start').update(description=1)), 'description',
        )
        self.assertReports(
            broken(lambda p: p['gates'].append('x')), f'gates[{len(example_policy()["gates"])}]',
        )

    def test_unknown_ack_type(self):
        self.assertReports(
            broken(lambda p: p['ack_items']['docs_checked'].update(type='sticky')),
            'ack_items.docs_checked.type',
        )

    def test_gate_without_name_or_trigger(self):
        self.assertReports(broken(lambda p: gate_of(p, 'task_start').pop('name')), 'name')
        self.assertReports(broken(lambda p: gate_of(p, 'task_start').pop('trigger')), 'trigger')
        self.assertReports(
            broken(lambda p: gate_of(p, 'task_start')['trigger'].pop('patterns')), 'patterns',
        )

    def test_empty_patterns(self):
        self.assertReports(
            broken(lambda p: gate_of(p, 'task_start')['trigger'].update(patterns=[])), 'patterns',
        )

    def test_require_of_unknown_item(self):
        self.assertReports(
            broken(lambda p: gate_of(p, 'task_start').update(require=['nope'])), 'task_start', 'require',
        )

    def test_duplicate_gate_names(self):
        self.assertReports(
            broken(lambda p: gate_of(p, 'git_push_gate').update(name='task_start')),
            'duplicate', 'task_start',
        )

    def test_bad_patterns_are_located(self):
        for bad in ('Bash(git push *) ', ' Bash(ls)', 'Bash(', '(ls)', 'Ba sh(ls)', 'Bash()', 'Bash(ls'):
            with self.subTest(pattern=bad):
                errors = broken(
                    lambda p: gate_of(p, 'git_push_gate')['trigger']['patterns'].append(bad)
                )
                self.assertReports(errors, 'git_push_gate', 'trigger.patterns[1]')

    def test_bad_except_pattern_is_located(self):
        errors = broken(
            lambda p: gate_of(p, 'git_push_gate')['trigger']['except_patterns'].append('Bash(x) ')
        )
        self.assertReports(errors, 'git_push_gate', 'except_patterns[1]')

    def test_prompt_match_item_rules(self):
        push = lambda p: p['ack_items']['user_authorized_push']
        self.assertReports(broken(lambda p: push(p).update(match_keywords=[])), 'match_keywords')
        self.assertReports(broken(lambda p: push(p).pop('match_keywords')), 'match_keywords')
        self.assertReports(broken(lambda p: push(p).update(match_keywords=[''])), 'match_keywords')
        self.assertReports(broken(lambda p: push(p).update(except_keywords='x')), 'except_keywords')
        self.assertReports(broken(lambda p: push(p).update(max_prompt_distance=0)), 'max_prompt_distance')
        self.assertReports(broken(lambda p: push(p).update(max_prompt_distance=-1)), 'max_prompt_distance')
        self.assertReports(broken(lambda p: push(p).update(max_prompt_distance=True)), 'max_prompt_distance')
        self.assertReports(
            broken(lambda p: push(p).update(auto_authorize_after_gate='no_such_gate')),
            'auto_authorize_after_gate',
        )

    def test_min_reason_length_rules(self):
        docs = lambda p: p['ack_items']['docs_checked']
        self.assertReports(broken(lambda p: docs(p).update(min_reason_length=True)), 'min_reason_length')
        self.assertReports(broken(lambda p: docs(p).update(min_reason_length=-1)), 'min_reason_length')
        self.assertReports(broken(lambda p: docs(p).update(min_reason_length='20')), 'min_reason_length')
        self.assertReports(broken(lambda p: docs(p).update(min_reason_length=1.5)), 'min_reason_length')

    def test_prompt_match_keys_on_other_types(self):
        for key, value in (
            ('match_keywords', ['x']), ('except_keywords', []), ('max_prompt_distance', 2),
            ('auto_authorize_after_gate', 'task_start'),
        ):
            with self.subTest(key=key):
                errors = broken(lambda p: p['ack_items']['commit_ready'].update({key: value}))
                self.assertReports(errors, 'commit_ready', key)

    def test_block_gate_rules(self):
        sudo = lambda p: gate_of(p, 'sudo_block')
        self.assertReports(broken(lambda p: sudo(p).update(block='yes')), 'sudo_block', '.block:')
        self.assertReports(
            broken(lambda p: sudo(p).update(require=['docs_checked'])), 'sudo_block', '.require:',
        )
        self.assertReports(broken(lambda p: sudo(p).pop('message')), 'sudo_block', '.message:')
        self.assertReports(broken(lambda p: sudo(p).update(message=' ')), 'sudo_block', '.message:')

    def test_block_false_needs_no_message(self):
        self.assertEqual(broken(lambda p: gate_of(p, 'git_push_gate').update(block=False)), [])

    def test_all_problems_are_listed_at_once(self):
        def mutate(p):
            p['extra'] = 1
            p['ack_items']['docs_checked']['type'] = 'bad'
            gate_of(p, 'task_start')['require'] = ['nope']
        self.assertGreaterEqual(len(broken(mutate)), 3)

    def test_ack_types_cover_the_example(self):
        used = {c['type'] for c in example_policy()['ack_items'].values()}
        self.assertEqual(used, set(ACK_TYPES))


class TestParseToolPattern(unittest.TestCase):

    def test_forms(self):
        self.assertEqual(parse_tool_pattern('Bash(git push *)'), ('Bash', 'git push *'))
        self.assertEqual(parse_tool_pattern('Agent'), ('Agent', '*'))
        self.assertEqual(parse_tool_pattern('Edit(*)'), ('Edit', '*'))
        self.assertEqual(parse_tool_pattern('Bash(echo (a))'), ('Bash', 'echo (a)'))

    def test_invalid_forms_raise(self):
        for bad in ('', '(x)', 'Bash(', 'Bash(x', 'Bash()', 'Ba sh(x)', 'Bash)x(', 5):
            with self.subTest(pattern=bad):
                with self.assertRaises(ValueError):
                    parse_tool_pattern(bad)


class TestLoadPolicy(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, 'policy.json')

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_is_none(self):
        self.assertIsNone(load_policy(self.path))

    def test_valid_file(self):
        with open(self.path, 'w') as f:
            f.write('{}')
        self.assertEqual(load_policy(self.path)['gates'], [])

    def test_invalid_file_raises_with_errors(self):
        with open(self.path, 'w') as f:
            f.write('{"gates": 1}')
        with self.assertRaises(PolicyError) as ctx:
            load_policy(self.path)
        self.assertIn('gates', ctx.exception.errors[0])

    def test_unreadable_content_raises(self):
        with open(self.path, 'wb') as f:
            f.write(b'\xff\xfe\x00garbage')
        with self.assertRaises(PolicyError):
            load_policy(self.path)

    def test_directory_in_place_of_file_raises(self):
        os.mkdir(self.path)
        with self.assertRaises(PolicyError):
            load_policy(self.path)


class TestHelpers(unittest.TestCase):

    def test_gates_requiring(self):
        policy = parse_policy(json.dumps(example_policy()))
        self.assertEqual(gates_requiring(policy, 'user_authorized_push'), ['git_push_gate'])
        self.assertEqual(gates_requiring(policy, 'unknown'), [])

    def test_format_errors_truncates(self):
        errors = [f'e{i}' for i in range(MAX_LISTED_ERRORS + 3)]
        text = format_errors(errors)
        self.assertIn('e0', text)
        self.assertNotIn(f'e{MAX_LISTED_ERRORS}', text)
        self.assertIn('3 more', text)
        self.assertNotIn('more', format_errors(errors[:2]))


if __name__ == '__main__':
    unittest.main()
