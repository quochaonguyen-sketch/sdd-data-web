"""Complete SPX return inventory, resolved by the seller's pickup point, KV1..KV5."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
from pathlib import Path
import re
import time

from .clients import SpxClient, WebsiteStore, load_coverage, load_seller_wards
from .config import ROOT
from .pickup_inventory import PickupInventoryClient, parse_pickup_ward
from .pickup_routes import pickup_point
from .transform import TZ, as_datetime, confirmed_pickup_location, location_key, match_zone, operating_area

log = logging.getLogger(__name__)
STATUSES = {67: 'Return FMHub received', 10: 'Return LMHub received', 72: 'Return FMHub returning'}
UNKNOWN_AREA = 'Chưa rõ KV'


class ReturnClient(SpxClient):
    def fetch(self):
        """Read pages concurrently and reject a changed or incomplete source."""
        for attempt in range(2):
            try:
                return self.fetch_once()
            except SourceChanged:
                if attempt:
                    raise
                log.info('Hàng trả đổi khi phân trang; kiểm lại toàn bộ một lần trước khi ghi.')

    def fetch_once(self):
        base = {'count': 200, 'current_station_ids': self.settings.station_id,
                'order_status': '67,10,72', 'page_no': 1}
        first = self.post(base)
        expected = int(first['total'])
        finish = int(datetime.now(TZ).timestamp())
        deadline = time.monotonic() + 1200
        def read(left, right, data=None):
            if time.monotonic() > deadline:
                raise RuntimeError('Hàng trả quá 20 phút; giữ snapshot cũ.')
            data = data if data is not None else self.post({**base, 'create_time': f'{left},{right}'})
            total, rows = int(data['total']), data['list']
            if total < 0:
                raise RuntimeError('Negative return total')
            if total <= len(rows):
                if len(rows) != total:
                    raise RuntimeError('Return page count mismatch')
                return list(rows)
            if left >= right:
                raise RuntimeError('Quá nhiều đơn hàng trả trong một giây; chưa thể xác nhận đủ nguồn.')
            middle = (left + right) // 2
            return read(left, middle) + read(middle + 1, right)
        if expected <= len(first['list']):
            rows = read(0, finish, first)
        elif expected < 10000:
            page_size = len(first['list'])
            if not page_size:
                raise SourceChanged('Trang đầu hàng trả rỗng trong khi tổng khác 0.')
            def page(number):
                data = self.post({**base, 'page_no': number})
                if int(data['total']) != expected:
                    raise SourceChanged('Tổng hàng trả đổi trong lúc đọc.')
                return data['list']
            rows = list(first['list'])
            with ThreadPoolExecutor(max_workers=min(12, self.settings.workers)) as pool:
                for batch in pool.map(page, range(2, (expected + page_size - 1) // page_size + 1)):
                    rows.extend(batch)
        else:
            # Coarse disjoint windows run concurrently, each recursively checks its own totals.
            year = 365 * 86400
            bounds = [(max(0, finish - year), finish), (0, max(0, finish - year) - 1)]
            with ThreadPoolExecutor(max_workers=2) as pool:
                rows = [row for page in pool.map(lambda b: read(*b), [b for b in bounds if b[0] <= b[1]]) for row in page]
        ids = [str(row.get('shipment_id') or '').strip() for row in rows]
        if len(rows) != expected or not all(ids) or len(set(ids)) != expected:
            raise SourceChanged(f'Hàng trả đổi hoặc thiếu/trùng trong lúc đọc ({len(set(ids))}/{expected}); giữ snapshot cũ, chạy lại.')
        if any(int(row.get('order_status') or 0) not in STATUSES or str(row.get('current_station_id')) != self.settings.station_id for row in rows):
            raise RuntimeError('SPX trả đơn ngoài trạng thái hoặc hub hàng trả yêu cầu.')
        return rows


class SourceChanged(RuntimeError):
    pass


class ReturnStore(WebsiteStore):
    tables = ('riders',)

    def riders(self):
        result, offset = [], 0
        while True:
            page = self.request('GET', 'riders', {'select': 'rider_code,full_name,name,point_name,status,cot',
                'status': 'eq.active', 'order': 'id.asc', 'offset': str(offset), 'limit': '500'})
            result.extend(page)
            if len(page) < 500:
                return result
            offset += 500


def return_points(settings, rows, client, root):
    cache_path = Path(root) / 'data' / f'return-points-{settings.station_id}.json'
    cache = {}
    if cache_path.is_file():
        body = json.loads(cache_path.read_text(encoding='utf-8'))
        if body.get('station_id') == settings.station_id:
            cache = {k: str(v) for k, v in body.get('points', {}).items() if v}
    ids = {str(row['shipment_id']) for row in rows}
    # Existing positive pickup-point caches share the same station and identity contract.
    days = {dt.date().isoformat() for row in rows if (dt := as_datetime(row.get('create_time')))}
    for day in days:
        candidate = Path(root) / 'data' / f'pickup-points-{settings.station_id}-{day}.json'
        if candidate.is_file():
            body = json.loads(candidate.read_text(encoding='utf-8'))
            if body.get('station_id') == settings.station_id and body.get('date') == day:
                cache.update({k: str(v) for k, v in body.get('points', {}).items() if k in ids and v})
    missing = sorted(ids - cache.keys())
    def resolve(sid):
        body = client.get('order/detail/pickup_info', {'shipment_id': sid, 'station_type': 12})
        if str(body.get('shipment_id') or '') != sid:
            raise RuntimeError('Pickup point thuộc vận đơn khác; không ghi dữ liệu hàng trả.')
        return sid, pickup_point(body)
    def save():
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        pending = cache_path.with_suffix('.pending')
        pending.write_text(json.dumps({'station_id': settings.station_id, 'points': cache}), encoding='utf-8')
        pending.replace(cache_path)
    try:
        with ThreadPoolExecutor(max_workers=min(12, settings.workers)) as pool:
            for done, (sid, point) in enumerate(pool.map(resolve, missing), 1):
                if point:
                    cache[sid] = point
                if done % 100 == 0:
                    save()
                    log.info('Hàng trả: đối chiếu pickup point %s/%s.', done, len(missing))
    finally:
        save()
    return {sid: cache.get(sid, '') for sid in ids}


def resolve_seller(order, detail, route, mapping, seller_wards):
    district = str(order.get('seller_district_name') or order.get('seller_addr_district') or '').strip()
    ward = seller_wards.get(str(order.get('lowest_seller_address_id') or '')) or str(order.get('lowest_seller_address_name') or '').strip()
    ward = ward or parse_pickup_ward(detail.get('district', ''), str(order.get('seller_address') or ''), detail.get('point_address', ''))
    confirmed = confirmed_pickup_location(route, ward, mapping) if route else None
    if confirmed:
        district = confirmed['district']
    candidates = []
    for entry in mapping.values():
        for info in entry.get('zone_candidates', [entry]):
            if ward and location_key(ward) in {location_key(info.get('ward')), location_key(info.get('old_ward'))}:
                if not district or location_key(info.get('district')) == location_key(district):
                    candidates.append(info)
    identities = {(item.get('area'), item.get('district'), item.get('ward')) for item in candidates}
    if len(identities) == 1:
        area, district, new_ward = next(iter(identities))
        old_wards = {item.get('old_ward') for item in candidates if item.get('old_ward')}
        return {'area': area, 'district': district, 'ward': next(iter(old_wards)) if len(old_wards) == 1 else ward, 'new_ward': new_ward}
    # Only the seller route is eligible. The buyer zone_id is retained as reference, never used to locate returns.
    _, exact = match_zone(route, mapping, kind='pickup', ward_hint=ward, district_hint=district) if route else ('', {})
    if exact:
        return {'area': exact.get('area'), 'district': exact.get('district'), 'ward': exact.get('old_ward') or ward, 'new_ward': exact.get('ward')}
    area = confirmed.get('area') if confirmed else operating_area(district, '', ward, None)
    return {'area': area, 'district': district, 'ward': ward, 'new_ward': ward}


def build_returns(rows, mapping, seller_wards, points, routes, details, riders, station_id):
    by_route = {}
    for rider in riders:
        if rider.get('status') != 'active':
            continue
        for route in re.split(r'[,;\n]+', str(rider.get('point_name') or '')):
            if route.strip():
                by_route.setdefault(route.strip().casefold(), {})[str(rider['rider_code'])] = rider
    result, stats, seen = [], Counter(input=len(rows)), set()
    for row in rows:
        sid, status = str(row.get('shipment_id') or '').strip(), int(row.get('order_status') or 0)
        if not sid or sid in seen or status not in STATUSES or str(row.get('current_station_id')) != station_id:
            raise RuntimeError('Invalid/duplicate return source row')
        seen.add(sid)
        point = points.get(sid, '')
        route = routes.get(point, '')
        info = resolve_seller(row, details.get(point, {}), route, mapping, seller_wards)
        area = info.get('area')
        label = f'Khu vực {area[-1]}' if area in {'KV1','KV2','KV3','KV4','KV5'} else UNKNOWN_AREA
        stats[label] += 1
        matched = list(by_route.get(route.casefold(), {}).values())
        labels = lambda values: '; '.join(f"{r['rider_code']} · {r.get('full_name') or r.get('name') or ''}".strip(' ·') for r in values)
        cot = lambda r: re.sub(r'[^A-Z0-9]', '', str(r.get('cot') or '').upper())
        record = {'shipment_id': sid, 'order_status': status, 'status_label': STATUSES[status],
            'sls_tracking_number': str(row.get('sls_tracking_number') or ''), 'shopee_order_sn': str(row.get('shopee_order_sn') or ''),
            'lowest_seller_address_id': str(row.get('lowest_seller_address_id') or ''),
            'seller_district': info.get('district') or '', 'seller_ward': info.get('ward') or '', 'seller_new_ward': info.get('new_ward') or '',
            'seller_area': label, 'seller_zone_id': route, 'order_zone_id': str(row.get('zone_id') or ''),
            'pickup_point_id': point, 'return_zone': route,
            'return_rider_codes': '; '.join(str(r['rider_code']) for r in matched), 'return_rider_names': '; '.join(str(r.get('full_name') or r.get('name') or '') for r in matched),
            'return_riders_cot1': labels([r for r in matched if cot(r) == 'COT1']), 'return_riders_cot2': labels([r for r in matched if cot(r) == 'COT2']),
            'return_driver_id': str(row.get('driver_id') or '').strip() if status == 72 and str(row.get('driver_id') or '') != '0' else '',
            'return_driver_name': str(row.get('driver_name') or '').strip() if status == 72 else ''}
        for key in ('current_station_id', 'pickup_station_id'):
            record[key] = int(row.get(key) or 0)
        for key in ('current_station_name', 'pickup_station_name'):
            record[key] = str(row.get(key) or '')
        for key in ('create_time', 'receive_time', 'delivering_time', 'current_station_received_time'):
            dt = as_datetime(row.get(key))
            record[key] = dt.isoformat() if dt else None
        result.append(record)
    return result, {**stats, 'included': len(result), 'source_complete': True}


def prepare_returns(settings, root=ROOT):
    client, points_client, store = ReturnClient(settings), PickupInventoryClient(settings), ReturnStore(settings)
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            orders_future = pool.submit(client.fetch)
            mapping_future = pool.submit(load_coverage, settings)
            wards_future = pool.submit(load_seller_wards, settings)
            riders_future = pool.submit(store.riders)
            rows = orders_future.result()
            log.info('Hàng trả: đọc đủ %s đơn tại hub %s.', len(rows), settings.station_id)
            with ThreadPoolExecutor(max_workers=2) as lookup_pool:
                routes_future = lookup_pool.submit(points_client.routes)
                points = return_points(settings, rows, points_client, root)
                details = points_client.fetch_point_details(set(points.values()) - {''}, Path(root) / 'data')
                routes, _ = routes_future.result()
            result, stats = build_returns(rows, mapping_future.result(), wards_future.result(), points, routes, details, riders_future.result(), settings.station_id)
        artifact = Path(root) / 'data' / f'return-inventory-{datetime.now(TZ):%Y%m%d-%H%M%S-%f}.json'
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({'stats': stats, 'records': result}, ensure_ascii=False), encoding='utf-8')
        log.info('Hàng trả: %s.', stats)
        return result, stats
    finally:
        client.close()
        points_client.close()
        store.close()
