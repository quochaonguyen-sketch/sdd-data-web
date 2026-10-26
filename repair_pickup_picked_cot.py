"""Reclassify Pickup COT from real pickup_time, leaving official totals/Delivery intact."""
import argparse,copy,json,logging
from pathlib import Path
from unittest.mock import patch
from sdd_data.auth import CookieManager
from sdd_data.clients import SpxClient,read_cookies
from sdd_data.config import Settings,ROOT
from sdd_data.performance import build_records,METRICS,COT_COUNTERS
from sdd_data.performance_cot_cache import save_cots
from sdd_data.transform import as_datetime
p=argparse.ArgumentParser();p.add_argument('day');args=p.parse_args()
s=Settings.load();read_cookies(s.cookie_file)
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(message)s')
source=ROOT/'data'/'performance-cot-backup'/f'picked-time-{args.day}-before.json'
previous=json.loads(source.read_text(encoding='utf8'))
with patch.object(CookieManager,'refresh',side_effect=RuntimeError('Expired SPX session; no browser')):
 client=SpxClient(s)
 try:orders=client.fetch('pickup',args.day)
 finally:client.close()
pickup=save_cots(s,args.day,'pickup',orders,ROOT)
reports={kind:[{'driver_id':r['driver_id'],'driver_name':r['driver_name'],'contract_type_name':r['contract_type_name'],'period_metric':{field:r[key] for key,field in METRICS[kind].items()}} for r in previous] for kind in METRICS}
info={r['driver_id']:{**pickup.get(r['driver_id'],{}),'cot':r.get('cot') or ''} for r in previous}
rows=build_records(args.day,reports[0],reports[2],rider_cots=info)
lookup={r['driver_id']:r for r in previous}
for row in rows:
 old=lookup[row['driver_id']]
 for key in COT_COUNTERS:
  if key.startswith('delivery_'):row[key]=old.get(key) or 0
 breakdown=copy.deepcopy(old.get('cot_breakdown') or {})
 breakdown['pickup']=(row.get('cot_breakdown') or {}).get('pickup',{})
 quality=breakdown.get('quality',{});quality.pop('pickup',None)
 new_quality=(row.get('cot_breakdown') or {}).get('quality',{}).get('pickup')
 if new_quality:quality['pickup']=new_quality
 if quality:breakdown['quality']=quality
 else:breakdown.pop('quality',None)
 row['cot_breakdown']=breakdown
stamp={'before_noon':sum(bool(as_datetime(o.get('pickup_time'))) and as_datetime(o['pickup_time']).hour<12 for o in orders),'from_noon':sum(bool(as_datetime(o.get('pickup_time'))) and as_datetime(o['pickup_time']).hour>=12 for o in orders),'missing_picked_time':sum(as_datetime(o.get('pickup_time')) is None for o in orders)}
target=ROOT/'data'/f'performance-picked-time-corrected-{args.day}.json'
target.write_text(json.dumps({'repair_kind':'cot_only','source_artifact':str(source),'stats':stamp,'records':rows},ensure_ascii=False),encoding='utf8')
print({'day':args.day,'orders':len(orders),'stats':stamp,'artifact':str(target)})
