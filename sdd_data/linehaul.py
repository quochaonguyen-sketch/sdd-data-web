"""Today's LH trips and latest vehicle fix. SPX is read-only; no dispatch mutations."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
import logging
import math
import re
import time as clock
import threading

import requests

from .clients import SpxClient
from .config import ROOT, Settings
from .pickup_cache import read_cache, write_cache, fingerprint
from .runner import configure_logging
from .transform import TZ

BASE = 'https://spx.shopee.vn/api/admin/transportation/trip/'
DASHBOARD = 'https://spx.shopee.vn/mgmt/api/pc/dashboard/lm/linehaul_trips'
log = logging.getLogger(__name__)


def stamp(value):
    return datetime.fromtimestamp(int(value), TZ).isoformat() if value else None


def count(value):
    if value is None:
        return None
    if value in (-1, '-1'):
        return None  # SPX's sentinel for a trip whose load has not been reported.
    if isinstance(value, bool) or int(value) < 0 or str(int(value)) != str(value):
        raise ValueError('Invalid LH quantity')
    return int(value)


def packs(description):
    if not description or not description.strip():
        return None, None, ''
    bags, loose, unknown, known = 0, 0, [], False
    for part in description.split(','):
        match = re.fullmatch(r'\s*(\d+)\s*(.+?)\s*', part)
        if match and match[2].casefold().startswith('bao'):
            bags += int(match[1])
            known = True
        elif match and match[2].upper() == 'NA':
            loose += int(match[1])
            known = True
        else:
            unknown.append(part.strip())
    return (bags if known else None), (loose if known else None), ', '.join(unknown)


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def gps_point(data):
    points = []
    for point in data.get('location_list') or []:
        try:
            lat, lng = float(point['lat']), float(point['lng'])
            uploaded = int(point['upload_time'])
            if -90 <= lat <= 90 and -180 <= lng <= 180 and (lat or lng) and 0 < uploaded <= clock.time()+60:
                points.append((uploaded, lat, lng))
        except (KeyError, TypeError, ValueError):
            continue
    if not points:
        return {'lat': None, 'lng': None, 'gps_at': None}
    uploaded, lat, lng = max(points)
    return {'lat': lat, 'lng': lng, 'gps_at': stamp(uploaded)}


def check_arrived_location(data, ata_ts, hub_lat, hub_lng):
    if not ata_ts or hub_lat is None or hub_lng is None:
        return {'arrived_lat': None, 'arrived_lng': None, 'arrived_at_hub': None, 'arrived_distance_km': None}
    points = []
    for point in data.get('location_list') or []:
        try:
            lat, lng = float(point['lat']), float(point['lng'])
            uploaded = int(point['upload_time'])
            if -90 <= lat <= 90 and -180 <= lng <= 180 and (lat or lng) and 0 < uploaded <= clock.time()+60:
                points.append((uploaded, lat, lng))
        except (KeyError, TypeError, ValueError):
            continue
    if not points:
        return {'arrived_lat': None, 'arrived_lng': None, 'arrived_at_hub': None, 'arrived_distance_km': None}
    candidates = [p for p in points if abs(p[0] - ata_ts) <= 1800]
    pool = candidates if candidates else points
    closest = min(pool, key=lambda p: abs(p[0] - ata_ts))
    if abs(closest[0] - ata_ts) > 7200:
        return {'arrived_lat': None, 'arrived_lng': None, 'arrived_at_hub': None, 'arrived_distance_km': None}
    dist = round(haversine_km(closest[1], closest[2], hub_lat, hub_lng), 2)
    return {
        'arrived_lat': closest[1],
        'arrived_lng': closest[2],
        'arrived_distance_km': dist,
        'arrived_at_hub': dist <= 1.0
    }


def dashboard_packs(description):
    if not description or not description.strip():
        return None, None, ''
    bags, loose, unknown, known = 0, 0, [], False
    for part in description.split(','):
        match = re.fullmatch(r'\s*(.+?)\s+(\d+)\s*', part)
        if match and match[1].casefold().startswith('bao'):
            bags += int(match[2]); known = True
        elif match and match[1].casefold() in ('other', 'na'):
            loose += int(match[2]); known = True
        else:
            unknown.append(part.strip())
    return (bags if known else None), (loose if known else None), ', '.join(unknown)


def build_dashboard_trip(raw, station, day):
    driver = str(raw.get('driver_name') or '').strip()
    code = str(raw.get('trip_number') or '').strip()
    if not re.fullmatch(r'LT[A-Za-z0-9]+', code):
        raise ValueError('Dashboard LH thiếu mã LT hợp lệ.')
    if str(raw.get('station_id')) != str(station) or str(raw.get('arrived_station_id')) != str(station):
        return None
    if not raw.get('driver_id') or str(raw.get('driver_id')) == '0' or driver in ('', '-', '--'):
        return None
    if not raw.get('trip_sta') or datetime.fromtimestamp(int(raw['trip_sta']),TZ).date()!=day:
        return None
    # Dashboard ATA is arrival; it is not an unloading completion timestamp.
    if str(raw.get('trip_status')) != '3' or not raw.get('trip_ata') or datetime.fromtimestamp(int(raw['trip_ata']),TZ).date()!=day:
        return None
    description = str(raw.get('to_pack_type_name_list') or '')
    bags, loose, unknown = dashboard_packs(description)
    return {'trip_id':code,'lt_code':code,'trip_day':day.isoformat(),'source':'dashboard',
            'vehicle':str(raw.get('vehicle_group_type') or ''),'plate_number':str(raw.get('vehicle_number') or ''),
            'driver_name':driver,'origin':str(raw.get('departed_station_name') or ''),
            'destination':str(raw.get('arrived_station_name') or ''),'direction':'inbound','phase':'at_hub','hub_sequence':0,
            'hub_eta':stamp(raw.get('trip_eta')),'hub_sta':stamp(raw.get('trip_sta')),'hub_ata':stamp(raw.get('trip_ata')),
            'hub_atd':None,'hub_unseal_at':None,'hub_unloaded_at':None,'origin_atd':stamp(raw.get('trip_atd')),
            'orders':count(raw.get('trip_order_qty_td')),'load_total':count(raw.get('trip_to_qty_td')),
            'bags':bags,'loose':loose,'pack_description':description,'pack_unknown':unknown,
            'expected_unload':None,'unloaded_units':None,
            'arrived_lat':None,'arrived_lng':None,'arrived_at_hub':None,'arrived_distance_km':None}


def build_trip(raw, station, day):
    driver_name = str(raw.get('driver_name') or '').strip()
    if not raw.get('driver') or str(raw.get('driver')) == '0' or driver_name in ('', '-', '--'):
        return None
    stations = sorted(raw.get('trip_station') or [], key=lambda s: int(s.get('sequence_number') or 0))
    # Today's workload is the STA at our hub, including trips that started yesterday.
    matches = [s for s in stations if str(s.get('station')) == str(station) and s.get('sta')
               and datetime.fromtimestamp(int(s['sta']), TZ).date() == day]
    if not matches:
        return None
    hub = next((s for s in matches if s.get('sequence_number') == raw.get('display_station_sequence')), matches[0])
    index = stations.index(hub)
    unloaded_today = bool(hub.get('unloaded_time') and datetime.fromtimestamp(int(hub['unloaded_time']), TZ).date() == day)
    direction = 'outbound' if index == 0 else 'inbound' if index == len(stations)-1 else 'stopover'
    has_unsealed = bool(hub.get('unseal_time') and int(hub.get('unseal_time') or 0) > 0)
    has_started_unload = bool((hub.get('unload_quantity') and int(hub.get('unload_quantity') or 0) > 0) or int(hub.get('trip_station_status') or 0) == 60)
    phase = ('unloaded' if unloaded_today else 'departed' if hub.get('atd')
             else 'unloading' if hub.get('ata') and (has_unsealed or has_started_unload)
             else 'at_hub' if hub.get('ata') else 'en_route' if index and stations[index-1].get('atd') else 'waiting')
    bags, loose, unknown = packs(str(raw.get('pack_type') or ''))
    return {
        'trip_id': str(raw['id']), 'lt_code': str(raw.get('trip_number') or raw['id']),
        'trip_day': day.isoformat(), 'vehicle': str(raw.get('vehicle_type_name') or ''),
        'source': 'active',
        'plate_number': str(raw.get('vehicle_number') or ', '.join(raw.get('vehicle_plate_number_list') or [])),
        'driver_name': driver_name,
        'origin': str(stations[0].get('station_name') or ''), 'destination': str(stations[-1].get('station_name') or ''),
        'direction': direction, 'phase': phase, 'hub_sequence': hub['sequence_number'],
        'hub_eta': stamp(hub.get('eta')), 'hub_sta': stamp(hub.get('sta')),
        'hub_ata': stamp(hub.get('ata')), 'hub_atd': stamp(hub.get('atd')),
        'hub_unseal_at': stamp(hub.get('unseal_time')),
        'hub_unloaded_at': stamp(hub.get('unloaded_time')),
        'orders': count(raw.get('order_loaded_quantity')), 'bags': bags, 'loose': loose,
        'load_total': count(raw.get('to_loaded_quantity')), 'pack_description': str(raw.get('pack_type') or ''),
        'pack_unknown': unknown, 'expected_unload': count(hub.get('expect_unload_quantity')),
        'unloaded_units': count(hub.get('unload_quantity')),
        'arrived_lat': None, 'arrived_lng': None, 'arrived_at_hub': None, 'arrived_distance_km': None
    }


class LinehaulClient(SpxClient):
    def __init__(self, settings):
        super().__init__(settings)
        self._pace = threading.Lock()
        self._next_request = 0

    def pace(self, delay=0):
        with self._pace:
            self._next_request = max(self._next_request, clock.monotonic()+delay)
            clock.sleep(max(0, self._next_request-clock.monotonic()))
            self._next_request = clock.monotonic()+0.6

    def get(self, path, params, *, dashboard=False):
        for attempt in range(5):
            self.pace()
            try:
                response = (self.session().post(DASHBOARD,json={},timeout=(15,60),allow_redirects=False) if dashboard
                            else self.session().get(BASE + path, params=params, timeout=(15, 60), allow_redirects=False))
            except requests.RequestException:
                if attempt == 4:
                    raise RuntimeError('Không kết nối được SPX LH sau 5 lần thử.') from None
            else:
                if response.status_code == 200:
                    try:
                        body = response.json()
                    except ValueError:
                        raise RuntimeError('LH không trả JSON; kiểm tra đăng nhập SPX.') from None
                    if isinstance(body,dict) and str(body.get('retcode')) == '130008001' and attempt<4:
                        self.pace(2 ** (attempt+1))
                        continue
                    if not isinstance(body, dict) or body.get('retcode') not in (0, '0') or not isinstance(body.get('data'), dict):
                        raise RuntimeError('SPX từ chối hoặc trả dữ liệu LH không hợp lệ.')
                    return body['data']
                if response.status_code != 429 and response.status_code < 500 or attempt == 4:
                    raise RuntimeError(f'SPX LH trả HTTP {response.status_code}.')
                response.close()
            clock.sleep(2 ** attempt)

    def dashboard_trips(self):
        data = self.get('', {}, dashboard=True)
        rows = data.get('list')
        if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
            raise RuntimeError('Dashboard LH thiếu danh sách; giữ snapshot cũ.')
        codes=[str(row.get('trip_number') or '').strip() for row in rows]
        if not all(codes) or len(set(codes))!=len(codes):
            raise RuntimeError('Dashboard LH thiếu/trùng mã LT; giữ snapshot cũ.')
        if 'total' in data and count(data['total'])!=len(rows):
            raise RuntimeError('Dashboard LH thiếu chuyến; giữ snapshot cũ.')
        return rows

    def trips(self, day):
        start = datetime.combine(day, time.min, TZ)
        params = {'station_type': '2,3,7,12,14,16,18', 'trip_station_status': '50,200,80,60',
                  'pageno': 1, 'count': 100, 'query_type': 1, 'tab_type': 2,
                  'sta': f'{int(start.timestamp())},{int((start+timedelta(days=1)).timestamp())-1}'}
        endpoint = 'list_v2'
        for attempt in range(2):
            first = self.get(endpoint, params)
            total = count(first.get('total'))
            rows = first.get('list')
            if total is None or not isinstance(rows, list) or (total and not rows):
                raise RuntimeError('LH thiếu tổng hoặc danh sách chuyến.')
            size = len(rows) or 100
            if total > 10000:
                raise RuntimeError('LH vượt giới hạn kiểm tra; không ghi snapshot thiếu.')
            with ThreadPoolExecutor(max_workers=4) as pool:
                pages = list(pool.map(lambda page: self.get(endpoint, {**params, 'pageno': page}),
                                      range(2, (total+size-1)//size+1)))
            rows = rows + [r for page in pages for r in page.get('list', [])]
            ids = [str(row.get('id') or '') for row in rows]
            if all(page.get('total') == total for page in pages) and len(rows) == total and len(set(ids)) == total and all(ids):
                return rows
            if attempt:
                raise RuntimeError('Danh sách LH thay đổi khi phân trang; giữ snapshot cũ.')
            log.info('Danh sách LH đang đổi; đọc lại đầy đủ một lần.')


def prepare_linehaul(settings, root=ROOT):
    day = datetime.now(TZ).date()
    client = LinehaulClient(settings)
    path = root / 'data' / f'linehaul-locations-{settings.station_id}.json'
    cache_body = read_cache(path)
    cached = cache_body.get('trips', {}) if cache_body.get('day') == day.isoformat() else {}
    known = cache_body.get('known_trips', {}) if cache_body.get('day') == day.isoformat() else {}
    saved = {}
    try:
        active = client.trips(day)
        active_codes = {str(row.get('trip_number') or row.get('id') or '').strip().upper() for row in active}
        completed = client.dashboard_trips()
        by_id = {trip['lt_code'].upper():(row,trip) for row in active if (trip:=build_trip(row,settings.station_id,day)) is not None}
        known.update({code: trip['trip_id'] for code, (_, trip) in by_id.items()})
        for row in completed:
            trip = build_dashboard_trip(row,settings.station_id,day)
            if trip is not None:
                previous = by_id.get(trip['lt_code'].upper())
                if previous is None and (known_id := known.get(trip['lt_code'].upper())):
                    try:
                        detail = client.get('detail', {'trip_id': known_id})
                        verified = build_trip(detail, settings.station_id, day)
                        if verified and verified['lt_code'].upper() == trip['lt_code'].upper():
                            previous = (detail, verified)
                    except RuntimeError:
                        log.warning('LH detail unavailable; using the complete first-API comparison for workflow status.')
                if previous:
                    trip['trip_id'] = previous[1]['trip_id']
                    trip['source'] = 'active'
                    for key in ('orders','bags','loose','load_total','pack_description','pack_unknown'):
                        if trip[key] is None or trip[key] == '':
                            trip[key] = previous[1][key]
                    # Dashboard has order totals; the active source has hub unloading scans.
                    for key in ('expected_unload', 'unloaded_units', 'hub_unseal_at', 'hub_unloaded_at', 'hub_atd', 'hub_sequence', 'direction',
                                'arrived_lat', 'arrived_lng', 'arrived_at_hub', 'arrived_distance_km'):
                        if previous[1].get(key) is not None:
                            trip[key] = previous[1][key]
                    if trip['hub_unloaded_at']:
                        trip['phase'] = 'unloaded'
                    else:
                        trip['phase'] = previous[1]['phase']
                # Only absence from the COMPLETE first API confirms completion by the operations rule.
                if trip['lt_code'].upper() not in active_codes:
                    trip['phase'] = 'unloaded'
                    trip['unload_confirmation'] = 'active_list_absence'
                by_id[trip['lt_code'].upper()] = (row,trip)
        trips = list(by_id.values())
        def locate(pair):
            row, trip = pair
            signature = fingerprint([row.get('vehicle_number'), row.get('driver'), row.get('trip_status'), row.get('current_sequence_number')])
            previous = cached.get(trip['trip_id'], {})
            if previous.get('signature') != signature:
                previous = {}
            if trip['source']=='dashboard' or trip['phase']=='unloaded':
                return {**trip,'lat':None,'lng':None,'gps_at':None,'hub_lat':None,'hub_lng':None,
                        'next_station':None,'location_error':None,'location_checked_at':None,
                        'arrived_lat':trip.get('arrived_lat'),'arrived_lng':trip.get('arrived_lng'),
                        'arrived_at_hub':trip.get('arrived_at_hub'),'arrived_distance_km':trip.get('arrived_distance_km')}
            now = clock.time()
            ttl = 600 if trip['phase'] in ('departed', 'at_hub', 'unloading') or (trip['phase']=='waiting' and not previous.get('location',{}).get('gps_at') and not previous.get('location',{}).get('location_error')) else 60
            if previous.get('signature') == signature and 0 <= now-previous.get('at', 0) < ttl:
                location = previous['location']
            else:
                try:
                    data = client.get('location', {'trip_id': trip['trip_id']})
                    hub_lat_val, hub_lng_val = None, None
                    for station in data.get('trip_station') or []:
                        if str(station.get('station_id')) == str(settings.station_id) and station.get('sequence_number') == trip['hub_sequence']:
                            lat, lng = station.get('lat'), station.get('lng')
                            if isinstance(lat,(int,float)) and isinstance(lng,(int,float)) and -90<=lat<=90 and -180<=lng<=180 and (lat or lng):
                                hub_lat_val, hub_lng_val = lat, lng
                    location = {**gps_point(data), 'hub_lat': hub_lat_val, 'hub_lng': hub_lng_val,
                                'location_error': None, 'location_checked_at': datetime.now(TZ).isoformat(), 'next_station': None}
                    for station in data.get('trip_station') or []:
                        if station.get('sequence_number') == data.get('next_sequence_number'):
                            location['next_station'] = station.get('station_name')
                    if data.get('next_sequence_number') == trip['hub_sequence'] and data.get('eta'):
                        location['hub_eta'] = stamp(data['eta'])
                    ata_ts = int(datetime.fromisoformat(trip['hub_ata']).timestamp()) if trip.get('hub_ata') else None
                    arrived_check = check_arrived_location(data, ata_ts, hub_lat_val, hub_lng_val)
                    location.update(arrived_check)
                except (RuntimeError, ValueError, TypeError, KeyError):
                    location = {**previous.get('location', {'lat':None,'lng':None,'gps_at':None,'hub_lat':None,'hub_lng':None,'next_station':None,'arrived_lat':None,'arrived_lng':None,'arrived_at_hub':None,'arrived_distance_km':None}),
                                'location_error':'Chưa cập nhật được GPS; vị trí cũ nếu có.', 'location_checked_at':datetime.now(TZ).isoformat()}
            saved[trip['trip_id']] = {'at':now, 'signature':signature, 'location':location}
            if len(saved) % 20 == 0:
                log.info('LH: đã kiểm tra vị trí %s/%s chuyến.', len(saved), len(trips))
            return {**trip, **location}
        with ThreadPoolExecutor(max_workers=6) as pool:
            result = list(pool.map(locate, trips))
        if datetime.now(TZ).date() != day:
            raise RuntimeError('LH đi qua nửa đêm; chạy lại để đúng ngày.')
        write_cache(path, {'day':day.isoformat(), 'trips':saved, 'known_trips':known})
        log.info('LH hôm nay: %s chuyến tại hub, %s có GPS, %s lỗi vị trí.', len(result),sum(r.get('lat') is not None for r in result),sum(bool(r.get('location_error')) for r in result))
        return result, datetime.now(TZ)
    finally:
        client.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--dry-run', action='store_true');args=parser.parse_args()
    if not args.dry_run:
        parser.error('Dùng worker trung tâm để ghi; --dry-run chỉ đọc nguồn.')
    configure_logging();prepare_linehaul(Settings.load())
