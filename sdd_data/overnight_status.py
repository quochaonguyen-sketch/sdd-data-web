"""Refresh frozen overnight orders through read-only SPX tracking lookups."""
from datetime import datetime
import logging
import threading

from .clients import SpxClient
from .source_control import Control
from .transform import as_datetime

log = logging.getLogger(__name__)


def current_status(row, order, station):
    raw = str(row.get('order_status', ''))
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
        raise RuntimeError('SPX thiếu mã trạng thái; giữ nguyên dữ liệu cũ.')
    status = {'2': 'Delivering', '4': 'Delivered', '5': 'Onhold',
              '10': 'Returned', '67': 'Returned', '72': 'Returned'}.get(raw, f'SPX ({raw})')
    if raw == '1':
        received = as_datetime(row.get('current_station_received_time'))
        # A pre-cutoff receipt does not prove the rider handed this parcel back.
        returned = (str(row.get('current_station_id')) == str(station)
                    and received and received >= datetime.fromisoformat(order['created_at']))
        status = 'Returned' if returned else 'LMHub_Received'
    return {'order_code': order['order_code'], 'current_status': status}


def lookup_statuses(client, orders):
    results = []
    for offset in range(0, len(orders), 24):
        batch = orders[offset:offset+24]
        codes = [order['order_code'] for order in batch]
        data = client.post({'count': 24, 'page_no': 1, 'search_id_list': codes})
        rows = data['list']
        if int(data['total']) != len(batch) or len(rows) != len(batch):
            raise RuntimeError('SPX trả thiếu mã đơn; giữ nguyên trạng thái cũ.')
        matched = {}
        for row in rows:
            ids = {str(row.get('shipment_id') or ''), str(row.get('sls_tracking_number') or '')}
            matches = ids.intersection(codes)
            if len(matches) != 1 or next(iter(matches)) in matched:
                raise RuntimeError('SPX trả mã sai hoặc trùng; giữ nguyên trạng thái cũ.')
            matched[next(iter(matches))] = row
        for order in batch:
            results.append(current_status(matched[order['order_code']], order, client.settings.station_id))
    return results


def sync_once(settings):
    control = Control(settings)
    client = None
    claim = None
    try:
        claim = control.rpc('claim_overnight_status_sync')
        if not claim:
            return None
        rows = []
        if claim['orders']:
            client = SpxClient(settings)
            rows = lookup_statuses(client, claim['orders'])
        result = control.rpc('finish_overnight_status_sync', p_day=claim['day'], p_token=claim['token'], p_rows=rows)
        log.info('Overnight SPX: day=%s, synced=%s, resolved=%s', claim['day'], len(rows), result['resolved_count'])
        return result
    except Exception:
        if claim:
            # Persist a safe message; remote exception payloads can contain credentials.
            control.rpc('finish_overnight_status_sync', p_day=claim['day'], p_token=claim['token'], p_rows=[],
                        p_error='Không đọc đủ trạng thái từ SPX. Kiểm tra kết nối/phiên SPX; hệ thống sẽ tự thử lại sau 5 phút.')
        raise
    finally:
        if client:
            client.close()
        control.close()


def run_worker(settings, *, stop_event=None):
    stop = stop_event or threading.Event()
    while not stop.is_set():
        try:
            sync_once(settings)
        except Exception:
            log.warning('Overnight SPX sync failed; retained previous statuses. Check SPX session and database configuration.')
        stop.wait(5)
