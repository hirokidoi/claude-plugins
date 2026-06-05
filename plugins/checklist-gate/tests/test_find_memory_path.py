"""Unit tests for _find_memory_path path normalisation.

Tests the project_key generation logic shared by stop_judge, agent_watcher,
and agent_pre_checker. Each script contains an identical _find_memory_path
implementation; we verify the normalisation rules that determine where each
script looks for MEMORY.md.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _project_key(cwd: str) -> str:
    """Replicate the project_key formula used by all three hook scripts."""
    return cwd.replace('/', '-').replace('_', '-')


def _find_memory_path(cwd: str, home: Path):
    """Replicate _find_memory_path with a configurable home for testing."""
    key = _project_key(cwd)
    base = home / '.claude_config' / 'projects' / key / 'memory' / 'MEMORY.md'
    if base.exists():
        return base
    alt = home / '.claude' / 'projects' / key / 'memory' / 'MEMORY.md'
    if alt.exists():
        return alt
    return None


class TestProjectKeyGeneration(unittest.TestCase):
    """project_key must normalise / and _ to -."""

    def test_slash_replaced_with_hyphen(self):
        self.assertEqual(_project_key('/home/user/myproject'), '-home-user-myproject')

    def test_underscore_replaced_with_hyphen(self):
        self.assertEqual(_project_key('/Volumes/hiroki/capricorn_app'),
                         '-Volumes-hiroki-capricorn-app')

    def test_existing_hyphen_unchanged(self):
        self.assertEqual(_project_key('/home/user/my-project'), '-home-user-my-project')

    def test_mixed_underscore_and_hyphen(self):
        self.assertEqual(_project_key('/home/user/my_cool-project'),
                         '-home-user-my-cool-project')


class TestFindMemoryPathResolution(unittest.TestCase):
    """_find_memory_path must resolve to the correct MEMORY.md."""

    def _make_memory(self, tmp: Path, cwd: str, base: str = '.claude_config') -> Path:
        key = _project_key(cwd)
        mem_dir = tmp / base / 'projects' / key / 'memory'
        mem_dir.mkdir(parents=True)
        p = mem_dir / 'MEMORY.md'
        p.write_text('# test')
        return p

    def test_underscore_in_cwd_finds_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            expected = self._make_memory(home, '/Volumes/hiroki/capricorn_app')
            result = _find_memory_path('/Volumes/hiroki/capricorn_app', home)
            self.assertEqual(result, expected)

    def test_plain_path_finds_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            expected = self._make_memory(home, '/home/user/myproject')
            result = _find_memory_path('/home/user/myproject', home)
            self.assertEqual(result, expected)

    def test_returns_none_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = _find_memory_path('/no/such/project', Path(tmp))
            self.assertIsNone(result)

    def test_falls_back_to_dotclaude(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            expected = self._make_memory(home, '/home/user/myproject', '.claude')
            result = _find_memory_path('/home/user/myproject', home)
            self.assertEqual(result, expected)


if __name__ == '__main__':
    unittest.main()
