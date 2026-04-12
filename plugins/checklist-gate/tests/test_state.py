"""Unit tests for lib/state.py State class."""
import unittest

from lib.state import State


class _StateTestBase(unittest.TestCase):
    """Base class that provides a fresh in-memory State for every test."""

    def setUp(self) -> None:
        self.state = State(':memory:')
        self.state.init_schema()

    def tearDown(self) -> None:
        self.state.close()

    # -- helpers --

    def _create_session(self, session_id: str = 'sess-1', cwd: str = '/tmp') -> None:
        self.state.start_session(session_id, cwd)


class TestInitSchema(_StateTestBase):
    """init_schema should be idempotent."""

    def test_init_schema_idempotent(self) -> None:
        # Already called once in setUp; calling again must not raise.
        self.state.init_schema()
        self.state.init_schema()


# ---- Session operations ----


class TestStartSession(_StateTestBase):

    def test_start_session_inserts_row(self) -> None:
        self._create_session('s1', '/home')
        conn = self.state._get_conn()
        row = conn.execute(
            'SELECT session_id, cwd, started_at FROM sessions WHERE session_id = ?',
            ('s1',),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['session_id'], 's1')
        self.assertEqual(row['cwd'], '/home')
        self.assertIsNotNone(row['started_at'])


# ---- SessionCheck operations (persistent ack) ----


class TestAddSessionCheck(_StateTestBase):

    def test_add_session_check_inserts(self) -> None:
        self._create_session('s1')
        self.state.add_session_check('s1', 'lint', 'all good')
        self.assertTrue(self.state.has_session_check('s1', 'lint'))

    def test_add_session_check_duplicate_ignored(self) -> None:
        self._create_session('s1')
        self.state.add_session_check('s1', 'lint', 'reason1')
        # Duplicate INSERT should not raise
        self.state.add_session_check('s1', 'lint', 'reason2')
        # Still only one row
        conn = self.state._get_conn()
        cnt = conn.execute(
            "SELECT COUNT(*) as cnt FROM session_checks WHERE session_id = 's1' AND item = 'lint'"
        ).fetchone()
        self.assertEqual(cnt['cnt'], 1)


class TestHasSessionCheck(_StateTestBase):

    def test_has_session_check_exists(self) -> None:
        self._create_session('s1')
        self.state.add_session_check('s1', 'test', 'ok')
        self.assertTrue(self.state.has_session_check('s1', 'test'))

    def test_has_session_check_not_exists(self) -> None:
        self.assertFalse(self.state.has_session_check('s1', 'test'))


# ---- Ack operations (consumable ack) ----


class TestAddAck(_StateTestBase):

    def test_add_ack_multiple(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'deploy', 'r1')
        self.state.add_ack('s1', 'deploy', 'r2')
        conn = self.state._get_conn()
        cnt = conn.execute(
            "SELECT COUNT(*) as cnt FROM acks WHERE session_id = 's1' AND item = 'deploy'"
        ).fetchone()
        self.assertEqual(cnt['cnt'], 2)

    def test_add_ack_with_prompt_id(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'push', 'reason', prompt_id=42)
        ack = self.state.get_oldest_unconsumed_ack('s1', 'push')
        self.assertIsNotNone(ack)
        self.assertEqual(ack.prompt_id, 42)

    def test_add_ack_without_prompt_id(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'deploy', 'reason')
        ack = self.state.get_oldest_unconsumed_ack('s1', 'deploy')
        self.assertIsNotNone(ack)
        self.assertIsNone(ack.prompt_id)


class TestHasUnconsumedAck(_StateTestBase):

    def test_has_unconsumed_ack_true(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'deploy', 'reason')
        self.assertTrue(self.state.has_unconsumed_ack('s1', 'deploy'))

    def test_has_unconsumed_ack_false_after_consume(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'deploy', 'reason')
        self.state.consume_oldest_unconsumed_ack('s1', 'deploy')
        self.assertFalse(self.state.has_unconsumed_ack('s1', 'deploy'))


class TestGetOldestUnconsumedAck(_StateTestBase):

    def test_returns_oldest(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'item', 'first', prompt_id=10)
        self.state.add_ack('s1', 'item', 'second', prompt_id=20)
        ack = self.state.get_oldest_unconsumed_ack('s1', 'item')
        self.assertIsNotNone(ack)
        self.assertEqual(ack.reason, 'first')
        self.assertEqual(ack.prompt_id, 10)

    def test_returns_none_when_empty(self) -> None:
        self._create_session('s1')
        result = self.state.get_oldest_unconsumed_ack('s1', 'item')
        self.assertIsNone(result)

    def test_skips_consumed(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'item', 'first', prompt_id=10)
        self.state.add_ack('s1', 'item', 'second', prompt_id=20)
        self.state.consume_oldest_unconsumed_ack('s1', 'item')
        ack = self.state.get_oldest_unconsumed_ack('s1', 'item')
        self.assertIsNotNone(ack)
        self.assertEqual(ack.reason, 'second')
        self.assertEqual(ack.prompt_id, 20)


class TestConsumeOldestUnconsumedAck(_StateTestBase):

    def test_consume_fifo_order(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'item', 'first', prompt_id=10)
        self.state.add_ack('s1', 'item', 'second', prompt_id=20)

        ack1 = self.state.consume_oldest_unconsumed_ack('s1', 'item')
        self.assertIsNotNone(ack1)
        self.assertEqual(ack1.reason, 'first')
        self.assertIsNotNone(ack1.consumed_at)
        self.assertEqual(ack1.prompt_id, 10)

        ack2 = self.state.consume_oldest_unconsumed_ack('s1', 'item')
        self.assertIsNotNone(ack2)
        self.assertEqual(ack2.reason, 'second')
        self.assertEqual(ack2.prompt_id, 20)

    def test_consume_returns_none_when_empty(self) -> None:
        self._create_session('s1')
        result = self.state.consume_oldest_unconsumed_ack('s1', 'item')
        self.assertIsNone(result)


class TestListUnconsumedAcks(_StateTestBase):

    def test_list_unconsumed_acks_only(self) -> None:
        self._create_session('s1')
        self.state.add_ack('s1', 'a', 'r1')
        self.state.add_ack('s1', 'b', 'r2')
        self.state.add_ack('s1', 'c', 'r3')
        # Consume one
        self.state.consume_oldest_unconsumed_ack('s1', 'a')

        unconsumed = self.state.list_unconsumed_acks('s1')
        items = [ack.item for ack in unconsumed]
        self.assertEqual(items, ['b', 'c'])

    def test_list_unconsumed_acks_empty(self) -> None:
        self._create_session('s1')
        self.assertEqual(self.state.list_unconsumed_acks('s1'), [])


# ---- GateToggle operations ----


class TestSetGateToggle(_StateTestBase):

    def test_set_gate_toggle_new(self) -> None:
        self._create_session('s1')
        self.state.set_gate_toggle('s1', 'pre-commit', True)
        self.assertTrue(self.state.is_gate_enabled('s1', 'pre-commit'))

    def test_set_gate_toggle_upsert(self) -> None:
        self._create_session('s1')
        self.state.set_gate_toggle('s1', 'pre-commit', True)
        self.state.set_gate_toggle('s1', 'pre-commit', False)
        self.assertFalse(self.state.is_gate_enabled('s1', 'pre-commit'))


class TestIsGateEnabled(_StateTestBase):

    def test_is_gate_enabled_true(self) -> None:
        self._create_session('s1')
        self.state.set_gate_toggle('s1', 'gate', True)
        self.assertTrue(self.state.is_gate_enabled('s1', 'gate'))

    def test_is_gate_enabled_false(self) -> None:
        self._create_session('s1')
        self.state.set_gate_toggle('s1', 'gate', False)
        self.assertFalse(self.state.is_gate_enabled('s1', 'gate'))

    def test_is_gate_enabled_default_true(self) -> None:
        self.assertTrue(self.state.is_gate_enabled('s1', 'nonexistent'))


class TestListGateToggles(_StateTestBase):

    def test_list_gate_toggles(self) -> None:
        self._create_session('s1')
        self.state.set_gate_toggle('s1', 'alpha', True)
        self.state.set_gate_toggle('s1', 'beta', False)

        toggles = self.state.list_gate_toggles('s1')
        names = {t.gate_name: t.enabled for t in toggles}
        self.assertEqual(names, {'alpha': True, 'beta': False})

    def test_list_gate_toggles_empty(self) -> None:
        self.assertEqual(self.state.list_gate_toggles('s1'), [])


class TestCleanupOldGateToggles(_StateTestBase):

    def test_cleanup_old_gate_toggles_deletes_old_only(self) -> None:
        # Active session (started now)
        self._create_session('active')
        self.state.set_gate_toggle('active', 'gate', True)

        # Old session via raw SQL (started long ago)
        conn = self.state._get_conn()
        conn.execute(
            "INSERT INTO sessions (session_id, started_at, cwd) "
            "VALUES ('old', '2020-01-01T00:00:00+00:00', '/tmp')"
        )
        conn.execute(
            "INSERT INTO gate_toggles (session_id, gate_name, enabled, updated_at) "
            "VALUES ('old', 'gate', 1, '2020-01-01T12:00:00+00:00')"
        )
        conn.commit()

        deleted = self.state.cleanup_old_gate_toggles(keep_days=7)
        self.assertEqual(deleted, 1)

        # Active session's toggles still present
        toggles = self.state.list_gate_toggles('active')
        self.assertEqual(len(toggles), 1)


# ---- Transaction ----


# ---- UserPrompt operations ----


class TestAddUserPrompt(_StateTestBase):

    def test_add_user_prompt_returns_positive_int(self) -> None:
        self._create_session('s1')
        pid = self.state.add_user_prompt('s1', 'hello')
        self.assertIsInstance(pid, int)
        self.assertGreater(pid, 0)

    def test_add_user_prompt_ids_increment(self) -> None:
        self._create_session('s1')
        pid1 = self.state.add_user_prompt('s1', 'first')
        pid2 = self.state.add_user_prompt('s1', 'second')
        self.assertGreater(pid2, pid1)


class TestGetUserPrompt(_StateTestBase):

    def test_get_existing_prompt(self) -> None:
        self._create_session('s1')
        pid = self.state.add_user_prompt('s1', 'push して')
        prompt = self.state.get_user_prompt(pid, 's1')
        self.assertIsNotNone(prompt)
        self.assertEqual(prompt.id, pid)
        self.assertEqual(prompt.session_id, 's1')
        self.assertEqual(prompt.prompt, 'push して')

    def test_get_nonexistent_prompt_id(self) -> None:
        self._create_session('s1')
        result = self.state.get_user_prompt(9999, 's1')
        self.assertIsNone(result)

    def test_get_prompt_wrong_session(self) -> None:
        self._create_session('s1')
        self._create_session('s2')
        pid = self.state.add_user_prompt('s1', 'hello')
        result = self.state.get_user_prompt(pid, 's2')
        self.assertIsNone(result)


class TestIsPromptWithinDistance(_StateTestBase):

    def test_within_distance(self) -> None:
        self._create_session('s1')
        pid1 = self.state.add_user_prompt('s1', 'a')
        pid2 = self.state.add_user_prompt('s1', 'b')
        pid3 = self.state.add_user_prompt('s1', 'c')
        # pid3 is latest, distance=2 covers pid3 and pid2
        self.assertTrue(self.state.is_prompt_within_distance(pid2, 's1', 2))

    def test_outside_distance(self) -> None:
        self._create_session('s1')
        pid1 = self.state.add_user_prompt('s1', 'a')
        pid2 = self.state.add_user_prompt('s1', 'b')
        pid3 = self.state.add_user_prompt('s1', 'c')
        # pid1 is 3rd from latest, distance=2 only covers pid3 and pid2
        self.assertFalse(self.state.is_prompt_within_distance(pid1, 's1', 2))

    def test_latest_prompt_always_within(self) -> None:
        self._create_session('s1')
        pid1 = self.state.add_user_prompt('s1', 'a')
        pid2 = self.state.add_user_prompt('s1', 'b')
        # distance=1 covers only the latest prompt
        self.assertTrue(self.state.is_prompt_within_distance(pid2, 's1', 1))


class TestGetOldestValidPromptId(_StateTestBase):

    def test_normal_case(self) -> None:
        self._create_session('s1')
        pid1 = self.state.add_user_prompt('s1', 'a')
        pid2 = self.state.add_user_prompt('s1', 'b')
        pid3 = self.state.add_user_prompt('s1', 'c')
        # max_distance=2: covers pid3, pid2 → oldest valid is pid2
        oldest = self.state.get_oldest_valid_prompt_id('s1', 2)
        self.assertEqual(oldest, pid2)

    def test_fewer_prompts_than_distance(self) -> None:
        self._create_session('s1')
        pid1 = self.state.add_user_prompt('s1', 'only one')
        # max_distance=5 but only 1 prompt → oldest valid is pid1
        oldest = self.state.get_oldest_valid_prompt_id('s1', 5)
        self.assertEqual(oldest, pid1)

    def test_no_prompts(self) -> None:
        self._create_session('s1')
        oldest = self.state.get_oldest_valid_prompt_id('s1', 3)
        self.assertIsNone(oldest)


# ---- GateDeny operations ----


class TestRecordGateDeny(_StateTestBase):

    def test_record_then_has_deny(self) -> None:
        self._create_session('s1')
        self.state.record_deny('s1', 'git_push_gate')
        self.assertTrue(self.state.has_deny('s1', 'git_push_gate'))

    def test_upsert_keeps_single_row(self) -> None:
        self._create_session('s1')
        self.state.record_deny('s1', 'git_push_gate')
        self.state.record_deny('s1', 'git_push_gate')
        conn = self.state._get_conn()
        cnt = conn.execute(
            "SELECT COUNT(*) as cnt FROM gate_denies "
            "WHERE session_id = 's1' AND gate_name = 'git_push_gate'"
        ).fetchone()
        self.assertEqual(cnt['cnt'], 1)


class TestHasGateDeny(_StateTestBase):

    def test_has_deny_true(self) -> None:
        self._create_session('s1')
        self.state.record_deny('s1', 'gate_a')
        self.assertTrue(self.state.has_deny('s1', 'gate_a'))

    def test_has_deny_false(self) -> None:
        self._create_session('s1')
        self.assertFalse(self.state.has_deny('s1', 'gate_a'))

    def test_has_deny_different_gate(self) -> None:
        self._create_session('s1')
        self.state.record_deny('s1', 'gate_a')
        self.assertFalse(self.state.has_deny('s1', 'gate_b'))


class TestDeleteGateDeny(_StateTestBase):

    def test_record_then_clear(self) -> None:
        self._create_session('s1')
        self.state.record_deny('s1', 'gate_a')
        self.assertTrue(self.state.has_deny('s1', 'gate_a'))
        self.state.clear_deny('s1', 'gate_a')
        self.assertFalse(self.state.has_deny('s1', 'gate_a'))

    def test_clear_nonexistent_no_error(self) -> None:
        self._create_session('s1')
        # Should not raise
        self.state.clear_deny('s1', 'nonexistent_gate')


# ---- Cleanup: UserPrompts and GateDenies ----


class TestCleanupOldUserPrompts(_StateTestBase):

    def test_cleanup_old_user_prompts_deletes_old_only(self) -> None:
        # Active session (started now)
        self._create_session('active')
        self.state.add_user_prompt('active', 'recent prompt')

        # Old session via raw SQL (started long ago)
        conn = self.state._get_conn()
        conn.execute(
            "INSERT INTO sessions (session_id, started_at, cwd) "
            "VALUES ('old', '2020-01-01T00:00:00+00:00', '/tmp')"
        )
        conn.execute(
            "INSERT INTO user_prompts (session_id, prompt, created_at) "
            "VALUES ('old', 'old prompt', '2020-01-01T12:00:00+00:00')"
        )
        conn.commit()

        deleted = self.state.cleanup_old_user_prompts(keep_days=7)
        self.assertEqual(deleted, 1)

        # Active session's prompts still present
        prompt = self.state.get_user_prompt(1, 'active')
        self.assertIsNotNone(prompt)


class TestCleanupOldGateDenies(_StateTestBase):

    def test_cleanup_old_gate_denies_deletes_old_only(self) -> None:
        # Active session (started now)
        self._create_session('active')
        self.state.record_deny('active', 'gate_a')

        # Old session via raw SQL (started long ago)
        conn = self.state._get_conn()
        conn.execute(
            "INSERT INTO sessions (session_id, started_at, cwd) "
            "VALUES ('old', '2020-01-01T00:00:00+00:00', '/tmp')"
        )
        conn.execute(
            "INSERT INTO gate_denies (session_id, gate_name, created_at) "
            "VALUES ('old', 'gate_b', '2020-01-01T12:00:00+00:00')"
        )
        conn.commit()

        deleted = self.state.cleanup_old_gate_denies(keep_days=7)
        self.assertEqual(deleted, 1)

        # Active session's denies still present
        self.assertTrue(self.state.has_deny('active', 'gate_a'))


# ---- Transaction ----


class TestTransaction(_StateTestBase):

    def test_transaction_rollback_on_error(self) -> None:
        self._create_session('s1')
        try:
            with self.state.transaction() as conn:
                conn.execute(
                    "INSERT INTO session_checks (session_id, item, reason, checked_at) "
                    "VALUES ('s1', 'rollback-item', 'reason', '2024-01-01T00:00:00+00:00')"
                )
                raise ValueError('force rollback')
        except ValueError:
            pass

        self.assertFalse(self.state.has_session_check('s1', 'rollback-item'))

    def test_transaction_commit_on_success(self) -> None:
        self._create_session('s1')
        with self.state.transaction() as conn:
            conn.execute(
                "INSERT INTO session_checks (session_id, item, reason, checked_at) "
                "VALUES ('s1', 'ok-item', 'reason', '2024-01-01T00:00:00+00:00')"
            )

        self.assertTrue(self.state.has_session_check('s1', 'ok-item'))


if __name__ == '__main__':
    unittest.main()
