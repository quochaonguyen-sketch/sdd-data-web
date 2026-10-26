from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta
import csv
import json
import logging
import math
import re
from pathlib import Path
import time as clock
import threading
from urllib.parse import quote

from .config import Settings
from .transform import JOBS, TZ, UNRESOLVED_AREA, coverage_map, normalized

log = logging.getLogger(__name__)
SPX_URL = "https://spx.shopee.vn/api/fleet_order/order/tracking_list/search"


def split_window(start, end, total):
    """Use total to avoid a deep binary tree; child ranges remain disjoint/inclusive."""
    seconds = end - start + 1
    parts = min(seconds, max(2, math.ceil(total / 160)))
    return [(start + index * seconds // parts, start + (index + 1) * seconds // parts - 1)
            for index in range(parts)]


_DISTRICT_PATTERNS = [
    (re.compile(r"\b(?:quan|q\.?)\s*" + str(n) + r"\b"), f"Quận {n}") for n in range(1, 13)
] + [
    (re.compile(r"\b(?:quan|q\.?)\s*" + key + r"\b"), name) for key, name in [
        ("go vap", "Quận Gò Vấp"), ("binh thanh", "Quận Bình Thạnh"), ("binh tan", "Quận Bình Tân"),
        ("tan binh", "Quận Tân Bình"), ("tan phu", "Quận Tân Phú"), ("phu nhuan", "Quận Phú Nhuận")
    ]
] + [
    (re.compile(r"\bhuyen\s*hoc mon\b"), "Huyện Hóc Môn"),
    (re.compile(r"\bhuyen\s*binh chanh\b"), "Huyện Bình Chánh"),
    (re.compile(r"\b(?:thanh pho|tp\.?|quan)\s*thu duc\b"), "Thành phố Thủ Đức"),
]


def volume_fields(row):
    keys = {"shipment_id", "create_time", "ctime", "createTime", "current_station_received_time",
            "zone_id", "pickup_zone_id", "sender_zone_id", "receiver_zone_id", "buyer_zone_id",
            "seller_zone_id", "lowest_seller_address_id", "lowest_seller_address_name",
            "seller_ward_name", "seller_district_name", "seller_addr_district", "seller_city_id",
            "driver_id", "driver_name", "delivering_time", "delivered_time", "pickup_time", "order_status"}
    keys.update({"lowest_buyer_address_id", "lowest_buyer_address_name", "buyer_district_name", "receiver_ward_name"})
    result = {key: value for key, value in row.items() if key in keys}
    # Retain only a district label, never the seller's street address.
    address = normalized(row.get("seller_address"))
    districts = {name for pattern, name in _DISTRICT_PATTERNS if pattern.search(address)}
    if len(districts) == 1 and not result.get("seller_district_name"):
        result["seller_district_name"] = districts.pop()
    if row.get("buyer_address"):
        # Reuse the locality-only parser; never retain street addresses.
        buyer = volume_fields({"seller_address": row["buyer_address"]})
        if buyer.get("seller_district_name"):
            result["buyer_district_name"] = buyer["seller_district_name"]
    return result


class AuthenticationError(RuntimeError):
    pass


def read_cookies(path: Path) -> dict:
    if not path.is_file():
        raise AuthenticationError("Chưa có cookie SPX. Bấm Đăng nhập SPX trong SDD Web Data.")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    data = data.get("cookies", []) if isinstance(data, dict) else data
    if not isinstance(data, list):
        raise AuthenticationError("File cookie phải là danh sách cookie JSON.")
    cookies = {}
    for item in data:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        domain = str(item.get("domain") or "spx.shopee.vn").lstrip(".").lower()
        if domain not in ("spx.shopee.vn", "shopee.vn"):
            continue
        if item["name"] in ("fms_user_skey", "csrftoken"):
            expiry = item.get("expiry", item.get("expires", -1))
            if isinstance(expiry, (int, float)) and expiry > 0 and expiry <= clock.time() + 60:
                raise AuthenticationError("Cookie SPX hết hạn/sắp hết hạn. Bấm Đăng nhập SPX trong SDD Web Data rồi chạy lại.")
        cookies[str(item["name"])] = str(item.get("value") or "")
    if not cookies.get("fms_user_skey"):
        raise AuthenticationError("Thiếu cookie phiên SPX fms_user_skey. Bấm Đăng nhập SPX trong SDD Web Data.")
    return cookies


def load_coverage(settings: Settings, csv_path: Path | None = None):
    if csv_path:
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            return coverage_map(list(csv.reader(handle)))
    if not settings.service_account_file.is_file():
        raise RuntimeError("Thiếu Google service account để đọc Coverage SA; có thể dùng --mapping-csv.")
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    credentials = Credentials.from_service_account_file(
        settings.service_account_file, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    sheet_range = "'" + settings.sheet_name.replace("'", "''") + "'!A:ZZ"
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(settings.sheet_id, safe='')}/values/{quote(sheet_range, safe='')}"
    with AuthorizedSession(credentials) as session:
        response = session.get(url, timeout=45)
        if response.status_code != 200:
            raise RuntimeError(f"Không đọc được Coverage SA (HTTP {response.status_code}). Kiểm tra quyền đọc Google Sheet.")
        result = coverage_map(response.json().get("values", []))
    alias_file = settings.coverage_alias_file
    if alias_file and alias_file.is_file():
        with alias_file.open(encoding="utf-8-sig", newline="") as handle:
            result.update(coverage_map(list(csv.reader(handle))))
    return result


def load_seller_wards(settings: Settings):
    """Resolve SPX seller address IDs using the existing read-only ward catalog."""
    from google.oauth2.service_account import Credentials
    from google.auth.transport.requests import AuthorizedSession
    credentials = Credentials.from_service_account_file(settings.service_account_file,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(settings.sheet_id, safe='')}/values/{quote('ward_new_upload!A:B', safe='')}"
    with AuthorizedSession(credentials) as session:
        response = session.get(url, timeout=45)
        if response.status_code != 200:
            raise RuntimeError(f"Không đọc được danh mục phường seller (HTTP {response.status_code}).")
        return {str(row[0]).strip(): str(row[1]).strip() for row in response.json().get('values', [])
                if len(row) >= 2 and str(row[0]).strip().isdigit() and str(row[1]).strip()}


class SpxClient:
    """Read SPX with project-owned cookies and a bounded automatic refresh."""

    api_url = SPX_URL

    def __init__(self, settings: Settings):
        self.settings = settings
        from .auth import CookieManager
        self.auth = CookieManager(settings)
        self.cookies = self.auth.cookies()
        self._local = threading.local()
        self._sessions = []
        self._sessions_lock = threading.Lock()

    def close(self):
        for session in getattr(self, "_sessions", []):
            session.close()

    def session(self):
        import requests
        if not hasattr(self, "_local"):
            self._local = threading.local()
            self._sessions, self._sessions_lock = [], threading.Lock()
        if not hasattr(self._local, "session"):
            from .auth import SpxSession
            session = SpxSession(self.auth) if hasattr(self, "auth") else requests.Session()
            session.cookies.update(self.cookies)
            session.headers.update({
                "accept": "application/json", "content-type": "application/json;charset=UTF-8",
                "origin": "https://spx.shopee.vn", "referer": "https://spx.shopee.vn/",
                "app": "FMS Portal", "x-api-source": "pc", "x-csrftoken": self.cookies.get("csrftoken", ""),
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            })
            self._local.session = session
            with self._sessions_lock:
                self._sessions.append(session)
        return self._local.session

    def post(self, payload):
        import requests
        for attempt in range(3):
            try:
                response = self.session().post(self.api_url, json=payload, timeout=60, allow_redirects=False)
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError("Không kết nối được SPX sau 3 lần thử.") from None
                clock.sleep(2 ** attempt)
                continue
            if response.status_code in (401, 403) or 300 <= response.status_code < 400:
                raise AuthenticationError("SPX từ chối phiên đăng nhập. Bấm Đăng nhập SPX trong SDD Web Data.")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    clock.sleep(2 ** attempt)
                    continue
            if response.status_code != 200:
                raise RuntimeError(f"SPX trả HTTP {response.status_code}; không coi là dữ liệu rỗng.")
            try:
                body = response.json()
            except ValueError:
                raise AuthenticationError("SPX trả trang không phải JSON. Kiểm tra phiên đăng nhập.") from None
            if not isinstance(body, dict):
                raise RuntimeError("SPX trả cấu trúc dữ liệu không hợp lệ.")
            code = body.get("retcode", body.get("error", 0))
            if body.get("is_login") is False or code in (401, 403, 90309999, "401", "403", "90309999"):
                raise AuthenticationError("Phiên SPX hết hạn hoặc không có quyền đọc báo cáo.")
            if code not in (None, 0, "0") or body.get("redirect_to_error_page"):
                raise RuntimeError("SPX báo lỗi ứng dụng; dừng để tránh ghi báo cáo thiếu.")
            data = body.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("list"), list) or "total" not in data:
                raise RuntimeError("SPX thiếu data.list/total; không coi là ngày không có đơn.")
            if any(not isinstance(row, dict) for row in data["list"]):
                raise RuntimeError("SPX trả danh sách đơn không hợp lệ.")
            return data
        raise RuntimeError("Không lấy được dữ liệu SPX.")

    def payload(self, kind, start, end, page=1):
        field = "create_time" if kind == "pickup" else "current_station_received_time"
        station = "pickup_station_ids" if kind == "pickup" else "current_station_ids"
        return {"count": 200, field: f"{start},{end}", station: self.settings.station_id,
                "page_no": page, "order_account": "21,22"}

    def fetch_window(self, kind, start, end):
        first = self.post(self.payload(kind, start, end))
        total = int(first["total"])
        if total < 0:
            raise RuntimeError("SPX trả total âm.")
        if total >= 10000 and start >= end:
            raise RuntimeError("Một giây đạt/vượt giới hạn 10.000 bản ghi SPX; không thể xác minh đủ snapshot.")
        rows = list(first["list"])
        # SPX can repeat IDs between pages even for completed historical hours.
        # Split to single-page time windows instead of relying on unstable sorting.
        if total > len(rows) and start < end:
            midpoint = (start + end) // 2
            return self.fetch_window(kind, start, midpoint) + self.fetch_window(kind, midpoint + 1, end)
        if total and not rows:
            raise RuntimeError("SPX báo có đơn nhưng trang đầu trống; hãy chạy lại.")
        page_size = int(first.get("count") or 200)
        if page_size <= 0:
            raise RuntimeError("SPX trả kích thước trang không hợp lệ.")
        pages = range(2, (total + page_size - 1) // page_size + 1)
        with ThreadPoolExecutor(max_workers=self.settings.workers) as executor:
            for batch in executor.map(lambda page: self.post(self.payload(kind, start, end, page)), pages):
                rows.extend(batch["list"])
        if len(rows) != total or len({str(row.get("shipment_id") or "") for row in rows}) != total:
            raise RuntimeError("Số đơn/phân trang thay đổi trong lúc lấy dữ liệu; dừng và chạy lại.")
        return rows

    def read_window(self, kind, start, end):
        # Fetching count=200 directly avoids a secondary round-trip for all single-page windows (<=200 orders).
        return self.post(self.payload(kind, start, end))

    def fetch(self, kind, report_date):
        day = date.fromisoformat(report_date)
        start = int(datetime.combine(day, time(), TZ).timestamp())
        finish = int((datetime.combine(day + timedelta(days=1), time(), TZ)).timestamp()) - 1
        if day == datetime.now(TZ).date():
            finish = min(finish, int(datetime.now(TZ).timestamp()) - 2)
        windows = []
        while start <= finish:
            end = min(start + 3599, finish)
            windows.append((start, end))
            start = end + 1
        rows, probes = [], 0
        with ThreadPoolExecutor(max_workers=self.settings.workers) as executor:
            pending = {executor.submit(self.read_window, kind, begin, end): (begin, end) for begin, end in windows}
            while pending:
                future = next(as_completed(pending))
                begin, end = pending.pop(future)
                data = future.result()
                probes += 1
                total, page_rows = int(data["total"]), data["list"]
                if total < 0:
                    raise RuntimeError("SPX trả total âm.")
                if total > len(page_rows):
                    if begin == end:
                        # A bulk-created second can exceed one page. Strictly verify the fallback.
                        rows.extend(volume_fields(row) for row in self.fetch_window(kind, begin, end))
                    else:
                        for left, right in split_window(begin, end, total):
                            pending[executor.submit(self.read_window, kind, left, right)] = (left, right)
                else:
                    if len(page_rows) != total or len({str(row.get("shipment_id") or "") for row in page_rows}) != total:
                        raise RuntimeError("SPX trả total không khớp danh sách hoặc đơn trùng trong một trang.")
                    rows.extend(volume_fields(row) for row in page_rows)
                if probes % 20 == 0 or not pending:
                    log.info("%s: %s đơn | %s khoảng đã kiểm tra | %s khoảng còn lại.", kind, len(rows), probes, len(pending))
        return rows


class WebsiteStore:
    """Only the two summary tables actually read by the website."""

    tables = tuple(JOBS.values())

    def __init__(self, settings: Settings):
        if not settings.supabase_url or not settings.supabase_key:
            raise RuntimeError("Thiếu Supabase của website. Kiểm tra WEBSITE_DIR/.env hoặc cấu hình riêng.")
        import requests
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({"apikey": settings.supabase_key,
            "Authorization": f"Bearer {settings.supabase_key}", "Content-Type": "application/json"})

    def request(self, method, table, params, rows=None):
        if table not in self.tables:
            raise ValueError("Bảng không thuộc phạm vi đồng bộ của project.")
        import requests
        for attempt in range(3):
            try:
                response = self.session.request(method, f"{self.settings.supabase_url}/rest/v1/{table}",
                    params=params, json=rows, headers={"Prefer": "resolution=merge-duplicates,return=minimal"},
                    timeout=60, allow_redirects=False)
            except requests.RequestException:
                if attempt == 2:
                    raise RuntimeError(f"Không kết nối được bảng {table} sau 3 lần thử.") from None
                clock.sleep(2 ** attempt)
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 2:
                    clock.sleep(2 ** attempt)
                    continue
            if not response.ok or 300 <= response.status_code < 400:
                try:
                    error_code = str(response.json().get("code", "unknown"))
                except (ValueError, AttributeError):
                    error_code = "unknown"
                raise RuntimeError(f"Bảng {table}: HTTP {response.status_code}, mã {error_code}. Không xoá snapshot cũ khi ghi lỗi.")
            return response.json() if method == "GET" else None
        raise RuntimeError("Không kết nối được Supabase.")

    def scope(self, day, kind=None):
        areas = (*self.settings.areas, UNRESOLVED_AREA) if kind == "pickup" else self.settings.areas
        return {"report_date": f"eq.{day}", "area": "in.(" + ",".join(areas) + ")"}

    def check(self):
        for kind, table in JOBS.items():
            columns = "summary_id,report_date,area,cot,district,total_orders," + ("new_ward,ma_tuyen" if kind == "pickup" else "old_ward")
            self.request("GET", table, {"select": columns, "limit": "1"})
            log.info("Đọc cấu trúc bảng %s: OK.", table)

    def existing_ids(self, table, day):
        result, offset = set(), 0
        while True:
            rows = self.request("GET", table, {**self.scope(day, "pickup" if table == JOBS["pickup"] else "delivery"), "select": "summary_id",
                "order": "summary_id.asc", "limit": "500", "offset": str(offset)})
            result.update(row["summary_id"] for row in rows)
            if len(rows) < 500:
                return result
            offset += len(rows)

    def commit_snapshot(self, kind, day, rows):
        if kind not in JOBS or not rows:
            raise ValueError("Không ghi/xoá khi snapshot rỗng hoặc job không hợp lệ.")
        ids = {row["summary_id"] for row in rows}
        areas = (*self.settings.areas, UNRESOLVED_AREA) if kind == "pickup" else self.settings.areas
        if len(ids) != len(rows) or any(row["report_date"] != day or row["area"] not in areas or row["total_orders"] <= 0 for row in rows):
            raise ValueError("Snapshot có ID trùng hoặc dòng ngoài phạm vi ngày/KV.")
        table = JOBS[kind]
        stale = self.existing_ids(table, day) - ids
        size = self.settings.batch_size
        for index in range(0, len(rows), size):
            self.request("POST", table, {"on_conflict": "summary_id"}, rows[index:index + size])
        # Delete only IDs observed before the write, and only after EVERY batch succeeded.
        stale = sorted(stale)
        for index in range(0, len(stale), 30):
            escaped = [json.dumps(item, ensure_ascii=False) for item in stale[index:index + 30]]
            self.request("DELETE", table, {**self.scope(day, kind), "summary_id": "in.(" + ",".join(escaped) + ")"})
        return {"upserted": len(rows), "stale_removed": len(stale)}

    def close(self):
        self.session.close()
