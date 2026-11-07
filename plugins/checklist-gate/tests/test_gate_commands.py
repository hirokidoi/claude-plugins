"""gate-ack and gate-toggle, run through the hook as the agent calls them."""
import unittest

from tests.hook_harness import (
    GATE_CHECK, HookTestCase, SESSION, assert_denied, assert_passed_silently, bash_call,
    example_policy, gate_of, run_hook, write_policy,
)

REASON = 'x' * 40
PUSH_ITEM = 'user_authorized_push'


class TestGateAck(HookTestCase):

    def ack(self, args: str) -> str:
        return self.gate_command(f'gate-ack {args}')

    def test_ack_before_any_deny_is_rejected(self):
        output = self.ack(f'commit_ready --reason {REASON}')
        self.assertIn('no prior deny', output)
        self.assertEqual(self.unconsumed('commit_ready'), 0)
        assert_denied(self.gate('git commit -m x'))

    def test_ack_for_every_type_needs_a_prior_deny(self):
        pid = self.add_prompt('push して')
        for args in (
            f'docs_checked --reason {REASON}',
            f'{PUSH_ITEM} --prompt-id {pid} --reason {REASON}',
        ):
            with self.subTest(args=args):
                self.assertIn('no prior deny', self.ack(args))

    def test_ack_after_deny_is_accepted_and_the_retry_passes(self):
        output = self.deny_then_ack('git_commit_gate', 'git commit -m x', 'commit_ready')
        self.assertIn('acknowledged', output)
        assert_passed_silently(self.gate('git commit -m x'))

    def test_deny_of_another_gate_does_not_count(self):
        assert_denied(self.gate('git push'))
        self.assertIn('no prior deny', self.ack(f'commit_ready --reason {REASON}'))

    def test_reason_shorter_than_the_minimum_is_rejected(self):
        assert_denied(self.gate('git commit -m x'))
        minimum = example_policy()['ack_items']['commit_ready']['min_reason_length']
        output = self.ack(f'commit_ready --reason {"x" * (minimum - 1)}')
        self.assertIn('too short', output)
        self.assertEqual(self.unconsumed('commit_ready'), 0)
        self.assertIn('acknowledged', self.ack(f'commit_ready --reason {"x" * minimum}'))

    def test_prompt_outside_the_distance_is_rejected(self):
        old = self.add_prompt('push して')
        distance = example_policy()['ack_items'][PUSH_ITEM]['max_prompt_distance']
        for _ in range(distance):
            self.add_prompt('OK')
        assert_denied(self.gate('git push'))
        output = self.ack(f'{PUSH_ITEM} --prompt-id {old} --reason {REASON}')
        self.assertIn('too old', output)
        self.assertEqual(self.unconsumed(PUSH_ITEM), 0)

    def test_prompt_at_the_edge_of_the_distance_is_accepted(self):
        pid = self.add_prompt('push して')
        distance = example_policy()['ack_items'][PUSH_ITEM]['max_prompt_distance']
        for _ in range(distance - 1):
            self.add_prompt('OK')
        output = self.deny_then_ack('git_push_gate', 'git push', PUSH_ITEM, prompt_id=pid)
        self.assertIn('acknowledged', output)

    def test_unknown_prompt_id_is_rejected(self):
        self.add_prompt('push して')
        assert_denied(self.gate('git push'))
        self.assertIn('not found', self.ack(f'{PUSH_ITEM} --prompt-id 99999 --reason {REASON}'))

    def test_prompt_id_is_required_for_prompt_match_items(self):
        assert_denied(self.gate('git push'))
        self.assertIn('--prompt-id is required', self.ack(f'{PUSH_ITEM} --reason {REASON}'))

    def test_prompt_id_must_be_an_integer(self):
        assert_denied(self.gate('git push'))
        self.assertIn('integer', self.ack(f'{PUSH_ITEM} --prompt-id abc --reason {REASON}'))

    def test_prompt_without_the_keyword_is_rejected(self):
        pid = self.add_prompt('テストを直して')
        output = self.deny_then_ack('git_push_gate', 'git push', PUSH_ITEM, prompt_id=pid)
        self.assertIn('Rejected', output)
        self.assertEqual(self.unconsumed(PUSH_ITEM), 0)

    def test_unknown_option_is_an_error(self):
        assert_denied(self.gate('git commit -m x'))
        output = self.ack(f'commit_ready --session-id abc --reason {REASON}')
        self.assertIn('unknown option', output)
        self.assertIn('--session-id', output)
        self.assertEqual(self.unconsumed('commit_ready'), 0)

    def test_positional_argument_count_is_checked(self):
        self.assertIn('item is required', self.ack(f'--reason {REASON}'))
        self.assertIn('exactly one item', self.ack(f'commit_ready docs_checked --reason {REASON}'))

    def test_option_without_a_value_is_an_error(self):
        self.assertIn('requires a value', self.ack('commit_ready --reason'))

    def test_reason_is_required(self):
        assert_denied(self.gate('git commit -m x'))
        self.assertIn('--reason is required', self.ack('commit_ready'))

    def test_unknown_item_lists_the_available_ones(self):
        output = self.ack(f'nope --reason {REASON}')
        self.assertIn('unknown item', output)
        for item in example_policy()['ack_items']:
            self.assertIn(item, output)

    def test_help_gates_lists_items_and_gates(self):
        output = self.ack('--help-gates')
        for name in list(example_policy()['ack_items']) + [g['name'] for g in example_policy()['gates']]:
            self.assertIn(name, output)

    def test_help_gates_takes_no_item(self):
        self.assertIn('takes no item', self.ack('--help-gates commit_ready'))

    def test_only_the_first_word_selects_the_command(self):
        assert_passed_silently(self.gate('echo gate-ack foo'))
        assert_passed_silently(self.gate('ls gate-toggle'))

    def test_command_is_found_by_the_basename_of_its_first_word(self):
        output = self.gate_command('/usr/local/bin/gate-ack --help-gates')
        self.assertIn('Ack Items', output)

    def test_unparseable_command_reports_an_error(self):
        output = self.gate_command("gate-ack 'unterminated")
        self.assertIn('failed to parse', output)


class TestAffirm(HookTestCase):

    def test_affirmative_answer_to_a_question_with_the_keyword_is_accepted(self):
        pid = self.add_prompt('OK')
        output = self.deny_then_ack(
            'git_push_gate', 'git push', PUSH_ITEM, prompt_id=pid, affirm='"push しますか？"',
        )
        self.assertIn('acknowledged', output)

    def test_affirm_without_affirm_keywords_in_the_policy_is_rejected(self):
        policy = example_policy()
        del policy['affirm_keywords']
        write_policy(self.data_dir, policy)
        pid = self.add_prompt('OK')
        output = self.deny_then_ack(
            'git_push_gate', 'git push', PUSH_ITEM, prompt_id=pid, affirm='"push しますか？"',
        )
        self.assertIn('affirm_keywords', output)
        self.assertEqual(self.unconsumed(PUSH_ITEM), 0)

    def test_non_affirmative_prompt_is_rejected(self):
        pid = self.add_prompt('うーん')
        output = self.deny_then_ack(
            'git_push_gate', 'git push', PUSH_ITEM, prompt_id=pid, affirm='"push しますか？"',
        )
        self.assertIn('Rejected', output)


class TestGateToggle(HookTestCase):

    def toggle(self, args: str) -> str:
        return self.gate_command(f'gate-toggle {args}')

    def test_list_shows_defaults_and_toggled_gates(self):
        output = self.toggle('list')
        for gate in example_policy()['gates']:
            self.assertIn(gate['name'], output)
        self.assertIn('git_push_gate: ON (default)', output)
        self.state.set_gate_toggle(SESSION, 'git_push_gate', enabled=False)
        self.assertIn('git_push_gate: OFF (toggled)', self.toggle('list'))

    def test_off_needs_the_users_instruction(self):
        self.assertIn('Rejected', self.toggle('off git_push_gate'))
        assert_denied(self.gate('git push'))

    def test_off_on_the_users_instruction_lets_the_operation_through(self):
        pid = self.add_prompt('git_push_gate を一旦外して')
        self.assertIn('disabled', self.toggle(f'off git_push_gate --prompt-id {pid}'))
        assert_passed_silently(self.gate('git push'))

    def test_on_restores_the_gate_without_instruction(self):
        self.state.set_gate_toggle(SESSION, 'git_push_gate', enabled=False)
        self.assertIn('enabled', self.toggle('on git_push_gate'))
        assert_denied(self.gate('git push'))

    def test_on_is_rejected_for_a_gate_disabled_in_the_policy(self):
        policy = example_policy()
        gate_of(policy, 'git_push_gate')['enabled'] = False
        write_policy(self.data_dir, policy)
        self.assertIn('disabled in policy.json', self.toggle('on git_push_gate'))
        self.assertEqual(self.state.list_gate_toggles(SESSION), [])
        assert_passed_silently(self.gate('git push'))

    def test_toggle_is_per_session(self):
        pid = self.add_prompt('git_push_gate を一旦外して')
        self.toggle(f'off git_push_gate --prompt-id {pid}')
        assert_denied(run_hook(GATE_CHECK, bash_call('git push', 'another-session'), self.data_dir))

    def test_other_gates_stay_on(self):
        pid = self.add_prompt('git_push_gate を一旦外して')
        self.toggle(f'off git_push_gate --prompt-id {pid}')
        assert_denied(self.gate('git commit -m x'))

    def test_errors(self):
        self.assertIn('subcommand required', self.toggle(''))
        self.assertIn('unknown subcommand', self.toggle('flip git_push_gate'))
        self.assertIn('gate name required', self.toggle('on'))
        self.assertIn('unknown gate', self.toggle('on no_such_gate'))
        self.assertIn('too many arguments', self.toggle('on git_push_gate extra more'))
        self.assertIn('unknown option', self.toggle('list --session-id abc'))


if __name__ == '__main__':
    unittest.main()
