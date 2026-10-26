import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from sdd_data.source_background import start_background


class BackgroundTests(unittest.TestCase):
    def test_already_running_does_not_spawn_duplicate(self):
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.source_background.is_running', return_value=True), patch('sdd_data.source_background.subprocess.Popen') as spawn:
            self.assertEqual(start_background(Path(folder)), 0)
            spawn.assert_not_called()

    def test_child_startup_failure_is_reported(self):
        child = Mock()
        child.poll.return_value = 1
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.source_background.is_running', return_value=False), patch('sdd_data.source_background.subprocess.Popen', return_value=child) as spawn:
            self.assertEqual(start_background(Path(folder)), 1)
            self.assertEqual(spawn.call_args.kwargs['stdin'], -3)
            self.assertNotIn('shell', spawn.call_args.kwargs)

    def test_success_waits_for_daemon_lock(self):
        child = Mock()
        child.poll.return_value = None
        with tempfile.TemporaryDirectory() as folder, patch('sdd_data.source_background.is_running', side_effect=[False, True]), patch('sdd_data.source_background.subprocess.Popen', return_value=child):
            self.assertEqual(start_background(Path(folder)), 0)


if __name__ == '__main__':
    unittest.main()
