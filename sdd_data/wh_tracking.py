"""WH High Value/iPhone tracking from spx_sheet_worker, without Sheet writes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import logging
import time
import requests

from .clients import AuthenticationError, SpxClient, load_coverage
from .config import ROOT, Settings
from .onhold_daily import first, REASONS
from .runner import configure_logging
from .transform import TZ, as_datetime, match_zone, operating_area

TRACKING_INFO = 'https://spx.shopee.vn/api/fleet_order/order/detail/tracking_info'
STATUSES = {'0': 'Created', '1': 'LMhub_received', '2': 'Delivering', '4': 'Delivered', '5': 'Onhold', '7': 'SOC_pendingReceive'}


def order_id(row):
    return first(row, 'shipment_id', 'sls_tracking_number').upper()


def pending_receive_timestamp(events):
    pending, timestamps = list(events), []
    while pending:
        event = pending.pop()
        if not isinstance(event, dict):
            continue
        for key in ('children', 'event_children'):
            if isinstance(event.get(key), list):
                pending.extend(event[key])
        if str(event.get('status')) == '7' or event.get('message') == 'Parcel pending inbound':
            value = as_datetime(event.get('timestamp'))
            if value:
                timestamps.append(value)
    return max(timestamps).isoformat() if timestamps else None


class WhClient(SpxClient):
    def payload(self, api, page=1):
        return {'count': 24, 'high_value': 1, 'page_no': page, 'pickup_station_ids': '5521',
                **({'current_station_ids': self.settings.station_id, 'order_status': '1,2,5,4'}
                   if api == 1 else {'station_ids': self.settings.station_id, 'order_status': '0,7'})}

    def fetch_api(self, api):
        for attempt in range(2):
            rows, seen, total, valid = [], set(), None, True
            for page in range(1, 501):
                data = self.post(self.payload(api, page))
                raw_total = data['total']
                if isinstance(raw_total, bool) or not str(raw_total).isdigit():
                    raise RuntimeError('WH: tổng SPX không hợp lệ; giữ snapshot cũ.')
                if total is None:
                    total = int(raw_total)
                if total != int(raw_total):
                    valid = False
                    break
                batch = data['list']
                ids = [order_id(row) for row in batch]
                allowed = {'1', '2', '5', '4'} if api == 1 else {'0', '7'}
                if any(str(row.get('order_status')) not in allowed for row in batch):
                    raise RuntimeError('WH: API trả sai nhóm trạng thái; giữ snapshot cũ.')
                if not all(ids) or len(ids) != len(set(ids)) or seen.intersection(ids):
                    valid = False
                    break
                seen.update(ids)
                rows.extend(batch)
                if len(rows) >= total or not batch:
                    valid = len(rows) == total
                    break
            else:
                valid = False
            if valid:
                return rows
            logging.warning('WH API%s phân trang không ổn định (%s/%s đơn); chia theo thời gian tạo.', api, len(rows), total)
            if total is not None:
                partitioned = self.fetch_window(api, 0, int(datetime.now(TZ).timestamp()))
                ids = [order_id(row) for row in partitioned]
                allowed = {'1', '2', '5', '4'} if api == 1 else {'0', '7'}
                if len(ids) == total and all(ids) and len(set(ids)) == total and all(str(r.get('order_status')) in allowed for r in partitioned):
                    return partitioned
        raise RuntimeError('WH: không xác minh được đủ trang/mã đơn; giữ snapshot cũ.')

    def fetch_window(self, api, start, end):
        payload = {**self.payload(api), 'count': 100, 'create_time': f'{start},{end}'}
        data = self.post(payload)
        total, rows = int(data['total']), list(data['list'])
        if total < 0 or len(rows) > total:
            raise RuntimeError('WH: tổng khoảng thời gian không hợp lệ.')
        if any(not (created := as_datetime(r.get('create_time'))) or not start <= int(created.timestamp()) <= end for r in rows):
            raise RuntimeError('WH: API không tuân thủ khoảng thời gian tạo.')
        if total > len(rows):
            if start < end:
                middle = (start + end) // 2
                return self.fetch_window(api, start, middle) + self.fetch_window(api, middle + 1, end)
            if total > 10000 or not rows:
                raise RuntimeError('WH: một giây vượt giới hạn phân trang.')
            size = len(rows)
            for page in range(2, (total + size - 1) // size + 1):
                batch = self.post({**payload, 'page_no': page})
                if int(batch['total']) != total:
                    raise RuntimeError('WH: tổng trong một giây thay đổi.')
                rows.extend(batch['list'])
        ids = [order_id(row) for row in rows]
        if len(rows) != total or not all(ids) or len(set(ids)) != total:
            raise RuntimeError('WH: thiếu/trùng đơn trong khoảng thời gian.')
        return rows

    def tracking(self, sid):
        for attempt in range(3):
            try:
                response = self.session().get(TRACKING_INFO, params={'shipment_id': sid}, timeout=30, allow_redirects=False)
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError('WH: không đọc được lịch sử tracking.') from None
                time.sleep(2 ** attempt)
                continue
            if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                raise AuthenticationError('WH: phiên SPX không hợp lệ.')
            if (response.status_code == 429 or response.status_code >= 500) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            if response.status_code != 200:
                raise RuntimeError(f'WH tracking: HTTP {response.status_code}; giữ snapshot cũ.')
            try:
                body = response.json()
            except ValueError:
                raise RuntimeError('WH tracking không trả JSON hợp lệ.') from None
            if not isinstance(body, dict):
                raise RuntimeError('WH tracking sai cấu trúc.')
            if body.get('retcode') in (401, 403, 90309999, '401', '403', '90309999') or body.get('is_login') is False:
                raise AuthenticationError('WH: phiên SPX hết hạn.')
            data = body.get('data')
            if body.get('retcode') not in (0, '0') or not isinstance(data, dict) or not isinstance(data.get('tracking_list'), list):
                raise RuntimeError('WH: thiếu lịch sử tracking; giữ snapshot cũ.')
            if data.get('shipment_id') and str(data['shipment_id']).upper() != sid:
                raise RuntimeError('WH: lịch sử thuộc mã đơn khác.')
            return pending_receive_timestamp(data['tracking_list'])


def build_rows(orders, mapping, day):
    result, seen = [], set()
    for order in orders:
        sid = order_id(order)
        if not sid or sid in seen:
            raise ValueError('WH: mã đơn thiếu hoặc trùng.')
        seen.add(sid)
        raw_status = str(order.get('order_status'))
        if raw_status not in STATUSES:
            raise ValueError('WH: trạng thái không hợp lệ.')
        stamps = {}
        fields = {'created_at': ('create_time',), 'received_at': ('receive_time',), 'delivering_at': ('delivering_time',),
                  'delivered_at': ('delivered_time',), 'onhold_at': ('on_hold_time', 'onhold_time')}
        for name, keys in fields.items():
            raw = next((order.get(k) for k in keys if order.get(k) not in (None, '', 0, '0')), None)
            value = as_datetime(raw)
            if raw is not None and value is None:
                raise ValueError(f'WH: timestamp {name} không hợp lệ.')
            stamps[name] = value.isoformat() if value else None
        # p1_data: delivered timestamp blank or today in Vietnam.
        if stamps['delivered_at'] and as_datetime(order['delivered_time']).date() != day:
            continue
        if raw_status == '4' and not stamps['delivered_at']:
            raise ValueError('WH: Delivered thiếu thời gian giao; giữ snapshot cũ.')
        zone = first(order, 'zone_id', 'zone_name', 'receiver_zone_id', 'buyer_zone_id')
        district = first(order, 'buyer_addr_district', 'receiver_district_name', 'buyer_district_name', 'delivery_district_name', 'district_name')
        ward = first(order, 'lowest_buyer_address_name', 'receiver_ward_name', 'buyer_ward_name', 'delivery_ward_name', 'ward_name')
        _, info = match_zone(zone, mapping, ward_hint=ward, district_hint=district)
        district, ward = info.get('district') or district, info.get('old_ward') or info.get('ward') or ward
        area = operating_area(district, ward, info.get('ward', ''), info.get('area', ''))
        attempts = first(order, 'on_hold_times', 'onhold_times', 'hold_times', 'onHoldTimes', 'onHold_times')
        if attempts and not attempts.isdigit():
            raise ValueError('WH: số lần giao không hợp lệ.')
        reason = first(order, 'on_hold_reason', 'onhold_reason')
        result.append({'shipment_id': sid, 'tracking_number': first(order, 'sls_tracking_number') or sid,
                       'raw_status': STATUSES[raw_status], 'area': area or 'UNRESOLVED', 'district': district, 'ward': ward, 'zone': zone,
                       'driver_id': first(order, 'driver_id', 'rider_id'), 'driver_name': first(order, 'driver_name'),
                       'attempt': int(attempts) if attempts else None,
                       'onhold_reason': f'{reason} - {REASONS[reason]}' if reason in REASONS else reason, **stamps})
    return result


def prepare_wh(settings, root=ROOT):
    day = datetime.now(TZ).date()
    mapping = load_coverage(settings, cache_dir=root / 'data')
    client = WhClient(settings)
    try:
        merged = {}
        for api in (1, 2):
            rows = client.fetch_api(api)
            logging.info('WH API%s: %s đơn.', api, len(rows))
            for row in rows:
                merged.setdefault(order_id(row), row)
        rows = build_rows(list(merged.values()), mapping, day)
        with ThreadPoolExecutor(max_workers=min(8, settings.workers)) as pool:
            stamps = list(pool.map(client.tracking, [r['shipment_id'] for r in rows]))
        for row, stamp in zip(rows, stamps):
            row['soc_pending_at'] = stamp
        if datetime.now(TZ).date() != day:
            raise RuntimeError('WH: đổi ngày khi đang lấy dữ liệu; chạy lại.')
        logging.info('WH: %s đơn, %s chưa rõ KV; không lưu payload raw.', len(rows), sum(r['area'] == 'UNRESOLVED' for r in rows))
        return rows, datetime.now(TZ)
    finally:
        client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true', required=True)
    parser.parse_args()
    configure_logging()
    prepare_wh(Settings.load())
