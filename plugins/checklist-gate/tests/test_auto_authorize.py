"""Unit and hook-level tests for auto-authorization of user-prompt-match acks."""
import unittest

from tests.hook_harness import (
    HookTestCase, SESSION, assert_context, assert_denied, assert_passed_silently,
)
from gate_check import _find_auto_authorization
from state import State

_PUSH_CFG = {
    'type': 'user-prompt-match',
    'min_reason_length': 20,
    'match_keywords': ['push', 'プッシュ'],
    'except_keywords': ['dry-run', 'pushはまだ'],
    'max_prompt_distance': 3,
    'auto_authorize_after_gate': 'git_commit_gate',
}

ITEM = 'user_authorized_push'


class TestFindAutoAuthorization(unittest.TestCase):

    def setUp(self) -> None:
        self.state = State(':memory:')
        self.state.init_schema()
        self.state.touch_session(SESSION, '/repo')

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
        cfg = {**_PUSH_CFG, 'auto_authorize_after_gate': None}
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
        self.state.add_ack_if_allowed(SESSION, ITEM, 'used', prompt_id=pid)
        self.state.consume_acks(SESSION, [ITEM])
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


class TestHookFlow(HookTestCase):
    """Run gate_check.py as the PreToolUse hook does, with the bundled example policy."""

    def _review_done(self) -> None:
        self.state.add_ack_if_allowed(SESSION, 'commit_ready', 'review passed')

    def test_commit_then_push_without_ack(self):
        pid = self.add_prompt('commit -> push して')
        self._review_done()
        assert_passed_silently(self.run_call('git commit -m x'))
        result = self.gate('git push origin master')
        assert_context(result, f'prompt_id={pid}')
        assert_context(result, 'auto-authorized')
        assert_denied(self.gate('git push origin master'))

    def test_chained_commit_and_push(self):
        pid = self.add_prompt('commit して push まで')
        self._review_done()
        result = self.gate('git commit -m x && git push')
        assert_context(result, 'auto-authorized')
        assert_context(result, f'prompt_id={pid}')
        self.assertFalse(self.state.has_unconsumed_ack(SESSION, 'commit_ready'))

    def test_push_without_commit_is_denied(self):
        self.add_prompt('push して')
        assert_denied(self.gate('git push'))

    def test_stash_push_is_not_gated(self):
        self.add_prompt('stash して')
        assert_passed_silently(self.gate('git stash push app/models/user.rb'))


if __name__ == '__main__':
    unittest.main()
