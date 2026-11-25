import json
import unittest
from sdd_data.pickup_transport import compact_pickup_payload, GROUP_FIELDS, RECORD_FIELDS


class PickupTransportTests(unittest.TestCase):
    def test_every_published_field_roundtrips_with_shared_groups_and_unicode(self):
        rows = [{**{key: f'Phường {key}' for key in GROUP_FIELDS},
                 **{key: f'{key}-{i}' for key in RECORD_FIELDS}, 'status':'Assigned',
                 'snapshot_id':'redundant', 'snapshot_at':'redundant'} for i in range(100)]
        rows[-1]['ward'] = None
        rows[-1]['status'] = 'Created'
        rows[-1]['assigned_riders_today'] = ''
        payload = {'p_rows':rows, 'p_job':'job', 'p_snapshot':'snapshot', 'p_at':'stamp', 'p_complete':True}
        packed = compact_pickup_payload(payload)
        self.assertEqual(len(packed['p_groups']), 2)
        decoded = [{**dict(zip(RECORD_FIELDS, r[:4])), **dict(zip(GROUP_FIELDS, packed['p_groups'][r[4]])),
                    'status':r[5] if len(r) == 6 else 'Assigned'} for r in packed['p_records']]
        self.assertEqual(decoded, [{k:v for k,v in r.items() if k not in ('snapshot_id','snapshot_at')} for r in rows])
        self.assertEqual(packed['p_snapshot'], 'snapshot')
        self.assertLess(len(json.dumps(packed)), len(json.dumps(payload)) // 2)
        with self.assertRaises(ValueError):
            compact_pickup_payload({'p_rows':[{'status':'Pickup Onhold'}]})

    def test_complete_empty_snapshot_preserves_completion(self):
        self.assertEqual(compact_pickup_payload({'p_rows':[], 'p_complete':True}),
                         {'p_records':[], 'p_groups':[], 'p_complete':True})
