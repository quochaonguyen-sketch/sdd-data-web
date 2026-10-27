import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from sdd_data.source_job import Boundary, Cancelled, execute
from sdd_data.source_worker import child_command, bridge_legacy
from sdd_data.source_control import SOURCES


class SourceTests(unittest.TestCase):
    def test_stop_file_shuts_down_daemon_without_claiming_more_work(self):
        from sdd_data.source_worker import run_worker
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            stop = threading.Event()
            control = Mock()
            control.pending_inventory.return_value = []
            def rpc(name, **kwargs):
                if name == 'pulse_data_source_worker':
                    (root / 'data' / 'source-worker' / 'stop.request').touch()
                    return True
                return None
            control.rpc.side_effect = rpc
            with patch('sdd_data.source_worker.Control', return_value=control), patch('sdd_data.source_worker.SessionControl'), patch.object(stop, 'wait', return_value=False), patch('sdd_data.source_worker.time.sleep'):
                run_worker(Mock(), root=root, stop_event=stop)
            self.assertTrue(stop.is_set())
            self.assertEqual(sum(c.args[0] == 'claim_data_source_job' for c in control.rpc.call_args_list), 1)
            control.close.assert_called_once()
            self.assertFalse((root / 'data' / 'source-worker' / 'stop.request').exists())

    def test_second_worker_reports_that_background_worker_is_already_running(self):
        from sdd_data.runner import RunLock
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / 'source-worker' / 'daemon.lock'
            with RunLock(lock):
                with self.assertRaisesRegex(RuntimeError, 'Worker trung tâm đã chạy nền'):
                    with RunLock(lock):
                        self.fail('Duplicate worker acquired daemon lock')

    def test_only_nine_sources_and_argv_without_shell(self):
        self.assertEqual(len(SOURCES), 9)
        for source in SOURCES:
            command = child_command({'source_id': source, 'id': '123', 'report_date': '2026-10-25'}, 'owner', Path('cancel'))
            self.assertIn(source, command)
            self.assertEqual(command[2:4], ['-m', 'sdd_data.source_job'])
        with self.assertRaises(ValueError):
            child_command({'source_id': 'cmd.exe'}, 'owner', Path('cancel'))

    def test_cancel_before_publish_but_finish_publication_after_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cancel'
            control = Mock()
            boundary = Boundary(control, 'job', 'worker', path)
            path.touch()
            with self.assertRaises(Cancelled):
                boundary.begin()
            control.rpc.assert_not_called()
            path.unlink()
            control.rpc.return_value = True
            boundary.begin()
            path.touch()
            boundary.check()  # Current publication must finish, not abort mid-write.
            boundary.begin()
            control.rpc.assert_called_once()

    def test_database_refusal_prevents_publication(self):
        with tempfile.TemporaryDirectory() as folder:
            control = Mock()
            control.rpc.return_value = False
            boundary = Boundary(control, 'job', 'worker', Path(folder) / 'cancel')
            with self.assertRaises(Cancelled):
                boundary.begin()
            self.assertFalse(boundary.publishing)

    def test_existing_web_fetch_buttons_bridge_to_central_queue(self):
        control = Mock()
        control.pending_inventory.return_value = [{'params': {'kind': 'pickup'}, 'requested_by': None}, {'params': {'kind': 'delivery'}, 'requested_by': None}]
        bridge_legacy(control)
        self.assertEqual([call.kwargs['p_source'] for call in control.rpc.call_args_list if 'p_source' in call.kwargs], ['pickup_inventory', 'delivery_inventory'])

    def test_return_adapter_only_publishes_complete_source(self):
        with tempfile.TemporaryDirectory() as folder:
            boundary = Mock(publishing=False, job='job', worker='worker')
            boundary.begin.side_effect = lambda: setattr(boundary, 'publishing', True)
            settings = Mock(station_id='4232')
            with patch('sdd_data.return_inventory.prepare_returns', return_value=([], {'source_complete':False})), self.assertRaises(RuntimeError):
                execute(settings, 'return_inventory', '2026-10-25', boundary, root=Path(folder))
            boundary.control.rpc.assert_not_called()
            with patch('sdd_data.return_inventory.prepare_returns', return_value=([], {'source_complete':True})):
                execute(settings, 'return_inventory', '2026-10-25', boundary, root=Path(folder))
            self.assertEqual(boundary.control.rpc.call_args.args[0], 'publish_central_returns')

    def test_volume_adapter_crosses_boundary_before_commit(self):
        with tempfile.TemporaryDirectory() as folder:
            boundary = Mock(publishing=False)
            order = []
            def begin():
                order.append('boundary')
                boundary.publishing = True
            boundary.begin.side_effect = begin
            from sdd_data.clients import WebsiteStore
            def run(*args, **kwargs):
                WebsiteStore.commit_snapshot(None, 'pickup', '2026-10-25', [])
            with patch('sdd_data.runner.run', run), patch.object(WebsiteStore, 'commit_snapshot', side_effect=lambda *args: order.append('write')):
                execute(Mock(), 'pickup_volume', '2026-10-25', boundary, root=Path(folder))
            self.assertEqual(order, ['boundary', 'write'])

    def test_other_two_dated_adapters_publish_only_after_boundary(self):
        from sdd_data.clients import WebsiteStore
        from sdd_data.performance import PerformanceStore
        for source, target in [('delivery_volume', WebsiteStore), ('performance', PerformanceStore)]:
            with self.subTest(source=source), tempfile.TemporaryDirectory() as folder:
                boundary = Mock(publishing=False)
                order = []
                def begin():
                    order.append('boundary')
                    boundary.publishing = True
                boundary.begin.side_effect = begin
                def run(*args, **kwargs):
                    target.commit_snapshot(None, '2026-10-25', [])
                with patch('sdd_data.runner.run', run), patch.object(target, 'commit_snapshot', side_effect=lambda *args: order.append('write')):
                    execute(Mock(), source, '2026-10-25', boundary, root=Path(folder))
                self.assertEqual(order, ['boundary', 'write'])

    def test_realtime_and_inventory_adapters_use_their_expected_publishers(self):
        with tempfile.TemporaryDirectory() as folder:
            boundary = Mock(publishing=False, job='job', worker='worker')
            boundary.begin.side_effect = lambda: setattr(boundary, 'publishing', True)
            boundary.control.rpc.return_value = {'id': 'legacy', 'worker_id': 'legacy-owner'}
            settings = Mock(areas=('KV1',))
            with patch('sdd_data.pickup_inventory.prepare_pickup_inventory', return_value=([{'tracking_id': 'TEST', 'status': 'Assigned', 'area': 'KV1'}], {'source_complete': True})):
                execute(settings, 'pickup_inventory', '2026-10-25', boundary, root=Path(folder))
            self.assertEqual(boundary.control.rpc.call_args.args[0], 'publish_central_pickup')
            boundary.publishing = False
            with patch('sdd_data.delivery_inventory.prepare_inventory', return_value=([{'shipment_id': 'TEST'}], {})), patch('sdd_data.delivery_inventory.InventoryStore') as store:
                execute(settings, 'delivery_inventory', '2026-10-25', boundary, root=Path(folder))
                self.assertEqual(store.return_value.rpc.call_args.args[0], 'publish_sdd_delivery_inventory')
            from sdd_data.pickup_realtime import RealtimeStore
            boundary.publishing = False
            def sync(*args, **kwargs):
                RealtimeStore.request(None, 'POST', 'pickup_realtime_current', {}, [{}])
            with patch('sdd_data.pickup_realtime.sync_once', sync), patch.object(RealtimeStore, 'request') as publish:
                execute(settings, 'pickup_realtime', '2026-10-25', boundary, root=Path(folder))
                publish.assert_called_once()
            self.assertTrue(boundary.publishing)


if __name__ == '__main__':
    unittest.main()
