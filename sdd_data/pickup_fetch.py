"""Parallel, complete live reads with reusable page connections and bounded fan-out."""
from concurrent.futures import ThreadPoolExecutor

from .pickup_cache import PICKUP_PAGE_SIZE, cached_windows, save_windows


def fetch_live_windows(client, path, date_field, start, end, filters=None,
                       id_fields=("id", "pickup_task_id"), *, cache_path=None,
                       namespace=None, initial_windows=None):
    windows = cached_windows(cache_path, namespace, end, start) if cache_path else [(start, end)]
    if windows == [(start, end)] and initial_windows:
        windows = initial_windows
    observed, rows = [], []
    # Page executors are shared across windows instead of reconnecting for each one.
    workers = max(1, min(8, client.settings.inventory_workers))
    interval_workers = max(1, min(12, client.settings.inventory_workers))
    with ThreadPoolExecutor(max_workers=workers) as pages, ThreadPoolExecutor(max_workers=interval_workers) as intervals:
        def read(bounds):
            check = getattr(client, "check_deadline", None)
            if check:
                check()
            completed = []
            items = client.search_window(path, date_field, *bounds, filters, id_fields,
                page_size=PICKUP_PAGE_SIZE, page_pool=pages, max_window_records=PICKUP_PAGE_SIZE,
                on_window=lambda left, right, total: completed.append((left, right, total)))
            return items, completed
        for items, completed in intervals.map(read, windows):
            rows.extend(items)
            observed.extend(completed)
    ids = [str(next((row.get(field) for field in id_fields if row.get(field)), "")) for row in rows]
    if any(not identity for identity in ids) or len(ids) != len(set(ids)):
        raise RuntimeError("SPX lặp/thiếu mã qua khoảng thời gian; giữ snapshot cũ.")
    check = getattr(client, "check_deadline", None)
    if check:
        check()
    if cache_path:
        save_windows(cache_path, namespace, observed)
    return rows
