from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from sdd_data.config import Settings
from sdd_data.performance import METRICS, PerformanceClient, PerformanceStore, build_records
from sdd_data.runner import run

DAY = "2026-10-23"


def settings(root):
    return Settings(root, root, root / "cookies.json", root / "missing-sa.json",
                    "https://example.supabase.co", "test-only")


def item(driver, kind=0, assigned=10, completed=7):
    return {"driver_id": driver, "driver_name": "Rider " + driver, "contract_type_name": "Fulltime",
            "period_metric": dict(zip(METRICS[kind].values(), (assigned, completed)))}


class PerformanceTests(unittest.TestCase):
    def test_cot_scope_mismatch_does_not_exceed_daily_total_or_modify_source_cache(self):
        info={"A":{"cot":"COT 1","delivery_assigned_cot1":50,"delivery_assigned_cot2":10,
                   "delivery_delivered_cot1":40,"delivery_delivered_cot2":5,
                   "breakdown":{"delivery":{"COT 1":{"assigned":50,"delivered":40}}}}}
        rows=build_records(DAY,[item("A",assigned=10,completed=7)],[],rider_cots=info)
        self.assertEqual(rows[0]["delivery_assigned"],10)
        self.assertEqual(rows[0]["delivery_assigned_cot1"],0)
        self.assertEqual(rows[0]["cot_breakdown"]["quality"]["delivery"],"source_scope_mismatch")
        self.assertTrue(info["A"]["breakdown"]["delivery"])

    def test_partial_cot_completion_is_not_fabricated_to_match_official_total(self):
        info={"A":{"cot":"COT 1","delivery_assigned_cot1":6,"delivery_assigned_cot2":4,
                   "delivery_delivered_cot1":2,"delivery_delivered_cot2":1}}
        row=build_records(DAY,[item("A",assigned=10,completed=7)],[],rider_cots=info)[0]
        self.assertEqual((row["delivery_delivered_cot1"],row["delivery_delivered_cot2"]),(2,1))
        self.assertEqual(row["delivery_delivered"],7)

    def test_merges_delivery_pickup_and_one_sided_riders_by_id(self):
        rows = build_records(DAY, [item("A"), item("B", assigned=0, completed=0)],
                             [item("A", 2, 20, 15), item("C", 2)])
        a, b, c = rows
        self.assertEqual(a["performance_id"], DAY + "|A")
        self.assertEqual(a["delivery_success_rate"], 0.7)
        self.assertEqual(a["pickup_success_rate"], 0.75)
        self.assertEqual(c["delivery_assigned"], 0)
        self.assertIsNone(b["delivery_success_rate"])
        self.assertEqual(b["pickup_assigned"], 0)

    def test_missing_duplicate_and_invalid_metrics_fail(self):
        for rows in ([item("")], [item("A"), item("A")],
                     [{**item("A"), "period_metric": {}}],
                     [item("A", assigned=-1)], [item("A", assigned=1.5)]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                build_records(DAY, rows, [])

    def client(self, responses):
        client = PerformanceClient.__new__(PerformanceClient)
        client.post = Mock(side_effect=responses)
        return client

    def test_fetches_all_pages_and_uses_launcher2_payload(self):
        client = self.client([{"total": 3, "count": 2, "list": [item("A"), item("B")]},
                              {"total": 3, "count": 2, "list": [item("C")]}])
        self.assertEqual(len(client.fetch_report(DAY, 0)), 3)
        self.assertEqual(client.post.call_count, 2)
        self.assertEqual(client.post.call_args.args[0],
                         {"count": 100, "frequency": 1, "start_date": DAY, "function_type": 0, "pageno": 2})

    def test_incomplete_duplicate_and_changing_pages_fail(self):
        first = {"total": 3, "count": 2, "list": [item("A"), item("B")]}
        for second in ({"total": 3, "count": 2, "list": []},
                       {"total": 3, "count": 2, "list": [item("A")]},
                       {"total": 4, "count": 2, "list": [item("C"), item("D")]},
                       {"total": 3, "count": 0, "list": [item("C")]}):
            with self.subTest(second=second), self.assertRaises(RuntimeError):
                self.client([first, second]).fetch_report(DAY, 0)

    def test_zero_report_and_discard_unneeded_fields(self):
        self.assertEqual(self.client([{"total": 0, "count": 100, "list": []}]).fetch_report(DAY, 2), [])
        client = self.client([{"total": 1, "list": [{**item("A"), "private_field": "discard"}]}])
        self.assertNotIn("private_field", client.fetch_report(DAY, 0)[0])

    def store(self):
        store = PerformanceStore.__new__(PerformanceStore)
        store.settings = replace(settings(Path(".")), batch_size=1)
        return store

    def test_failed_second_upsert_never_deletes_old_snapshot(self):
        store = self.store()
        store.request = Mock(side_effect=[[], [{"performance_id": DAY + "|old"}], None, RuntimeError("write failed")])
        with self.assertRaisesRegex(RuntimeError, "write failed"):
            store.commit_snapshot(DAY, build_records(DAY, [item("A"), item("B")], []))
        self.assertFalse(any(call.args[0] == "DELETE" for call in store.request.call_args_list))

    def test_schema_failure_prevents_all_writes(self):
        store = self.store()
        store.request = Mock(side_effect=RuntimeError("schema mismatch"))
        with self.assertRaisesRegex(RuntimeError, "schema mismatch"):
            store.commit_snapshot(DAY, build_records(DAY, [item("A")], []))
        self.assertEqual(store.request.call_count, 1)
        self.assertEqual(store.request.call_args.args[0], "GET")

    def test_cleanup_occurs_after_upserts_and_only_for_selected_date(self):
        store = self.store()
        store.request = Mock(side_effect=[[], [{"performance_id": DAY + "|old"}], None, None, None])
        result = store.commit_snapshot(DAY, build_records(DAY, [item("A"), item("B")], []))
        self.assertEqual(result, {"upserted": 2, "stale_removed": 1})
        calls = store.request.call_args_list
        self.assertEqual([c.args[0] for c in calls], ["GET", "GET", "POST", "POST", "DELETE"])
        self.assertEqual(calls[-1].args[2]["report_date"], "eq." + DAY)
        self.assertIn(DAY + "|old", calls[-1].args[2]["performance_id"])
        self.assertTrue(all(c.args[1] == "driver_performance_daily" for c in calls))

    def test_invalid_or_empty_snapshot_never_accesses_database(self):
        for rows in ([], build_records("2026-10-22", [item("A")], [])):
            store = self.store()
            store.request = Mock()
            with self.assertRaises(ValueError):
                store.commit_snapshot(DAY, rows)
            store.request.assert_not_called()

    def test_offline_performance_requires_no_cookie_mapping_or_db(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            fixture = root / "fixture.json"
            fixture.write_text(json.dumps({"performance": {"delivery": [item("A")], "pickup": [item("A", 2)]}}))
            with patch("sdd_data.runner.load_coverage", side_effect=AssertionError("Mapping not needed")), \
                 patch("sdd_data.performance.PerformanceClient", side_effect=AssertionError("SPX not needed")), \
                 patch("sdd_data.performance.PerformanceStore", side_effect=AssertionError("DB not needed")):
                result = run(settings(root), "performance", DAY, dry_run=True, orders_json=fixture, root=root)
            self.assertEqual(result[0]["stats"]["riders"], 1)
            self.assertEqual(result[0]["status"], "dry_run")
            self.assertTrue(list((root / "data").glob("performance-*.json")))

    def test_fetch_failure_never_constructs_store(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch("sdd_data.performance.PerformanceClient") as client, \
                 patch("sdd_data.performance.PerformanceStore") as store:
                client.return_value.fetch_report.side_effect = RuntimeError("incomplete source")
                with self.assertRaisesRegex(RuntimeError, "incomplete source"):
                    run(settings(root), "performance", DAY, root=root)
                store.assert_not_called()
                client.return_value.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
