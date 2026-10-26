"""OH Delivery daily pivot. Order identifiers exist in RAM only; no raw artifacts."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
import logging
import hashlib
from .clients import SpxClient, load_coverage
from .transform import TZ, as_datetime, match_zone, operating_area

REASONS = {'169': 'Recipient unavailable for parcel', '170': 'Recipient reschedule', '172': 'Recipient reject',
           '13': 'Buyer Never Place', '185': 'Insufficient time', '187': 'Severe weather',
           '180': 'Parcel lost', '182': 'Unable to Deliver', '184': 'Wrongly assigned'}
UNKNOWN = 'Chưa rõ KV'


def first(row, *keys):
    return next((str(row[k]).strip() for k in keys if row.get(k) is not None and str(row[k]).strip()), '')


def station_matches(row, station, home, day):
    # Same rule as legacy launcher, but compare to requested report day for backfills.
    if str(row.get('current_station_id') or '') == str(station) or first(row, 'current_station_name', 'station_name', 'current_station') == home:
        return True
    received = as_datetime(row.get('current_station_received_time'))
    held = as_datetime(row.get('on_hold_time') or row.get('onhold_time'))
    return bool(received and held and received.date() == day and received > held)


class OnholdClient(SpxClient):
    def fetch(self, day):
        start = int(datetime.combine(day, time.min, TZ).timestamp())
        end = min(int((datetime.combine(day + timedelta(days=1), time.min, TZ)).timestamp()) - 1,
                  int(datetime.now(TZ).timestamp()))
        base = {'delivering_time': f'{start},{end}', 'on_hold_times': '1,10', 'order_status': '5,1', 'count': 200}
        # Changing OH statuses make offset pagination skip/duplicate rows even when
        # delivering_time is fixed. Read disjoint time buckets until each fits ONE
        # response. This is a complete scan, not a transactional SPX snapshot.
        base.pop('delivering_time')
        def read(left, right, depth=0):
            body = self.post({**base, 'page_no': 1, 'delivering_time': f'{left},{right}'})
            total, rows = int(body['total']), body['list']
            if total < 0:
                raise RuntimeError('Invalid OH source total')
            if total <= len(rows):
                if total != len(rows):
                    raise RuntimeError('Incomplete OH time bucket')
                return list(rows)
            if left >= right or depth >= 18:
                raise RuntimeError('OH vượt giới hạn một giây; giữ nguyên pivot cũ để tránh thiếu đơn.')
            middle = (left + right) // 2
            return read(left, middle, depth+1) + read(middle+1, right, depth+1)
        step = 7200
        bounds = [(left, min(end, left+step-1)) for left in range(start, end+1, step)]
        with ThreadPoolExecutor(max_workers=min(6, self.settings.workers)) as pool:
            rows = [row for batch in pool.map(lambda b: read(*b), bounds) for row in batch]
        ids = [first(row, 'shipment_id', 'sls_tracking_number').upper() for row in rows]
        if not all(ids) or len(set(ids)) != len(ids):
            raise RuntimeError('OH thay đổi giữa các khung giờ; có đơn trùng hoặc thiếu định danh, giữ pivot cũ.')
        return rows



def aggregate(rows, mapping, day, station, home):
    counts, seen = Counter(), set()
    stats = {'input': len(rows), 'included': 0, 'excluded': 0, 'unmapped': 0}
    for row in rows:
        sid = first(row, 'shipment_id', 'sls_tracking_number').upper()
        if not sid:
            raise RuntimeError('OH thiếu định danh; không thể khử trùng an toàn.')
        if sid in seen:
            stats['excluded'] += 1
            continue
        seen.add(sid)
        status = int(row.get('order_status') or 0)
        delivered = as_datetime(row.get('delivering_time'))
        if status not in (1, 5) or not delivered or delivered.date() != day or not station_matches(row, station, home, day):
            stats['excluded'] += 1
            continue
        zone = first(row, 'zone_id', 'zone_name', 'receiver_zone_id', 'buyer_zone_id')
        district = first(row, 'buyer_addr_district', 'receiver_district_name', 'buyer_district_name', 'delivery_district_name')
        ward = first(row, 'lowest_buyer_address_name', 'receiver_ward_name', 'buyer_ward_name')
        _, info = match_zone(zone, mapping, ward_hint=ward, district_hint=district)
        # Coverage resolves old/new zone aliases to the web's current five areas.
        district = info.get('district') or district
        ward = info.get('old_ward') or info.get('ward') or ward
        area = operating_area(district, ward, info.get('ward', ''), info.get('area', ''))
        area = f'Khu vực {area[-1]}' if area in ('KV1','KV2','KV3','KV4','KV5') else UNKNOWN
        held = as_datetime(row.get('on_hold_time') or row.get('onhold_time'))
        hour = held.hour if held and held.date() == day else -1
        raw_reason = first(row, 'on_hold_reason', 'onhold_reason', 'on_hold_reason_code')
        # Numeric codes only; never persist arbitrary source strings containing PII.
        reason = raw_reason if raw_reason.isascii() and raw_reason.isdigit() and len(raw_reason) <= 6 else 'unknown'
        counts[(area, reason, hour, status)] += 1
        stats['included'] += 1
        stats['unmapped'] += area == UNKNOWN
    pivot = [{'area': a, 'reason': r, 'hour': h, 'status': s, 'orders': n}
             for (a, r, h, s), n in sorted(counts.items())]
    return pivot, stats


def prepare_onhold(settings, day):
    day = date.fromisoformat(day) if isinstance(day, str) else day
    if day > datetime.now(TZ).date():
        raise ValueError('OH không lấy ngày tương lai')
    mapping = load_coverage(settings)
    client = OnholdClient(settings)
    try:
        rows = client.fetch(day)
        pivot, stats = aggregate(rows, mapping, day, settings.station_id, '52-HCM SDD-01 Hub')
        logging.info('OH pivot: input=%s included=%s excluded=%s unmapped=%s cells=%s',
                     stats['input'], stats['included'], stats['excluded'], stats['unmapped'], len(pivot))
        return pivot, stats
    finally:
        client.close()


def prepare_onhold_observations(settings, day):
    """Send only day-scoped digests and pivot dimensions, never source order JSON."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    if day != datetime.now(TZ).date():
        raise ValueError('OH lũy kế chỉ thu thập ngày hiện tại; không tái dựng lịch sử từ trạng thái hiện tại.')
    started = datetime.now(TZ).isoformat()
    mapping = load_coverage(settings)
    client = OnholdClient(settings)
    try:
        rows = client.fetch(day)
        events = []
        for row in rows:
            pivot, stats = aggregate([row], mapping, day, settings.station_id, '52-HCM SDD-01 Hub')
            if not stats['included']:
                continue
            cell = pivot[0]
            zone = first(row, 'zone_id', 'zone_name', 'receiver_zone_id', 'buyer_zone_id')
            district = first(row, 'buyer_addr_district', 'receiver_district_name', 'buyer_district_name', 'delivery_district_name')
            ward = first(row, 'lowest_buyer_address_name', 'receiver_ward_name', 'buyer_ward_name')
            _, info = match_zone(zone, mapping, ward_hint=ward, district_hint=district)
            sid = first(row, 'shipment_id', 'sls_tracking_number').upper()
            tracking = first(row, 'sls_tracking_number', 'shipment_id').upper()
            order_code = (tracking or sid)[:100]
            events.append({
                'order_key': hashlib.sha256(f'oh-v1:{day}:{sid}'.encode()).hexdigest(),
                'order_code': order_code,
                **{k: cell[k] for k in ('area', 'reason', 'hour', 'status')},
                'reason_label': first(row, 'on_hold_reason__desc') or REASONS.get(cell['reason'], f'Unknown ({cell["reason"]})'),
                'district': (info.get('district') or district or 'Chưa rõ quận')[:150],
                'ward': (info.get('old_ward') or info.get('ward') or ward or 'Chưa rõ phường')[:150],
                'rider_code': first(row, 'driver_id', 'rider_id', 'driverId')[:100] or 'unknown',
                'rider_name': first(row, 'driver_name', 'rider_name', 'driverName')[:150] or 'Chưa rõ rider',
            })
        logging.info('OH cumulative scan: input=%s eligible=%s excluded=%s', len(rows), len(events), len(rows)-len(events))
        return events, {'input':len(rows), 'excluded':len(rows)-len(events), 'scan_started_at':started}
    finally:
        client.close()
