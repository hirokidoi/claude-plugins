"""Unit tests for lib/state.py State class."""
import datetime
import os
import sqlite3
import tempfile
import unittest

from tests.hook_harness import (
    AGED_TIME, TABLES, row_counts, seed_all_rows, seed_row, seed_session,
)
from state import State, _RETENTION_DAYS

NOW = '2999-01-01T00:00:00+00:00'


class _StateTestBase(unittest.TestCase):
    """Base class that provides a fresh in-memory State for every test."""

    def setUp(self) -> None:
        self.state = State(':memory:')
        self.state.init_schema()

    def tearDown(self) -> None:
        self.state.close()

    def _create_session(self, session_id: str = 'sess-1', cwd: str = '/tmp') -> None:
        self.state.touch_session(session_id, cwd)

    def _sql(self, sql: str, params: tuple = ()) -> list:
        return self.state._get_conn().execute(sql, params).fetchall()

    def _ack(self, session_id: str, item: str, reason: str = 'r', prompt_id=None) -> str:
        return self.state.add_ack_if_allowed(session_id, item, reason, prompt_id=prompt_id)


class TestInitSchema(_StateTestBase):

    def test_init_schema_idempotent(self) -> None:
        self.state.init_schema()
        self.state.init_schema()

    def test_old_schema_gains_the_new_columns(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'old.sqlite')
            conn = sqlite3.connect(path)
            conn.executescript(
                'CREATE TABLE sessions (session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, '
                'cwd TEXT NOT NULL);'
                'CREATE TABLE acks (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, '
                'item TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, consumed_at TEXT);'
                "INSERT INTO sessions VALUES ('old', '2020-01-01T00:00:00+00:00', '/x');"
                "INSERT INTO acks (session_id, item, reason, created_at) "
                "VALUES ('old', 'i', 'r', '2020-01-01T00:00:00+00:00');"
            )
            conn.commit()
            conn.close()
            state = State(path)
            try:
                state.init_schema()
                state.init_schema()
                sessions = [r[1] for r in state._get_conn().execute('PRAGMA table_info(sessions)')]
                acks = [r[1] for r in state._get_conn().execute('PRAGMA table_info(acks)')]
                self.assertIn('last_active_at', sessions)
                self.assertIn('prompt_id', acks)
                self.assertTrue(state.has_unconsumed_ack('old', 'i'))
                state.touch_session('old', '/x')
            finally:
                state.close()


# ---- Session operations ----


class TestTouchSession(_StateTestBase):

    def test_inserts_row_marked_active(self) -> None:
        self._create_session('s1', '/home')
        row = self._sql(
            'SELECT cwd, started_at, last_active_at FROM sessions WHERE session_id = ?', ('s1',)
        )[0]
        self.assertEqual(row['cwd'], '/home')
        self.assertIsNotNone(row['started_at'])
        self.assertIsNotNone(row['last_active_at'])

    def test_existing_session_keeps_its_start_and_moves_its_last_activity(self) -> None:
        seed_session(self.state, 's1', AGED_TIME, AGED_TIME)
        self.state.touch_session('s1', '/elsewhere')
        rows = self._sql('SELECT * FROM sessions')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['started_at'], AGED_TIME)
        self.assertGreater(rows[0]['last_active_at'], AGED_TIME)


# ---- SessionCheck operations (persistent ack) ----


class TestSessionCheck(_StateTestBase):

    def test_add_session_check_inserts(self) -> None:
        self.state.add_session_check('s1', 'lint', 'all good')
        self.assertTrue(self.state.has_session_check('s1', 'lint'))

    def test_add_session_check_duplicate_ignored(self) -> None:
        self.state.add_session_check('s1', 'lint', 'reason1')
        self.state.add_session_check('s1', 'lint', 'reason2')
        rows = self._sql("SELECT reason FROM session_checks WHERE session_id = 's1'")
        self.assertEqual([r['reason'] for r in rows], ['reason1'])

    def test_has_session_check_not_exists(self) -> None:
        self.assertFalse(self.state.has_session_check('s1', 'test'))

    def test_session_check_is_per_session_and_item(self) -> None:
        self.state.add_session_check('s1', 'lint', 'ok')
        self.assertFalse(self.state.has_session_check('s2', 'lint'))
        self.assertFalse(self.state.has_session_check('s1', 'other'))


# ---- Ack operations ----


class TestAddAckIfAllowed(_StateTestBase):

    def test_first_ack_is_added(self) -> None:
        self.assertEqual(self._ack('s1', 'deploy'), 'added')
        self.assertTrue(self.state.has_unconsumed_ack('s1', 'deploy'))

    def test_second_unconsumed_ack_is_stacked(self) -> None:
        self._ack('s1', 'deploy')
        self.assertEqual(self._ack('s1', 'deploy'), 'stacked')
        self.assertEqual(len(self._sql("SELECT 1 FROM acks WHERE item = 'deploy'")), 1)

    def test_ack_can_be_added_again_after_consumption(self) -> None:
        self._ack('s1', 'deploy')
        self.assertTrue(self.state.consume_acks('s1', ['deploy']))
        self.assertEqual(self._ack('s1', 'deploy'), 'added')

    def test_used_prompt_id_is_rejected(self) -> None:
        self._ack('s1', 'push', prompt_id=42)
        self.state.consume_acks('s1', ['push'])
        self.assertEqual(self._ack('s1', 'push', prompt_id=42), 'prompt_used')

    def test_prompt_id_is_remembered(self) -> None:
        self._ack('s1', 'push', prompt_id=42)
        self.assertTrue(self.state.has_ack_for_prompt('s1', 'push', 42))
        self.assertFalse(self.state.has_ack_for_prompt('s1', 'push', 43))
        self.assertFalse(self.state.has_ack_for_prompt('s1', 'other', 42))

    def test_acks_are_per_session_and_item(self) -> None:
        self._ack('s1', 'deploy')
        self.assertEqual(self._ack('s2', 'deploy'), 'added')
        self.assertEqual(self._ack('s1', 'other'), 'added')


class TestConsumeAcks(_StateTestBase):

    def test_consumes_one_ack_per_item_oldest_first(self) -> None:
        self._ack('s1', 'item', 'first')
        self.state.consume_acks('s1', ['item'])
        self._ack('s1', 'item', 'second')
        rows = self._sql('SELECT reason, consumed_at FROM acks ORDER BY id')
        self.assertIsNotNone(rows[0]['consumed_at'])
        self.assertIsNone(rows[1]['consumed_at'])

    def test_oldest_by_id_is_consumed_when_created_at_ties(self) -> None:
        conn = self.state._get_conn()
        for reason in ('first', 'second'):
            conn.execute(
                "INSERT INTO acks (session_id, item, reason, created_at) "
                "VALUES ('s1', 'item', ?, '2024-01-01T00:00:00+00:00')", (reason,),
            )
        conn.commit()
        self.assertTrue(self.state.consume_acks('s1', ['item']))
        rows = self._sql('SELECT reason, consumed_at FROM acks ORDER BY id')
        self.assertIsNotNone(rows[0]['consumed_at'])
        self.assertIsNone(rows[1]['consumed_at'])

    def test_consuming_without_an_ack_changes_nothing(self) -> None:
        self._ack('s1', 'a')
        self.assertFalse(self.state.consume_acks('s1', ['a', 'b']))
        self.assertTrue(self.state.has_unconsumed_ack('s1', 'a'))

    def test_the_same_item_twice_needs_two_acks(self) -> None:
        self._ack('s1', 'a')
        self.assertFalse(self.state.consume_acks('s1', ['a', 'a']))
        self.assertTrue(self.state.has_unconsumed_ack('s1', 'a'))

    def test_grants_are_recorded_as_used(self) -> None:
        self.assertTrue(self.state.consume_acks('s1', [], [('push', 7, 'auto')]))
        self.assertTrue(self.state.has_ack_for_prompt('s1', 'push', 7))
        self.assertFalse(self.state.has_unconsumed_ack('s1', 'push'))

    def test_grant_for_a_used_prompt_fails_and_changes_nothing(self) -> None:
        self._ack('s1', 'a')
        self.state.consume_acks('s1', [], [('push', 7, 'auto')])
        self.assertFalse(self.state.consume_acks('s1', ['a'], [('push', 7, 'auto')]))
        self.assertTrue(self.state.has_unconsumed_ack('s1', 'a'))


class TestExpirePromptAcks(_StateTestBase):

    def test_only_acks_of_older_prompts_are_expired(self) -> None:
        self._ack('s1', 'push', prompt_id=5)
        self.state.expire_prompt_acks_before('s1', 'push', 5)
        self.assertTrue(self.state.has_unconsumed_ack('s1', 'push'))
        self.state.expire_prompt_acks_before('s1', 'push', 6)
        self.assertFalse(self.state.has_unconsumed_ack('s1', 'push'))

    def test_none_expires_every_prompt_ack(self) -> None:
        self._ack('s1', 'push', prompt_id=5)
        self.state.expire_prompt_acks_before('s1', 'push', None)
        self.assertFalse(self.state.has_unconsumed_ack('s1', 'push'))

    def test_acks_without_a_prompt_are_left_alone(self) -> None:
        self._ack('s1', 'review')
        self.state.expire_prompt_acks_before('s1', 'review', None)
        self.assertTrue(self.state.has_unconsumed_ack('s1', 'review'))

    def test_other_items_and_sessions_are_left_alone(self) -> None:
        self._ack('s1', 'push', prompt_id=1)
        self._ack('s2', 'push', prompt_id=1)
        self._ack('s1', 'other', prompt_id=1)
        self.state.expire_prompt_acks_before('s1', 'push', None)
        self.assertTrue(self.state.has_unconsumed_ack('s2', 'push'))
        self.assertTrue(self.state.has_unconsumed_ack('s1', 'other'))

    def test_expired_ack_still_blocks_reuse_of_its_prompt(self) -> None:
        self._ack('s1', 'push', prompt_id=5)
        self.state.expire_prompt_acks_before('s1', 'push', None)
        self.assertEqual(self._ack('s1', 'push', prompt_id=5), 'prompt_used')


# ---- GateToggle operations ----


class TestGateToggles(_StateTestBase):

    def test_set_gate_toggle_upsert(self) -> None:
        self.state.set_gate_toggle('s1', 'pre-commit', True)
        self.assertTrue(self.state.is_gate_enabled('s1', 'pre-commit'))
        self.state.set_gate_toggle('s1', 'pre-commit', False)
        self.assertFalse(self.state.is_gate_enabled('s1', 'pre-commit'))

    def test_is_gate_enabled_default_true(self) -> None:
        self.assertTrue(self.state.is_gate_enabled('s1', 'nonexistent'))

    def test_toggle_is_per_session(self) -> None:
        self.state.set_gate_toggle('s1', 'g', False)
        self.assertTrue(self.state.is_gate_enabled('s2', 'g'))

    def test_list_gate_toggles(self) -> None:
        self.state.set_gate_toggle('s1', 'alpha', True)
        self.state.set_gate_toggle('s1', 'beta', False)
        names = {t.gate_name: t.enabled for t in self.state.list_gate_toggles('s1')}
        self.assertEqual(names, {'alpha': True, 'beta': False})

    def test_list_gate_toggles_empty(self) -> None:
        self.assertEqual(self.state.list_gate_toggles('s1'), [])


# ---- UserPrompt operations ----


class TestUserPrompts(_StateTestBase):

    def test_ids_increment(self) -> None:
        pid1 = self.state.add_user_prompt('s1', 'first')
        pid2 = self.state.add_user_prompt('s1', 'second')
        self.assertGreater(pid1, 0)
        self.assertGreater(pid2, pid1)

    def test_add_marks_the_session_active_and_creates_it(self) -> None:
        self.state.add_user_prompt('s1', 'first')
        row = self._sql('SELECT cwd, last_active_at FROM sessions WHERE session_id = ?', ('s1',))[0]
        self.assertEqual(row['cwd'], '')
        self.assertIsNotNone(row['last_active_at'])

    def test_add_moves_last_activity_of_an_existing_session(self) -> None:
        seed_session(self.state, 's1', AGED_TIME, AGED_TIME)
        self.state.add_user_prompt('s1', 'x')
        rows = self._sql('SELECT started_at, last_active_at FROM sessions')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['started_at'], AGED_TIME)
        self.assertGreater(rows[0]['last_active_at'], AGED_TIME)

    def test_get_existing_prompt(self) -> None:
        pid = self.state.add_user_prompt('s1', 'push して')
        prompt = self.state.get_user_prompt(pid, 's1')
        self.assertEqual((prompt.id, prompt.session_id, prompt.prompt), (pid, 's1', 'push して'))

    def test_get_prompt_of_another_session_or_unknown_id(self) -> None:
        pid = self.state.add_user_prompt('s1', 'hello')
        self.assertIsNone(self.state.get_user_prompt(pid, 's2'))
        self.assertIsNone(self.state.get_user_prompt(9999, 's1'))

    def test_recent_prompts_are_newest_first_and_limited(self) -> None:
        ids = [self.state.add_user_prompt('s1', t) for t in 'abc']
        self.state.add_user_prompt('s2', 'other')
        recent = self.state.get_recent_prompts('s1', 2)
        self.assertEqual([p.id for p in recent], [ids[2], ids[1]])

    def test_recent_prompts_with_fewer_prompts_than_the_limit(self) -> None:
        pid = self.state.add_user_prompt('s1', 'only')
        self.assertEqual([p.id for p in self.state.get_recent_prompts('s1', 5)], [pid])
        self.assertEqual(self.state.get_recent_prompts('none', 5), [])

    def test_latest_prompt_id(self) -> None:
        self.assertIsNone(self.state.get_latest_prompt_id('s1'))
        self.state.add_user_prompt('s1', 'first')
        pid2 = self.state.add_user_prompt('s1', 'second')
        self.assertEqual(self.state.get_latest_prompt_id('s1'), pid2)


# ---- GateDeny operations ----


class TestGateDenies(_StateTestBase):

    def test_record_then_has_deny(self) -> None:
        self.state.record_deny('s1', 'git_push_gate')
        self.assertTrue(self.state.has_deny('s1', 'git_push_gate'))
        self.assertFalse(self.state.has_deny('s1', 'other'))
        self.assertFalse(self.state.has_deny('s2', 'git_push_gate'))

    def test_upsert_keeps_single_row(self) -> None:
        self.state.record_deny('s1', 'git_push_gate')
        self.state.record_deny('s1', 'git_push_gate')
        self.assertEqual(len(self._sql('SELECT 1 FROM gate_denies')), 1)

    def test_clear(self) -> None:
        self.state.record_deny('s1', 'gate_a')
        self.state.clear_deny('s1', 'gate_a')
        self.assertFalse(self.state.has_deny('s1', 'gate_a'))
        self.state.clear_deny('s1', 'nonexistent_gate')


# ---- Transaction ----


class TestTransaction(_StateTestBase):

    def _insert(self, conn, item: str) -> None:
        conn.execute(
            "INSERT INTO session_checks (session_id, item, reason, checked_at) "
            "VALUES ('s1', ?, 'reason', '2024-01-01T00:00:00+00:00')", (item,),
        )

    def test_rollback_on_error(self) -> None:
        with self.assertRaises(ValueError):
            with self.state.transaction() as conn:
                self._insert(conn, 'rollback-item')
                raise ValueError('force rollback')
        self.assertFalse(self.state.has_session_check('s1', 'rollback-item'))

    def test_commit_on_success(self) -> None:
        with self.state.transaction() as conn:
            self._insert(conn, 'ok-item')
        self.assertTrue(self.state.has_session_check('s1', 'ok-item'))


class TestWriteLock(unittest.TestCase):

    def test_second_writer_waits_for_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, 'lock.sqlite')
            first, second = State(path), State(path)
            try:
                first.init_schema()
                second.init_schema()
                with first.transaction():
                    conn = second._get_conn()
                    conn.execute('PRAGMA busy_timeout = 0')
                    with self.assertRaises(sqlite3.OperationalError):
                        with second.transaction():
                            pass
            finally:
                first.close()
                second.close()


# ---- Nudge firings and gate passes ----


class TestNudgeFirings(_StateTestBase):

    def test_was_nudge_fired_not_recorded(self) -> None:
        self.assertFalse(self.state.was_nudge_fired_this_turn('s1', 'gate1', 42))

    def test_was_nudge_fired_same_prompt_id(self) -> None:
        self.state.record_nudge_firing('s1', 'gate1', 42)
        self.assertTrue(self.state.was_nudge_fired_this_turn('s1', 'gate1', 42))

    def test_new_turn_fires_again(self) -> None:
        self.state.record_nudge_firing('s1', 'gate1', 1)
        self.assertFalse(self.state.was_nudge_fired_this_turn('s1', 'gate1', 2))

    def test_record_upserts(self) -> None:
        self.state.record_nudge_firing('s1', 'gate1', 1)
        self.state.record_nudge_firing('s1', 'gate1', 2)
        self.assertTrue(self.state.was_nudge_fired_this_turn('s1', 'gate1', 2))
        self.assertFalse(self.state.was_nudge_fired_this_turn('s1', 'gate1', 1))


class TestGatePasses(_StateTestBase):

    def test_record_and_read(self) -> None:
        self.assertIsNone(self.state.get_gate_pass_prompt_id('s1', 'g'))
        self.state.record_gate_pass('s1', 'g', 3)
        self.assertEqual(self.state.get_gate_pass_prompt_id('s1', 'g'), 3)

    def test_record_replaces_the_previous_turn(self) -> None:
        self.state.record_gate_pass('s1', 'g', 3)
        self.state.record_gate_pass('s1', 'g', 8)
        self.assertEqual(self.state.get_gate_pass_prompt_id('s1', 'g'), 8)


# ---- Cleanup, table by table ----


class TestCleanup(_StateTestBase):
    """Each table is checked on its own: the other tables hold rows that must survive."""

    def _count(self, session_id: str, table: str) -> int:
        return row_counts(self.state, session_id)[table]

    def test_stale_session_loses_its_row_in_each_table(self) -> None:
        for table in TABLES:
            with self.subTest(table=table):
                sid = f'stale-{table}'
                seed_session(self.state, sid, AGED_TIME, AGED_TIME)
                seed_row(self.state, table, sid, AGED_TIME)
                self.state.cleanup()
                self.assertEqual(self._count(sid, table), 0)
                self.assertEqual(row_counts(self.state, sid)['sessions'], 0)

    def test_recently_used_session_keeps_its_row_in_each_table(self) -> None:
        for table in TABLES:
            with self.subTest(table=table):
                sid = f'resumed-{table}'
                seed_session(self.state, sid, AGED_TIME, NOW)
                seed_row(self.state, table, sid, AGED_TIME)
                self.state.cleanup()
                self.assertEqual(self._count(sid, table), 1)

    def test_session_without_last_activity_is_judged_by_its_start(self) -> None:
        seed_session(self.state, 'old', AGED_TIME)
        seed_session(self.state, 'new', NOW)
        self.state.cleanup()
        self.assertEqual(row_counts(self.state, 'old')['sessions'], 0)
        self.assertEqual(row_counts(self.state, 'new')['sessions'], 1)

    def test_rows_of_unknown_sessions_with_a_time_column_follow_their_own_time(self) -> None:
        for table in ('user_prompts', 'gate_toggles', 'gate_denies', 'session_checks', 'acks'):
            with self.subTest(table=table):
                seed_row(self.state, table, f'ghost-old-{table}', AGED_TIME)
                seed_row(self.state, table, f'ghost-new-{table}', NOW)
                self.state.cleanup()
                self.assertEqual(self._count(f'ghost-old-{table}', table), 0)
                self.assertEqual(self._count(f'ghost-new-{table}', table), 1)

    def test_rows_of_unknown_sessions_without_a_time_column_follow_the_prompts(self) -> None:
        for table in ('nudge_firings', 'gate_passes'):
            with self.subTest(table=table):
                seed_row(self.state, table, f'ghost-lone-{table}', AGED_TIME)
                seed_row(self.state, table, f'ghost-prompted-{table}', AGED_TIME)
                seed_row(self.state, 'user_prompts', f'ghost-prompted-{table}', NOW)
                self.state.cleanup()
                self.assertEqual(self._count(f'ghost-lone-{table}', table), 0)
                self.assertEqual(self._count(f'ghost-prompted-{table}', table), 1)

    def test_returns_the_deleted_count_per_table(self) -> None:
        seed_session(self.state, 'stale', AGED_TIME, AGED_TIME)
        seed_all_rows(self.state, 'stale', AGED_TIME)
        deleted = self.state.cleanup()
        for table in TABLES:
            self.assertEqual(deleted[table], 1, table)
        self.assertEqual(deleted['sessions'], 1)

    def test_nothing_to_delete(self) -> None:
        self._create_session('active')
        self.state.add_user_prompt('active', 'x')
        deleted = self.state.cleanup()
        self.assertEqual(sum(deleted.values()), 0)

    def test_now_argument_moves_the_cutoff(self) -> None:
        self._create_session('s1')
        self.state.add_user_prompt('s1', 'x')
        future = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650)
        self.state.cleanup(now=future)
        self.assertEqual(row_counts(self.state, 's1')['sessions'], 0)
        self.assertEqual(row_counts(self.state, 's1')['user_prompts'], 0)

    def test_each_table_is_aged_by_its_own_retention(self) -> None:
        short, long_ = min(_RETENTION_DAYS.values()), max(_RETENTION_DAYS.values())
        self.assertLess(short, long_)
        middle_days = (short + long_) / 2
        middle = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=middle_days)
        ).isoformat()
        seed_session(self.state, 'mid', middle, middle)
        seed_all_rows(self.state, 'mid', middle)
        self.state.cleanup()
        for table, days in _RETENTION_DAYS.items():
            self.assertEqual(self._count('mid', table), 0 if days < middle_days else 1, table)
        self.assertEqual(row_counts(self.state, 'mid')['sessions'], 1)


if __name__ == '__main__':
    unittest.main()
