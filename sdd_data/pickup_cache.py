"""Disposable local caches. Live SPX status is never inferred from cached orders."""
import hashlib
import json
import logging
import os
from pathlib import Path
from uuid import uuid4

log = logging.getLogger(__name__)
PICKUP_PAGE_SIZE = 500  # Verified against SPX order and task search endpoints.


def read_cache(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_cache(path, value):
    path = Path(path)
    pending = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        pending.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(pending, path)
    except OSError:
        log.warning("Không lưu được cache cục bộ; lượt sau sẽ đọc lại nguồn.")
    finally:
        try:
            pending.unlink(missing_ok=True)
        except OSError:
            pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def cached_windows(path, station, end, start=0):
    body = read_cache(path)
    windows = body.get("windows")
    if body.get("version") != 1 or body.get("station") != station or not isinstance(windows, list):
        return [(start, end)]
    cursor, result = start, []
    for item in windows:
        if (not isinstance(item, list) or len(item) != 2 or
                any(type(v) is not int for v in item)):
            return [(start, end)]
        left, right = item
        if left != cursor or right < left or right > end:
            return [(start, end)]
        result.append((left, right))
        cursor = right + 1
    if cursor <= end:
        result.append((cursor, end))
    return result or [(start, end)]


def save_windows(path, station, windows):
    # Empty adjacent historical windows can share a single fresh probe next run.
    merged = []
    for left, right, total in windows:
        if (merged and merged[-1][1] + 1 == left and
                ((not total and not merged[-1][2]) or
                 (total > 0 and merged[-1][2] > 0 and total + merged[-1][2] <= PICKUP_PAGE_SIZE))):
            merged[-1][1] = right
            merged[-1][2] += total
        else:
            merged.append([left, right, total])
    write_cache(path, {"version": 1, "station": station,
                       "windows": [[left, right] for left, right, _ in merged]})


class PickupRecordCache:
    def __init__(self, path, context):
        self.path = path
        self.context = fingerprint(context)
        body = read_cache(path)
        self.previous = body.get("records", {}) if body.get("context") == self.context else {}
        if not isinstance(self.previous, dict):
            self.previous = {}
        self.current = {}
        self.hits = 0

    def get(self, identity, signature):
        entry = self.previous.get(identity)
        if (isinstance(entry, dict) and entry.get("signature") == signature
                and isinstance(entry.get("record"), dict)):
            self.hits += 1
            self.put(identity, signature, entry["record"])
            return dict(entry["record"])
        return None

    def put(self, identity, signature, record):
        if len(self.current) < 50000:
            self.current[identity] = {"signature": signature,
                "record": {k: v for k, v in record.items() if k not in ("snapshot_id", "snapshot_at")}}

    def save(self):
        # Only orders present in this complete live read survive into the next run.
        write_cache(self.path, {"context": self.context, "records": self.current})
