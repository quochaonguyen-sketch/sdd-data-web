"""Full-hub PUP directory. Only reads SPX; never imports the assignment engine."""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import json
import logging
import math
from pathlib import Path
import time

from .config import ROOT, Settings
from .pickup_routes import PickupRouteClient
from .pickup_inventory import parse_pickup_ward
from .runner import RunLock, configure_logging
from .source_control import Control
from .transform import TZ, as_datetime


def fetch_shop_orders(settings, day):
    """Read complete historical windows at the verified 1,000-row SPX limit."""
    from .clients import SpxClient
    client=SpxClient(settings)
    start=int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp())
    def window(bounds):
        left,right=bounds
        data=client.post({**client.payload('pickup',left,right),'count':1000})
        total=int(data['total']);rows=data['list']
        if total>len(rows):
            if left==right:
                # Strict existing pagination verifies an unusually busy second.
                return client.fetch_window('pickup',left,right)
            parts=min(right-left+1,max(2,math.ceil(total/900)))
            step=max(1,(right-left+1)//parts)
            result=[]
            for begin in range(left,right+1,step):result.extend(window((begin,min(begin+step-1,right))))
            return result
        if total<0 or len(rows)!=total:raise RuntimeError('Nguồn Volume shop thiếu dòng.')
        compact=[]
        for row in rows:
            stamp=as_datetime(row.get('create_time'))
            if stamp is None or stamp.date().isoformat()!=day:
                raise RuntimeError('Volume shop nhận đơn sai ngày; không ghi thống kê.')
            compact.append({'shipment_id':row.get('shipment_id'),'create_time':row.get('create_time')})
        return compact
    try:
        with ThreadPoolExecutor(max_workers=min(8,settings.workers)) as pool:
            batches=pool.map(window,[(left,left+3599) for left in range(start,start+86400,3600)])
            rows=[row for batch in batches for row in batch]
        ids=[str(row.get('shipment_id')or'').strip() for row in rows]
        if any(not pid for pid in ids)or len(ids)!=len(set(ids)):raise RuntimeError('Nguồn Volume shop thiếu/trùng vận đơn.')
        return rows
    finally:client.close()


class PointsClient(PickupRouteClient):
    def get(self,path,params):
        stop_file=getattr(self,'stop_file',None)
        if stop_file and stop_file.exists():raise InterruptedError('Đã dừng đồng bộ PUP theo yêu cầu.')
        return super().get(path,params)

    def catalog(self):
        routes, expected, seen = defaultdict(set), None, set()
        for page in range(1, 1001):
            data = self.get('pickup_point_group/search', {'pageno':page,'count':200,
                'group_type':4,'station_id':self.settings.station_id,'is_filter':1})
            if not isinstance(data.get('list'), list) or 'total' not in data:
                raise RuntimeError('Thiếu danh mục PUP toàn hub.')
            total = int(data['total'])
            if expected is not None and expected != total:
                raise RuntimeError('Danh mục tuyến đổi trong lúc đọc; chạy lại.')
            expected = total
            for group in data['list']:
                if str(group.get('group_name') or '').lower() in ('all','no group'): continue
                gid = str(group.get('id'))
                if gid in seen: raise RuntimeError('Danh mục tuyến lặp trang.')
                seen.add(gid)
                if str(group.get('station_id')) != self.settings.station_id:
                    raise RuntimeError('Danh mục tuyến ngoài hub cấu hình.')
                ids = group.get('pickup_point_id_list') or ''
                ids = ids if isinstance(ids,list) else ids.split(',')
                for pid in ids:
                    if str(pid).strip(): routes[str(pid).strip()].add(group['group_name'])
            if page*200 >= total: break
        else: raise RuntimeError('Danh mục PUP vượt giới hạn trang.')
        # Include ungrouped current points; never require a KV/driver Sheet rule.
        expected = None
        point_ids = set()
        for page in range(1,1001):
            data=self.get('pickup_point/pending_assign',{'pageno':page,'count':100,
                'pickup_status':12,'group_type':4,'pickup_timeslot_list':'666,222','order_by_oldest':1})
            if not isinstance(data.get('list'),list) or 'total' not in data: raise RuntimeError('Thiếu trang PUP chưa có nhóm.')
            total=int(data['total'])
            # Current pending points move while riders work. This is an additive
            # directory enrichment, not an inventory replacement or assignment.
            expected=total
            for point in data['list']:
                pid=str(point.get('pickup_point_id')or'').strip()
                if not pid: raise RuntimeError('PUP thiếu mã khi phân trang.')
                point_ids.add(pid)
                routes.setdefault(pid,set())
            if page*100>=total: return routes
        raise RuntimeError('Danh mục PUP vượt giới hạn trang.')

    def point(self, pid):
        data = self.get('pickup_point/detail', {'pickup_point_id':pid,'is_edit':'true'})
        extra = data.get('extra') or {}
        if str(extra.get('pickup_point_id')) != pid or str(extra.get('station_id')) != self.settings.station_id:
            raise RuntimeError('Chi tiết PUP sai mã hoặc sai hub; bỏ qua và báo lỗi.')
        district = str(extra.get('pickup_point_district') or '').strip()
        address = str(extra.get('full_address') or extra.get('pickup_point_address') or '').strip()
        if not extra.get('pickup_point_name') or not address:
            raise RuntimeError('PUP thiếu tên/địa chỉ; không ghi dữ liệu thiếu.')
        return {'pickup_point_id':pid,'pup_code':pid,'shop_name':str(extra['pickup_point_name']),
                'shop_address':address,'ward':parse_pickup_ward(district,address),
                'district':district,'station_id':self.settings.station_id}


def daily_counts(orders):
    seen, counts, missing = set(), Counter(), 0
    for order in orders:
        sid = str(order.get('shipment_id') or '').strip()
        if not sid or sid in seen: raise ValueError('Volume PUP thiếu/trùng mã đơn.')
        seen.add(sid)
        pid = str(order.get('pickup_point_id') or '').strip()
        if pid: counts[pid] += 1
        else: missing += 1
    return [{'pickup_point_id':pid,'total_orders':count} for pid,count in sorted(counts.items())], missing


def publish_daily(settings, day, orders):
    from .pickup_routes import pickup_point
    # Confirm whether an order really has no PUP instead of turning an unknown
    # mapping into a zero for every shop. Ambiguous or failed lookups stay unknown.
    missing_orders=[row for row in orders if not str(row.get('pickup_point_id')or'').strip()]
    excluded=0
    if missing_orders:
        client=PickupRouteClient(settings)
        def resolve(row):
            sid=str(row.get('shipment_id')or'').strip()
            try:
                data=client.get('order/detail/pickup_info',{'shipment_id':sid,'station_type':12})
                if str(data.get('shipment_id')or'')!=sid:return False
                point=pickup_point(data)
                if point:row['pickup_point_id']=point;return False
                return not any(str(info.get('pickup_point_id')or'').strip() for info in data['pickup_info_list'] if isinstance(info,dict))
            except Exception:return False
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:excluded=sum(pool.map(resolve,missing_orders))
        finally:client.close()
    rows, missing = daily_counts(orders)
    logging.info('Volume shop %s: %s đơn nguồn, %s được SPX xác nhận không thuộc PUP, %s chưa xác định.',day,len(orders),excluded,missing-excluded)
    control = Control(settings)
    try:
        return control.rpc('publish_pickup_shop_volume', p_station=settings.station_id,
            p_day=day,p_rows=rows,p_input=len(orders)-excluded,p_unmapped=missing-excluded)
    finally: control.close()


def sync_points(settings, *, root=ROOT, dry_run=False, stop_file=None):
    """Resume successful detail reads; publish in small batches without deleting old rows."""
    with RunLock(Path(root)/'data'/'pickup-points-directory.lock'):
        cache_file = Path(root)/'data'/f'pickup-directory-{settings.station_id}.json'
        cache = json.loads(cache_file.read_text(encoding='utf-8')) if cache_file.exists() else {}
        client, control = PointsClient(settings), Control(settings)
        client.stop_file=Path(stop_file) if stop_file else None
        try:
            routes = client.catalog()
            if not routes: raise RuntimeError('Danh mục PUP rỗng; giữ dữ liệu cũ.')
            logging.info('Danh mục toàn hub: %s PUP, không lọc KV hoặc Sheet gán.',len(routes))
            needed = {pid for pid in routes if pid not in cache or time.time()-cache[pid]['at']>86400}
            def fetch(pid): return pid,client.point(pid)
            errors, batch, published = 0, [], 0
            def save():
                pending = cache_file.with_suffix('.pending')
                pending.write_text(json.dumps(cache,ensure_ascii=False),encoding='utf-8')
                pending.replace(cache_file)
            def publish():
                nonlocal published
                if batch and not dry_run:
                    control.rpc('sync_pickup_point_catalog',p_station=settings.station_id,p_rows=batch)
                published += len(batch)
                batch.clear()
            def add(pid,row):
                row=dict(row);names=sorted(routes[pid])
                row.update(route_name=names[0] if len(names)==1 else None,mapped_pickup_point_group='; '.join(names))
                batch.append(row)
                if len(batch)>=200: publish()
            for pid in sorted(routes):
                if pid in cache and pid not in needed: add(pid,cache[pid]['row'])
            publish()
            with ThreadPoolExecutor(max_workers=min(8,settings.inventory_workers)) as pool:
                for index, future in enumerate(as_completed([pool.submit(fetch,pid) for pid in needed]),1):
                    try:
                        pid,row = future.result()
                    except Exception:
                        errors += 1
                        continue
                    cache[pid]={'at':time.time(),'row':row}
                    add(pid,row)
                    if index%500==0:save()
                    if index%100==0:logging.info('Đã đọc %s/%s PUP; lỗi %s.',index,len(needed),errors)
            save()
            if client.stop_file and client.stop_file.exists():raise InterruptedError('Đã dừng đồng bộ PUP; có thể chạy lại từ cache.')
            publish()
            logging.info('Pickup Points: %s PUP đồng bộ; %s lỗi chi tiết. Không gửi lệnh gán.',published,errors)
            if errors: raise RuntimeError(f'{errors} PUP chưa đọc được; chạy lại để hoàn tất từ cache.')
            return published
        finally: client.close();control.close()


def backfill(settings, days, root=ROOT):
    """Re-fetch the same authoritative tracking source as Pickup Volume."""
    from .pickup_routes import enrich_pickup_routes
    for delta in range(days,0,-1):
        day=(datetime.now(TZ).date()-timedelta(days=delta)).isoformat()
        with RunLock(Path(root)/'data'/f'pickup-{day}.lock'):
            logging.info('Bắt đầu Volume shop %s.',day)
            orders=fetch_shop_orders(settings,day)
            enrich_pickup_routes(settings,orders,day,root,client_factory=ShopRouteClient)
            publish_daily(settings,day,orders)
            logging.info('Volume shop %s: %s đơn nguồn.',day,len(orders))


class ShopRouteClient(PickupRouteClient):
    def get(self,path,params):
        if path=='fm_hub/pickup_order/search':params={**params,'count':1000}
        return super().get(path,params)


def main():
    parser=argparse.ArgumentParser(description='Danh mục PUP toàn hub và ADO shop; không gán rider.')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--backfill-days',type=int,choices=range(1,31))
    parser.add_argument('--stop-file',type=Path)
    args=parser.parse_args();configure_logging()
    settings=Settings.load()
    if args.backfill_days:
        if args.dry_run: parser.error('Backfill ghi thống kê; không ghép --dry-run.')
        backfill(settings,args.backfill_days)
    else: sync_points(settings,dry_run=args.dry_run,stop_file=args.stop_file)


if __name__=='__main__':main()
