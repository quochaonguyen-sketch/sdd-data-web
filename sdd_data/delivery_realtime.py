"""SPX delivery progress -> the website's realtime dashboard, one atomic snapshot."""
import argparse
from datetime import datetime
import logging
from pathlib import Path
import time

import requests

from .clients import SpxClient
from .config import ROOT, Settings
from .runner import RunLock, configure_logging
from .source_control import Control
from .transform import TZ

API = 'https://spx.shopee.vn/mgmt/api/pc/forward/data/api_mart/mgmt_app/data_api/operation__lm_delivery_progress_all_fleets__10m_v3'
COUNTERS = {
    'total_assigned': 'lm_hub_fleet_assigned_order_qty_td',
    'delivered': 'lm_hub_fleet_delivered_order_qty_eod',
    'delivering': 'lm_hub_fleet_delivering_in_system_order_qty_eod',
    'failed': 'lm_hub_fleet_failed_delivery_order_qty_eod',
}


def integer(value):
    if isinstance(value, bool):
        raise ValueError('Bộ đếm Delivery không hợp lệ.')
    number = int(str(value))
    if number < 0:
        raise ValueError('Bộ đếm Delivery âm; giữ snapshot cũ.')
    return number


class DeliveryRealtimeClient(SpxClient):
    def fetch_progress(self):
        # SpxSession handles cookie renewal once, using this project's private profile.
        for attempt in range(3):
            try:
                response = self.session().post(API, timeout=(15, 120), allow_redirects=False)
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError('Không kết nối được Delivery realtime sau 3 lần thử; giữ snapshot cũ.') from None
            else:
                if response.status_code != 429 and response.status_code < 500:
                    break
                if attempt == 2:
                    break
                response.close()
            logging.warning('Delivery realtime: lỗi mạng/nguồn tạm thời; thử lại lần %s/3.', attempt + 2)
            time.sleep(2 ** attempt)
        if response.status_code != 200:
            raise RuntimeError(f'Delivery realtime: HTTP {response.status_code}; giữ snapshot cũ.')
        try:
            body = response.json()
        except ValueError:
            raise RuntimeError('Delivery realtime không trả JSON; kiểm tra phiên SPX.') from None
        if not isinstance(body, dict) or body.get('retcode', body.get('error', 0)) not in (None, 0, '0') or body.get('is_login') is False or body.get('redirect_to_error_page'):
            raise RuntimeError('SPX từ chối báo cáo Delivery; giữ snapshot cũ.')
        data = body.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('list'), list):
            raise RuntimeError('SPX thiếu data.list Delivery; giữ snapshot cũ.')
        if 'total' in data and integer(data['total']) != len(data['list']):
            raise RuntimeError('SPX trả báo cáo Delivery thiếu dòng; giữ snapshot cũ.')
        return data['list']


def build_rows(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError('Báo cáo Delivery rỗng; giữ snapshot cũ.')
    rows, seen = [], set()
    negative_idle = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError('Dòng Delivery không hợp lệ.')
        driver = str(item.get('driver_id') or '').strip()
        if not driver:
            raise ValueError('Delivery thiếu driver_id; giữ snapshot cũ.')
        counts = {key: integer(item[field]) for key, field in COUNTERS.items()}
        if driver in seen:
            raise ValueError('Delivery trùng driver_id; giữ snapshot cũ.')
        seen.add(driver)
        if counts['total_assigned'] == 0:
            continue
        first = integer(item.get('lm_hub_fleet_first_delivering_time_eod') or 0)
        idle = int(str(item.get('lm_hub_fleet_idle_delivery_second') or 0))
        # SPX can report a negative elapsed duration; it must not become an idle alert.
        if idle < 0:
            negative_idle += 1
            idle = 0
        rows.append({
            'driver_id': driver, 'driver_name': item.get('driver_name') or None,
            **counts, 'zone_id': str(item.get('max_assigned_zone_id') or '').strip() or None,
            'first_delivery_at': datetime.fromtimestamp(first, TZ).strftime('%H:%M:%S') if first else None,
            'idle_delivery_seconds': idle,
        })
    if not rows:
        raise ValueError('Delivery không có rider được giao đơn; giữ snapshot cũ.')
    if negative_idle:
        logging.warning('Delivery realtime: chuẩn hóa thời gian đứng yên âm về 0 cho %s rider.', negative_idle)
    return rows


def prepare_delivery_realtime(settings):
    client = DeliveryRealtimeClient(settings)
    started = datetime.now(TZ)
    try:
        raw = client.fetch_progress()
        rows = build_rows(raw)
        timestamps = {integer(r.get('update_at_timestamp') or 0) for r in raw}
        if len(timestamps) != 1 or 0 in timestamps:
            raise RuntimeError('SPX thiếu hoặc trộn thời điểm báo cáo Delivery; giữ snapshot cũ.')
        source_at = datetime.fromtimestamp(timestamps.pop(), TZ)
    finally:
        client.close()
    stamp = datetime.now(TZ)
    if stamp.date() != started.date():
        raise RuntimeError('Delivery đi qua nửa đêm; chạy lại để đúng ngày dữ liệu.')
    if source_at.date() != stamp.date() or not -60 <= (stamp-source_at).total_seconds() <= 1800:
        raise RuntimeError('Báo cáo nguồn Delivery quá cũ hoặc sai ngày; giữ snapshot cũ.')
    logging.info('Delivery realtime: %s rider, %s đã giao, %s đang giao, %s lỗi.',
                 len(rows), sum(r['delivered'] for r in rows),
                 sum(r['delivering'] for r in rows), sum(r['failed'] for r in rows))
    return rows, source_at


def sync_once(settings, *, root=ROOT, dry_run=False):
    """Standalone one-shot entry point; central worker also uses the same lock."""
    with RunLock(Path(root) / 'data' / 'delivery-realtime-worker.lock'):
        rows, stamp = prepare_delivery_realtime(settings)
        if dry_run:
            return len(rows)
        control = Control(settings)
        try:
            job = control.rpc('claim_delivery_realtime_once', p_day=stamp.date().isoformat())
            if not job:
                raise RuntimeError('Delivery realtime đang có tác vụ; đợi lượt chạy hiện tại.')
            try:
                if not control.rpc('begin_data_source_publish', p_job=job['id'], p_worker=job['worker_id']):
                    raise RuntimeError('Không còn quyền ghi Delivery realtime.')
                count = control.rpc('publish_central_delivery_realtime', p_job=job['id'],
                                    p_worker=job['worker_id'], p_rows=rows, p_at=stamp.isoformat())
            except BaseException:
                control.rpc('finish_data_source_job', p_job=job['id'], p_worker=job['worker_id'], p_status='FAILED')
                raise
            control.rpc('finish_data_source_job', p_job=job['id'], p_worker=job['worker_id'], p_status='SUCCEEDED')
            return count
        finally:
            control.close()


def main():
    parser = argparse.ArgumentParser(description='Lấy dữ liệu dashboard Delivery realtime một lần.')
    parser.add_argument('--dry-run', action='store_true', help='Chỉ đọc SPX, không ghi Supabase.')
    args = parser.parse_args()
    configure_logging()
    try:
        sync_once(Settings.load(), dry_run=args.dry_run)
    except Exception as error:
        logging.error('%s', error)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
