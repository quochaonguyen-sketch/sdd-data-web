"""Publish full HV snapshots on the archive project after the primary queue grants publication."""
from datetime import datetime, timedelta
import re
from urllib.parse import urlsplit

import requests

from .transform import TZ

VERSION_PREFIX = "wh-full:"


def publish_wh_archive(settings, job, day, rows, stamp):
    target = urlsplit(settings.wh_archive_url)
    if (not re.fullmatch(r"https://[a-z0-9]+[.]supabase[.]co", settings.wh_archive_url)
            or target.hostname == urlsplit(settings.supabase_url).hostname or not settings.wh_archive_key):
        raise ValueError("WH: can URL va khoa cua Supabase luu tru rieng.")
    now = datetime.now(TZ)
    if (settings.station_id != "4232" or stamp.tzinfo is None
            or stamp.astimezone(TZ).date().isoformat() != day or now.date().isoformat() != day
            or not now - timedelta(minutes=10) <= stamp <= now + timedelta(minutes=1)):
        raise ValueError("WH: tram hoac thoi gian snapshot khong hop le.")
    if any(not re.fullmatch(r"[A-Z0-9_-]{1,100}", row["shipment_id"])
           or (row["attempt"] is not None and not 0 <= row["attempt"] <= 999999) for row in rows):
        raise ValueError("WH: ma don hoac so lan giao khong hop le.")

    key = {"kind": "eq.wh", "report_date": "eq." + day}
    stamp_text = stamp.isoformat()
    payload = {"kind": "wh", "report_date": day, "version": f"{VERSION_PREFIX}{job}:{stamp_text}",
               "payload": {"report_date": day, "snapshot_at": stamp_text, "station_id": settings.station_id,
                           "job_id": job, "orders": rows}}
    url = settings.wh_archive_url + "/rest/v1/operations_archive_days"
    with requests.Session() as session:
        session.headers.update({"apikey": settings.wh_archive_key, "Authorization": "Bearer " + settings.wh_archive_key})

        def request(method, params, **kwargs):
            try:
                response = session.request(method, url, params=params, timeout=20, allow_redirects=False, **kwargs)
                if not response.ok:
                    raise RuntimeError(f"WH: Supabase luu tru tra HTTP {response.status_code}.")
                return response.json()
            except (requests.RequestException, ValueError):
                raise RuntimeError("WH: loi ket noi hoac phan hoi Supabase luu tru.") from None

        for _ in range(3):
            existing = request("GET", {**key, "select": "version,snapshot_at:payload->>snapshot_at"})
            if existing:
                current = existing[0]
                if current["version"].startswith(VERSION_PREFIX) and datetime.fromisoformat(current["snapshot_at"]) >= stamp:
                    return
                saved = request("PATCH", {**key, "version": "eq." + current["version"], "select": "version"},
                                json=payload, headers={"Prefer": "return=representation"})
            else:
                saved = request("POST", {"on_conflict": "kind,report_date", "select": "version"}, json=payload,
                                headers={"Prefer": "resolution=ignore-duplicates,return=representation"})
            if saved and saved[0]["version"] == payload["version"]:
                return
    raise RuntimeError("WH: snapshot thay doi trong luc ghi. Thu lai o luot tiep theo.")
