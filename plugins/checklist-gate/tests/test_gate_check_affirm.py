"""Unit tests for --affirm logic in gate_check.py _handle_gate_ack."""
import os
import shlex
import sys
import unittest
from unittest.mock import patch

# Ensure lib/ and scripts/ are importable
PLUGIN_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'lib'))
sys.path.insert(0, os.path.join(PLUGIN_DIR, 'scripts'))

from state import State
from gate_check import _handle_gate_ack


# -- Test policy used across tests --

_POLICY_WITH_AFFIRM = {
    'affirm_keywords': ['OK', 'はい', 'よい', 'いいよ', 'お願い', 'yes', 'sure'],
    'ack_items': {
        'user_authorized_push': {
            'type': 'user-prompt-match',
            'min_reason_length': 20,
            'hint': 'push hint',
            'match_keywords': ['push', 'プッシュ'],
            'except_keywords': ['dry-run', 'ドライラン'],
            'max_prompt_distance': 3,
        }
    },
    'gates': [
        {
            'name': 'git_push_gate',
            'description': 'Require user_authorized_push before git push',
            'trigger': {
                'type': 'gate',
                'patterns': ['Bash(git push *)'],
                'except_patterns': ['Bash(git push --dry-run *)'],
            },
            'require': ['user_authorized_push'],
            'enabled': True,
        }
    ],
}


class _AffirmTestBase(unittest.TestCase):
    """Base class with a fresh in-memory State and a patched _load_policy."""

    def setUp(self) -> None:
        self.state = State(':memory:')
        self.state.init_schema()
        self.state.start_session('sess-1', '/tmp')

        # Pre-record a deny so deny-first passes
        self.state.record_deny('sess-1', 'git_push_gate')

        # Patch _load_policy to return our test policy
        self._patcher = patch('gate_check._load_policy', return_value=_POLICY_WITH_AFFIRM)
        self._patcher.start()

    def tearDown(self) -> None:
        self._patcher.stop()
        self.state.close()

    # -- helpers --

    def _call_ack(self, command: str) -> str:
        """Parse command and call _handle_gate_ack, returning the result string."""
        tokens = shlex.split(command)
        return _handle_gate_ack(tokens, command, 'sess-1', self.state)

    def _add_prompt(self, text: str) -> int:
        """Add a user prompt and return its id."""
        return self.state.add_user_prompt('sess-1', text)


# ---- Affirm flow: success cases ----


class TestAffirmFlowSuccess(_AffirmTestBase):

    def test_affirm_ok_with_push_keyword(self) -> None:
        """User says 'OK', AI asked 'push しますか？' → should pass."""
        pid = self._add_prompt('OK')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--affirm "push しますか？" '
            f'--reason "AIの質問「push しますか？」に対しユーザーがOKと回答したことを確認"'
        )
        self.assertIn('acknowledged', result)

    def test_affirm_hai_with_push_keyword(self) -> None:
        """User says 'はい', AI asked 'push しますか？' → should pass."""
        pid = self._add_prompt('はい')
        # Re-record deny (consumed by previous ack)
        self.state.record_deny('sess-1', 'git_push_gate')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--affirm "push しますか？" '
            f'--reason "AIの質問「push しますか？」に対しユーザーが「はい」と回答したことを確認"'
        )
        self.assertIn('acknowledged', result)

    def test_affirm_yes_with_push_keyword_in_katakana(self) -> None:
        """User says 'yes', AI asked 'プッシュしますか？' → should pass (プッシュ matches)."""
        pid = self._add_prompt('yes')
        self.state.record_deny('sess-1', 'git_push_gate')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--affirm "プッシュしますか？" '
            f'--reason "AIの質問「プッシュしますか？」に対しユーザーがyesと回答したことを確認"'
        )
        self.assertIn('acknowledged', result)


# ---- Affirm flow: rejection cases ----


class TestAffirmFlowRejection(_AffirmTestBase):

    def test_affirm_rejected_prompt_not_affirmative(self) -> None:
        """User says 'うーん考え中' (not in affirm_keywords) → should reject."""
        pid = self._add_prompt('うーん考え中')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--affirm "push しますか？" '
            f'--reason "AIの質問「push しますか？」に対しユーザーが「うーん考え中」と回答した"'
        )
        self.assertIn('Rejected', result)

    def test_affirm_rejected_no_match_keyword_in_affirm_text(self) -> None:
        """User says 'OK', but AI asked 'commit しますか？' (no push keyword) → should reject."""
        pid = self._add_prompt('OK')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--affirm "commit しますか？" '
            f'--reason "AIの質問「commit しますか？」に対しユーザーがOKと回答したことを確認"'
        )
        self.assertIn('Rejected', result)

    def test_affirm_rejected_except_keyword_in_affirm_text(self) -> None:
        """User says 'OK', AI asked 'dry-run で push しますか？' → except_keywords → reject."""
        pid = self._add_prompt('OK')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--affirm "dry-run で push しますか？" '
            f'--reason "AIの質問「dry-run で push しますか？」に対しユーザーがOKと回答した"'
        )
        self.assertIn('Rejected', result)


# ---- Normal flow (without --affirm): keyword check uses unified reject message ----


class TestNormalFlowKeywordReject(_AffirmTestBase):

    def test_normal_flow_no_match_keyword(self) -> None:
        """User says 'マージして' (no push keyword, no --affirm) → should reject."""
        pid = self._add_prompt('マージして')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--reason "ユーザー発話『マージして』を受信したが push キーワードが含まれない（prompt_id={pid}）"'
        )
        self.assertIn('Rejected', result)

    def test_normal_flow_except_keyword_hit(self) -> None:
        """User says 'dry-run で push して' → except_keywords → reject."""
        pid = self._add_prompt('dry-run で push して')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--reason "ユーザー発話『dry-run で push して』を受信（prompt_id={pid}）"'
        )
        self.assertIn('Rejected', result)

    def test_normal_flow_success(self) -> None:
        """User says 'push して' → match_keywords hit, no except → should pass."""
        pid = self._add_prompt('push して')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--reason "ユーザー発話『push して』を受信（prompt_id={pid}）"'
        )
        self.assertIn('acknowledged', result)


# ---- Edge cases ----


class TestAffirmEdgeCases(_AffirmTestBase):

    def test_affirm_with_case_insensitive_ok(self) -> None:
        """User says 'ok' (lowercase) → affirm_keywords includes 'ok' → should pass."""
        pid = self._add_prompt('ok')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--affirm "push しますか？" '
            f'--reason "AIの質問「push しますか？」に対しユーザーがokと小文字で回答したことを確認"'
        )
        self.assertIn('acknowledged', result)

    def test_affirm_whitespace_normalization(self) -> None:
        """Whitespace in user prompt should be stripped before comparison."""
        pid = self._add_prompt(' O K ')
        result = self._call_ack(
            f'gate-ack user_authorized_push --prompt-id {pid} '
            f'--affirm "push しますか？" '
            f'--reason "AIの質問「push しますか？」に対しユーザーが空白付きOKで回答したことを確認"'
        )
        self.assertIn('acknowledged', result)


if __name__ == '__main__':
    unittest.main()
