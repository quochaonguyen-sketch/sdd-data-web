from datetime import datetime
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock,patch

from sdd_data.config import Settings
from sdd_data.performance import METRICS,run_performance
from sdd_data.performance_cot_cache import load_cots,read_cots,save_cots
from sdd_data.pickup_cache import read_cache,write_cache,fingerprint
from sdd_data.performance_cot_cache import cache_file
from sdd_data.transform import TZ

DAY="2026-10-25"
def settings(root):
    return Settings(root,root,root/"cookie",root/"sa","https://example.supabase.co","test-only")
def order(sid,hour):
    stamp=int(datetime(2026,10,25,hour,tzinfo=TZ).timestamp())
    return {"shipment_id":sid,"driver_id":"A","create_time":stamp,"current_station_received_time":stamp,
            "delivering_time":stamp,"pickup_time":stamp,"order_status":4}

class PerformanceCotCacheTests(unittest.TestCase):
    def test_pickup_cache_from_creation_time_rule_is_invalidated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=settings(root)
            save_cots(s,DAY,"pickup",[order("P1",13)],root)
            path=cache_file(root,DAY,"pickup");body=read_cache(path)
            body["context"]=fingerprint([2,s.supabase_url,s.station_id])
            write_cache(path,body)
            self.assertIsNone(read_cots(s,DAY,"pickup",root))

    def test_independent_sources_merge_counts_and_primary_cot_without_overwriting_each_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=settings(root)
            save_cots(s,DAY,"delivery",[order("D1",9),order("D2",15)],root)
            save_cots(s,DAY,"pickup",[order("P1",13)],root)
            with patch("sdd_data.performance_cot_cache.SpxClient",side_effect=AssertionError("Fresh sources already cached")):
                result=load_cots(s,DAY,root)["A"]
            self.assertEqual(result["delivery_assigned_cot1"],1)
            self.assertEqual(result["delivery_assigned_cot2"],1)
            self.assertEqual(result["pickup_assigned_cot2"],1)
            self.assertEqual(result["cot"],"COT 1")

    def test_missing_pickup_cache_fetches_only_pickup_and_closes_client(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=settings(root)
            save_cots(s,DAY,"delivery",[order("D1",9)],root)
            with patch("sdd_data.performance_cot_cache.SpxClient") as client:
                client.return_value.fetch.return_value=[order("P1",13)]
                result=load_cots(s,DAY,root)
            client.return_value.fetch.assert_called_once_with("pickup",DAY)
            client.return_value.close.assert_called_once()
            self.assertEqual(result["A"]["pickup_picked_cot2"],1)

    def test_stale_wrong_scope_and_malformed_caches_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=settings(root);path=cache_file(root,DAY,"delivery")
            save_cots(s,DAY,"delivery",[order("D1",9)],root)
            body=read_cache(path);body["fetched_at"]-=3601;write_cache(path,body)
            self.assertIsNone(read_cots(s,DAY,"delivery",root))
            save_cots(s,DAY,"delivery",[order("D1",9)],root)
            body=read_cache(path);body["cots"]["A"]["delivery_assigned_cot1"]="invalid";write_cache(path,body)
            self.assertIsNone(read_cots(s,DAY,"delivery",root))
            self.assertIsNone(read_cots(s,"2026-10-24","delivery",root))
            save_cots(s,DAY,"delivery",[order("D1",9)],root)
            body=read_cache(path);body["context"]="other-station";write_cache(path,body)
            self.assertIsNone(read_cots(s,DAY,"delivery",root))

    def test_duplicate_source_does_not_replace_valid_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=settings(root)
            save_cots(s,DAY,"delivery",[order("D1",9)],root)
            with self.assertRaises(ValueError):save_cots(s,DAY,"delivery",[order("D1",9),order("D1",9)],root)
            self.assertEqual(read_cots(s,DAY,"delivery",root)["A"]["delivery_assigned_cot1"],1)

    def test_standalone_performance_loads_cots_and_source_failure_prevents_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);s=settings(root)
            report=lambda kind:{"driver_id":"A","period_metric":dict(zip(METRICS[kind].values(),(1,1)))}
            with patch("sdd_data.performance.PerformanceClient") as client,patch("sdd_data.performance.PerformanceStore") as store:
                client.return_value.fetch_report.side_effect=lambda day,kind:[report(kind)]
                with patch("sdd_data.performance_cot_cache.load_cots",side_effect=RuntimeError("COT source unavailable")):
                    with self.assertRaisesRegex(RuntimeError,"COT source unavailable"):
                        run_performance(s,DAY,dry_run=False,root=root)
                store.assert_not_called()
                cot=save_cots(s,DAY,"delivery",[order("D1",9)],root)
                with patch("sdd_data.performance_cot_cache.load_cots",return_value=cot):
                    result=run_performance(s,DAY,dry_run=True,root=root)
                self.assertEqual(result["records"][0]["delivery_assigned_cot1"],1)

if __name__=="__main__":unittest.main()
