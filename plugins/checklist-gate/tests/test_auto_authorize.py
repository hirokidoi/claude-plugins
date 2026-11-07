"""Unit and hook-level tests for auto-authorization of user-prompt-match acks."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'scripts'))

from state import State
from gate_check import _find_auto_authorization

_PUSH_CFG = {
    'type': 'user-prompt-match',
    'min_reason_length': 20,
    'match_keywords': ['push', 'プッシュ'],
    'except_keywords': ['dry-run', 'pushはまだ'],
    'max_prompt_distance': 3,
    'auto_authorize_after_gate': 'git_commit_gate',
}

_POLICY = {
    'ack_items': {
        'review_done': {'type': 'consumable', 'min_reason_length': 10},
        'user_authorized_push': _PUSH_CFG,
    },
    'gates': [
        {
            'name': 'git_commit_gate',
            'trigger': {'patterns': ['Bash(git commit *)'], 'except_patterns': []},
            'require': ['review_done'],
            'enabled': True,
        },
        {
            'name': 'git_push_gate',
            'trigger': {
                'patterns': ['Bash(git push *)'],
                'except_patterns': ['Bash(git push --dry-run *)'],
            },
            'require': ['user_authorized_push'],
            'enabled': True,
        },
    ],
}

SESSION = 'sess-1'
ITEM = 'user_authorized_push'


class TestFindAutoAuthorization(unittest.TestCase):

    def setUp(self) -> None:
        self.state = State(':memory:')
        self.state.init_schema()
        self.state.start_session(SESSION, '/repo')

    def tearDown(self) -> None:
        self.state.close()

    def _prompt(self, text: str) -> int:
        return self.state.add_user_prompt(SESSION, text)

    def _commit_passed_now(self) -> None:
        self.state.record_gate_pass(
            SESSION, 'git_commit_gate', self.state.get_latest_prompt_id(SESSION)
        )

    def _find(self, cfg: dict = _PUSH_CFG, passing_now: set = frozenset()):
        return _find_auto_authorization(
            self.state, SESSION, ITEM, cfg, set(passing_now),
            self.state.get_latest_prompt_id(SESSION),
        )

    def test_commit_after_push_instruction(self):
        pid = self._prompt('commit -> push して')
        self._commit_passed_now()
        self.assertEqual(self._find(), pid)
        self.assertFalse(self.state.has_ack_for_prompt(SESSION, ITEM, pid))

    def test_commit_passing_in_the_same_call(self):
        pid = self._prompt('commit -> push して')
        self.assertEqual(self._find(passing_now={'git_commit_gate'}), pid)

    def test_commit_in_later_turn_still_counts(self):
        pid = self._prompt('commit して push まで')
        self._prompt('OK')
        self._commit_passed_now()
        self.assertEqual(self._find(), pid)

    def test_disabled_without_after_gate(self):
        cfg = {k: v for k, v in _PUSH_CFG.items() if k != 'auto_authorize_after_gate'}
        self._prompt('commit -> push して')
        self._commit_passed_now()
        self.assertIsNone(self._find(cfg))

    def test_no_commit_pass(self):
        self._prompt('push して')
        self.assertIsNone(self._find())

    def test_commit_before_push_instruction(self):
        self._prompt('commit して')
        self._commit_passed_now()
        self._prompt('push して')
        self.assertIsNone(self._find())

    def test_prompt_used_once(self):
        pid = self._prompt('commit -> push して')
        self._commit_passed_now()
        self.state.add_ack(SESSION, ITEM, 'used', prompt_id=pid)
        self.assertIsNone(self._find())

    def test_newest_mention_excepted_blocks_older(self):
        self._prompt('commit -> push して')
        self._prompt('やっぱり push はまだ')
        self._commit_passed_now()
        self.assertIsNone(self._find())

    def test_negated_instruction(self):
        for text in (
            'commit して、push しないで', 'commitだけ。pushは待って', 'push は不要',
            'pushせずにcommitだけ', "commit but don't push", 'do not push yet',
            'push しなくていいよ', 'push は控えて', 'no need to push', 'push is not needed',
        ):
            with self.subTest(text=text):
                self.state.close()
                self.setUp()
                self._prompt(text)
                self._commit_passed_now()
                self.assertIsNone(self._find())

    def test_newest_negation_revokes_older(self):
        self._prompt('commit -> push して')
        self._prompt('やっぱり push はやめて')
        self._commit_passed_now()
        self.assertIsNone(self._find())

    def test_identifier_containing_push_is_not_a_mention(self):
        self._prompt('web_push.md と push_subscription.rb を commit して')
        self._commit_passed_now()
        self.assertIsNone(self._find())

    def test_ide_tag_and_paste_are_ignored(self):
        self._prompt(
            '<ide_opened_file>/app/docs/push.md</ide_opened_file>'
            '<pasted_content id="1">git push</pasted_content id="1"> commit して'
        )
        self._commit_passed_now()
        self.assertIsNone(self._find())

    def test_prompt_outside_distance(self):
        self._prompt('commit -> push して')
        for _ in range(_PUSH_CFG['max_prompt_distance']):
            self._prompt('OK')
        self._commit_passed_now()
        self.assertIsNone(self._find())


class TestHookFlow(unittest.TestCase):
    """Run gate_check.py as the PreToolUse hook does."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = self._tmp.name
        with open(os.path.join(self.data_dir, 'policy.json'), 'w', encoding='utf-8') as f:
            json.dump(_POLICY, f)
        self.state = State(os.path.join(self.data_dir, 'checklist-gate.sqlite'))
        self.state.init_schema()
        self.state.start_session(SESSION, '/repo')

    def tearDown(self) -> None:
        self.state.close()
        self._tmp.cleanup()

    def _run(self, command: str) -> dict:
        hook_input = {
            'session_id': SESSION,
            'tool_name': 'Bash',
            'tool_input': {'command': command},
        }
        result = subprocess.run(
            [sys.executable, os.path.join(PLUGIN_DIR, 'scripts', 'gate_check.py')],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            env={**os.environ, 'CLAUDE_PLUGIN_DATA': self.data_dir},
            check=True,
        )
        output = json.loads(result.stdout) if result.stdout.strip() else {}
        return output.get('hookSpecificOutput', {})

    def _review_done(self) -> None:
        self.state.add_ack(SESSION, 'review_done', 'review passed')

    def test_commit_then_push_without_ack(self):
        pid = self.state.add_user_prompt(SESSION, 'commit -> push して')
        self._review_done()
        self.assertNotEqual(self._run('git commit -m x').get('permissionDecision'), 'deny')
        out = self._run('git push origin master')
        self.assertNotEqual(out.get('permissionDecision'), 'deny')
        self.assertIn(f'prompt_id={pid}', out.get('additionalContext', ''))
        self.assertEqual(self._run('git push origin master').get('permissionDecision'), 'deny')

    def test_chained_commit_and_push(self):
        self.state.add_user_prompt(SESSION, 'commit して push まで')
        self._review_done()
        out = self._run('git commit -m x && git push')
        self.assertNotEqual(out.get('permissionDecision'), 'deny')

    def test_push_without_commit_is_denied(self):
        self.state.add_user_prompt(SESSION, 'push して')
        self.assertEqual(self._run('git push').get('permissionDecision'), 'deny')

    def test_stash_push_is_not_gated(self):
        self.state.add_user_prompt(SESSION, 'stash して')
        self.assertEqual(self._run('git stash push app/models/user.rb'), {})


if __name__ == '__main__':
    unittest.main()
