"""Independent volume jobs share only validated rider COT aggregates, never raw orders."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import time

from .clients import SpxClient
from .cot_classifier import compute_rider_cot_breakdown
from .performance import COT_COUNTERS
from .pickup_cache import fingerprint, read_cache, write_cache
from .transform import TZ


def cache_file(root, day, kind):
    return Path(root)/"data"/"performance-cot"/f"{day}-{kind}.json"


def context(settings,kind=None):
    version="pickup-picked-at-noon-v3" if kind=="pickup" else 2
    return fingerprint([version,settings.supabase_url,settings.station_id])


def save_cots(settings, day, kind, orders, root):
    if kind not in ("delivery","pickup"):
        raise ValueError("Invalid COT source")
    ids=[str(row.get("shipment_id") or "").strip() for row in orders]
    if any(not identity for identity in ids) or len(ids)!=len(set(ids)):
        raise ValueError("Nguồn COT thiếu/trùng mã đơn; giữ dữ liệu Performance cũ.")
    result=compute_rider_cot_breakdown(**{f"{kind}_orders":orders})
    stats={driver:{key:row[key] for key in ("cot",*COT_COUNTERS,"breakdown")} for driver,row in result.items()}
    write_cache(cache_file(root,day,kind),{"context":context(settings,kind),"date":day,"kind":kind,
        "fetched_at":time.time(),"orders":len(orders),"cots":stats})
    return stats


def read_cots(settings, day, kind, root):
    body=read_cache(cache_file(root,day,kind))
    ttl=600 if day==datetime.now(TZ).date().isoformat() else 3600
    fetched=body.get("fetched_at")
    if body.get("context")!=context(settings,kind) or body.get("date")!=day or body.get("kind")!=kind:
        return None
    if not isinstance(fetched,(int,float)) or not 0<=time.time()-fetched<=ttl or not isinstance(body.get("cots"),dict):
        return None
    for row in body["cots"].values():
        if not isinstance(row,dict) or not isinstance(row.get("breakdown"),dict): return None
        if any(type(row.get(key)) is not int or row[key]<0 for key in COT_COUNTERS): return None
    return body["cots"]


def load_cots(settings, day, root, supplied_orders=None):
    supplied_orders=supplied_orders or {}
    def read(kind):
        if kind in supplied_orders:
            return kind,save_cots(settings,day,kind,supplied_orders[kind],root)
        saved=read_cots(settings,day,kind,root)
        if saved is not None:
            return kind,saved
        client=SpxClient(settings)
        try:
            orders=client.fetch(kind,day)
            return kind,save_cots(settings,day,kind,orders,root)
        finally:
            client.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        parts=dict(pool.map(read,("delivery","pickup")))
    merged={}
    for driver in parts["delivery"].keys()|parts["pickup"].keys():
        delivery,pickup=parts["delivery"].get(driver,{}),parts["pickup"].get(driver,{})
        merged[driver]={"cot":delivery.get("cot") or pickup.get("cot") or "",
            **{key:delivery.get(key,0)+pickup.get(key,0) for key in COT_COUNTERS},
            "breakdown":{"delivery":delivery.get("breakdown",{}).get("delivery",{}),
                         "pickup":pickup.get("breakdown",{}).get("pickup",{})}}
    return merged
