"""Block gates stop matching calls whatever the acks, toggles or database say."""
import os
import unittest

from tests.hook_harness import (
    DB_NAME, SESSION, HookTestCase, assert_denied, assert_passed_silently, example_policy,
    gate_of, write_policy,
)

SUDO = 'su' + 'do'
BLOCKED = 'sudo_block blocked'
MESSAGE = gate_of(example_policy(), 'sudo_block')['message']


class TestBlockGate(HookTestCase):

    def toggle(self, args: str) -> str:
        return self.gate_command(f'gate-toggle {args}')

    def deny_count(self) -> int:
        return self.state._get_conn().execute('SELECT COUNT(*) FROM gate_denies').fetchone()[0]

    def test_matching_commands_are_denied_with_the_gate_message(self):
        for command in (
            f'{SUDO} ls', f'bash -c "{SUDO} ls"', f'/usr/bin/{SUDO} ls', f'ls && {SUDO} rm x',
            f'echo $({SUDO} id)', f'{SUDO}',
        ):
            with self.subTest(command=command):
                assert_denied(self.gate(command), BLOCKED, MESSAGE, 'cannot lift')
        assert_denied(self.gate_tool('Monitor', {'command': f'{SUDO} ls'}), BLOCKED)

    def test_quoted_words_and_other_commands_pass(self):
        for command in (f'echo "{SUDO} ls"', f'ls {SUDO}-notes', f'git log --author {SUDO}'):
            with self.subTest(command=command):
                assert_passed_silently(self.gate(command))

    def test_no_deny_is_recorded_so_no_ack_can_follow(self):
        assert_denied(self.gate(f'{SUDO} ls'), BLOCKED)
        self.assertEqual(self.deny_count(), 0)

    def test_block_is_reported_instead_of_the_acks_other_gates_lack(self):
        result = self.gate(f'{SUDO} git push')
        assert_denied(result, BLOCKED)
        self.assertNotIn('user_authorized_push', result.reason)
        self.assertEqual(self.deny_count(), 0)

    def test_every_matching_block_gate_is_reported(self):
        policy = example_policy()
        policy['gates'].append({
            'name': 'rm_block', 'trigger': {'patterns': ['Bash(rm *)']},
            'block': True, 'message': 'rm is not allowed.',
        })
        write_policy(self.data_dir, policy)
        assert_denied(self.gate(f'{SUDO} rm x'), BLOCKED, MESSAGE, 'rm_block blocked', 'rm is not allowed.')

    def test_unreadable_command_is_denied_with_a_hint(self):
        assert_denied(self.gate(f"{SUDO} echo 'unterminated"), BLOCKED, 'could not read')
        self.assertNotIn('could not read', self.gate(f'{SUDO} ls').reason)

    def test_off_is_rejected_even_on_the_users_instruction(self):
        pid = self.add_prompt('sudo_block を外して')
        output = self.toggle(f'off sudo_block --prompt-id {pid}')
        self.assertIn('Rejected', output)
        self.assertIn('block gate', output)
        self.assertEqual(self.state.list_gate_toggles(SESSION), [])
        assert_denied(self.gate(f'{SUDO} ls'), BLOCKED)

    def test_off_toggle_recorded_before_it_became_a_block_gate_is_ignored(self):
        self.state.set_gate_toggle(SESSION, 'sudo_block', enabled=False)
        assert_denied(self.gate(f'{SUDO} ls'), BLOCKED)
        self.assertIn('sudo_block: ON (block)', self.toggle('list'))

    def test_broken_database_still_blocks(self):
        self.state.close()
        for suffix in ('', '-wal', '-shm'):
            path = os.path.join(self.data_dir, DB_NAME + suffix)
            if os.path.exists(path):
                os.remove(path)
        with open(os.path.join(self.data_dir, DB_NAME), 'wb') as f:
            f.write(b'this is not a sqlite database' * 100)
        assert_denied(self.gate(f'{SUDO} ls'), BLOCKED, MESSAGE)

    def test_gate_disabled_in_the_policy_does_not_block(self):
        policy = example_policy()
        gate_of(policy, 'sudo_block')['enabled'] = False
        write_policy(self.data_dir, policy)
        assert_passed_silently(self.gate(f'{SUDO} ls'))
        self.assertIn('sudo_block: OFF (default)', self.toggle('list'))

    def test_help_gates_marks_the_block_gate(self):
        output = self.gate_command('gate-ack --help-gates')
        self.assertIn('block: cannot be acked or turned off', output)


if __name__ == '__main__':
    unittest.main()
