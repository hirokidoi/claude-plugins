"""SessionStart hook: data files, policy notice and housekeeping."""
import os
import tempfile
import unittest

from tests.hook_harness import (
    AGED_TIME, DB_NAME, SESSION_INIT, TABLES, example_policy, row_counts, run_hook, seed_all_rows,
    seed_row, seed_session, write_policy,
)
from state import State

NOW = '2999-01-01T00:00:00+00:00'


class _InitCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = os.path.join(self._tmp.name, 'plugin-data')

    def tearDown(self):
        self._tmp.cleanup()

    def start(self, session_id: str = 's1'):
        return run_hook(SESSION_INIT, {'session_id': session_id, 'cwd': '/repo'}, self.data_dir)

    def open_state(self) -> State:
        os.makedirs(self.data_dir, exist_ok=True)
        state = State(os.path.join(self.data_dir, DB_NAME))
        state.init_schema()
        self.addCleanup(state.close)
        return state


class TestFirstRun(_InitCase):

    def test_missing_data_directory_is_created_with_the_default_source(self):
        result = self.start()
        self.assertEqual((result.returncode, result.stderr.count('Traceback')), (0, 0))
        self.assertTrue(os.path.isfile(os.path.join(self.data_dir, 'policy-source.md')))
        self.assertTrue(os.path.isfile(os.path.join(self.data_dir, DB_NAME)))
        self.assertIn(self.data_dir, result.context)

    def test_existing_source_is_not_overwritten(self):
        os.makedirs(self.data_dir)
        path = os.path.join(self.data_dir, 'policy-source.md')
        with open(path, 'w') as f:
            f.write('mine')
        self.start()
        with open(path) as f:
            self.assertEqual(f.read(), 'mine')

    def test_session_row_is_recorded(self):
        self.start('s1')
        row = self.open_state()._get_conn().execute(
            'SELECT cwd, last_active_at FROM sessions WHERE session_id = ?', ('s1',)
        ).fetchone()
        self.assertEqual(row['cwd'], '/repo')
        self.assertIsNotNone(row['last_active_at'])

    def test_second_start_marks_the_session_active_again(self):
        self.start('s1')
        state = self.open_state()
        state._get_conn().execute("UPDATE sessions SET last_active_at = ?", (AGED_TIME,))
        state._get_conn().commit()
        self.start('s1')
        row = state._get_conn().execute('SELECT last_active_at FROM sessions').fetchone()
        self.assertGreater(row[0], AGED_TIME)

    def test_invalid_stdin_does_nothing(self):
        result = run_hook(SESSION_INIT, 'nonsense', self.data_dir)
        self.assertEqual((result.returncode, result.stdout), (0, ''))


class TestPolicyNotice(_InitCase):

    def test_missing_policy_is_announced_to_user_and_agent(self):
        result = self.start()
        self.assertIn('未作成', result.context)
        self.assertIn('未作成', result.output['systemMessage'])
        self.assertIn('apply-config', result.output['systemMessage'])

    def test_valid_policy_is_not_announced(self):
        os.makedirs(self.data_dir)
        write_policy(self.data_dir, example_policy())
        result = self.start()
        self.assertNotIn('systemMessage', result.output)
        self.assertNotIn('未作成', result.context)
        self.assertNotIn('不正', result.context)

    def test_invalid_policy_is_announced_with_its_problems(self):
        os.makedirs(self.data_dir)
        write_policy(self.data_dir, {'unknown_key': 1})
        result = self.start()
        for text in (result.context, result.output['systemMessage']):
            self.assertIn('不正', text)
            self.assertIn('unknown_key', text)
            self.assertIn('読み取り専用のツール以外', text)

    def test_unparseable_policy_is_announced(self):
        os.makedirs(self.data_dir)
        write_policy(self.data_dir, '{"gates": [')
        self.assertIn('不正', self.start().output['systemMessage'])

    def test_context_tells_where_the_validation_script_is(self):
        self.assertIn('validate_policy.py', self.start().context)


class TestWhenSomethingFails(_InitCase):

    def test_broken_database_still_produces_the_output(self):
        os.makedirs(self.data_dir)
        with open(os.path.join(self.data_dir, DB_NAME), 'wb') as f:
            f.write(b'not a database' * 100)
        result = self.start()
        self.assertEqual(result.returncode, 0)
        self.assertIn(self.data_dir, result.context)
        self.assertIn('未作成', result.output['systemMessage'])
        self.assertNotEqual(result.stderr, '')

    def test_data_directory_that_cannot_be_created_still_produces_the_output(self):
        blocker = os.path.join(self._tmp.name, 'blocker')
        with open(blocker, 'w') as f:
            f.write('a file where a directory is needed')
        self.data_dir = os.path.join(blocker, 'plugin-data')
        result = self.start()
        self.assertEqual(result.returncode, 0)
        self.assertIsNotNone(result.output)
        self.assertIn(self.data_dir, result.context)


class TestHousekeeping(_InitCase):

    def test_session_started_long_ago_but_used_recently_keeps_its_rows(self):
        state = self.open_state()
        seed_session(state, 'resumed', AGED_TIME, last_active_at=NOW)
        seed_all_rows(state, 'resumed', AGED_TIME)
        self.start()
        counts = row_counts(state, 'resumed')
        self.assertEqual(counts, {table: 1 for table in counts})

    def test_unused_session_loses_rows_in_every_table(self):
        state = self.open_state()
        seed_session(state, 'stale', AGED_TIME, last_active_at=AGED_TIME)
        seed_all_rows(state, 'stale', AGED_TIME)
        self.start()
        counts = row_counts(state, 'stale')
        self.assertEqual(counts, {table: 0 for table in counts})
        self.assertEqual(set(TABLES) | {'sessions'}, set(counts))

    def test_session_without_last_active_falls_back_to_its_start(self):
        state = self.open_state()
        seed_session(state, 'legacy', AGED_TIME)
        seed_session(state, 'legacy-new', NOW)
        seed_all_rows(state, 'legacy', AGED_TIME)
        seed_all_rows(state, 'legacy-new', NOW)
        self.start()
        self.assertEqual(sum(row_counts(state, 'legacy').values()), 0)
        self.assertEqual(sum(row_counts(state, 'legacy-new').values()), len(TABLES) + 1)

    def test_rows_of_an_unknown_session_are_judged_by_their_own_time(self):
        state = self.open_state()
        seed_row(state, 'user_prompts', 'ghost-old', AGED_TIME)
        seed_row(state, 'user_prompts', 'ghost-new', NOW)
        self.start()
        self.assertEqual(row_counts(state, 'ghost-old')['user_prompts'], 0)
        self.assertEqual(row_counts(state, 'ghost-new')['user_prompts'], 1)

    def test_rows_without_a_time_column_follow_the_prompts_of_their_session(self):
        state = self.open_state()
        for table in ('nudge_firings', 'gate_passes'):
            seed_row(state, table, 'ghost-orphan', AGED_TIME)
            seed_row(state, table, 'ghost-kept', AGED_TIME)
        seed_row(state, 'user_prompts', 'ghost-kept', NOW)
        self.start()
        self.assertEqual(row_counts(state, 'ghost-orphan')['nudge_firings'], 0)
        self.assertEqual(row_counts(state, 'ghost-orphan')['gate_passes'], 0)
        self.assertEqual(row_counts(state, 'ghost-kept')['nudge_firings'], 1)
        self.assertEqual(row_counts(state, 'ghost-kept')['gate_passes'], 1)

    def test_the_starting_session_is_never_removed(self):
        self.start('fresh')
        self.start('fresh')
        self.assertEqual(row_counts(self.open_state(), 'fresh')['sessions'], 1)


if __name__ == '__main__':
    unittest.main()
