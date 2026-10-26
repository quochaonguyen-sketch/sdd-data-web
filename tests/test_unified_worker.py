from datetime import datetime
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from sdd_data.config import Settings
from sdd_data.unified_worker import run_worker


def dummy_settings(root):
    return Settings(root, root, root / "cookie", root / "sa", "https://example.supabase.co", "test-key")


class UnifiedWorkerTests(unittest.TestCase):
    def test_run_worker_invalid_args(self):
        with tempfile.TemporaryDirectory() as td:
            s = dummy_settings(Path(td))
            with self.assertRaises(ValueError):
                run_worker(s, interval=10)  # interval < 60
            with self.assertRaises(ValueError):
                run_worker(s, job_timeout=30)  # timeout < 60

    @patch("sdd_data.unified_worker.process_delivery")
    @patch("sdd_data.unified_worker.process_pickup")
    def test_run_worker_once(self, mock_pickup, mock_delivery):
        mock_delivery.return_value = True
        mock_pickup.return_value = True

        with tempfile.TemporaryDirectory() as td:
            s = dummy_settings(Path(td))
            result = run_worker(s, root=Path(td), once=True, enable_delivery=True, enable_pickup=True)
            self.assertTrue(result)
            mock_delivery.assert_called_once()
            mock_pickup.assert_called_once()

    @patch("sdd_data.unified_worker.PickupStore")
    @patch("sdd_data.unified_worker.process_delivery")
    @patch("sdd_data.unified_worker.process_pickup")
    def test_run_worker_handles_pending_pickup_job(self, mock_pickup, mock_delivery, mock_store_cls):
        mock_store = Mock()
        mock_store.request.return_value = [
            {"id": "job-1", "status": "PENDING", "params": {"kind": "pickup"}}
        ]
        mock_store_cls.return_value = mock_store
        mock_pickup.return_value = True

        stop_evt = threading.Event()

        def stop_after_pickup(*args, **kwargs):
            stop_evt.set()
            return True

        mock_pickup.side_effect = stop_after_pickup

        with tempfile.TemporaryDirectory() as td:
            s = dummy_settings(Path(td))
            run_worker(s, root=Path(td), stop_event=stop_evt, poll_seconds=1, enable_realtime=False)
            mock_pickup.assert_called_once()
            mock_delivery.assert_not_called()
