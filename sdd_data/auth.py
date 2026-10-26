"""Project-owned SPX login, encrypted credentials and one-retry API sessions."""
import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)
_lock = threading.Lock()


def auth_failure(response):
    if response.status_code in (401, 403) or 300 <= response.status_code < 400:
        return True
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except ValueError:
        return "text/html" in response.headers.get("Content-Type", "").lower()
    return isinstance(body, dict) and (body.get("is_login") is False or
        body.get("retcode", body.get("error")) in (401, 403, 90309999, "401", "403", "90309999") or
        bool(body.get("redirect_to_error_page")))


def _crypt(data, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("Lưu tài khoản tự động yêu cầu Windows DPAPI.")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = Blob()
    if decrypt:
        ok = ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target))
    else:
        ok = ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), "SDD Web Data SPX", None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise RuntimeError("Windows không mở/lưu được tài khoản SPX của người dùng hiện tại.")
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        ctypes.windll.kernel32.LocalFree(target.data)


def atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    pending.write_bytes(data)
    os.replace(pending, path)


class CookieManager:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.cookie_file
        self.state = self.path.parent.parent / "auth"
        self.credentials_path = self.state / "credentials.dat"

    def fingerprint(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.is_file() else "missing"

    def credentials(self):
        if not self.credentials_path.is_file():
            return None
        try:
            value = json.loads(_crypt(base64.b64decode(self.credentials_path.read_bytes()), decrypt=True))
            return (value["username"], value["password"]) if value.get("username") and value.get("password") else None
        except Exception:
            raise RuntimeError("Không đọc được tài khoản SPX đã mã hóa. Lưu lại tài khoản trong SDD Web Data.") from None

    def save_credentials(self, username, password):
        if not username.strip() or not password:
            raise ValueError("Nhập đủ tài khoản và mật khẩu SPX.")
        raw = json.dumps({"username": username.strip(), "password": password}).encode()
        atomic_write(self.credentials_path, base64.b64encode(_crypt(raw)))

    def migrate_once(self):
        """One-time explicit migration; no runtime dependency on the old cookie."""
        source = self.settings.launcher_dir / "config/cookies/cookies_assign_pick.json"
        if not self.path.exists() and source.is_file() and source.resolve() != self.path.resolve():
            atomic_write(self.path, source.read_bytes())
        # Existing DPAPI ciphertext remains protected by the same Windows user.
        legacy = Path(os.environ.get("LOCALAPPDATA", "")) / "OrderVolumeSPX/spx_credentials.dat"
        if not self.credentials_path.exists() and legacy.is_file():
            atomic_write(self.credentials_path, legacy.read_bytes())
        return {"local_cookie": self.path.is_file(), "saved_account": self.credentials_path.is_file()}

    @contextmanager
    def locked(self):
        with _lock:
            self.state.mkdir(parents=True, exist_ok=True)
            handle = (self.state / "refresh.lock").open("a+b")
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            deadline = time.monotonic() + 360
            try:
                while True:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise RuntimeError("Đang có tiến trình khác làm mới cookie SPX. Thử lại sau.") from None
                        time.sleep(.25)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def cookies(self):
        from .clients import read_cookies, AuthenticationError
        try:
            return read_cookies(self.path)
        except (AuthenticationError, ValueError, TypeError):
            return self.refresh(rejected=self.fingerprint())

    def refresh(self, *, rejected=None, interactive=False):
        from .clients import read_cookies
        with self.locked():
            if rejected is not None and self.fingerprint() != rejected:
                return read_cookies(self.path)
            failure = self.state / "failed-refresh.json"
            if not interactive and failure.is_file() and time.time() - failure.stat().st_mtime < 60:
                raise RuntimeError("Làm mới cookie vừa thất bại. Bấm Đăng nhập SPX trong SDD Web Data để kiểm tra.")
            log.info("SDD Web Data: đang làm mới phiên SPX bằng profile riêng%s.", " (Chrome hiện)" if interactive else " (nền)")
            try:
                cookies = self.browser_login(interactive=interactive)
                self.verify(cookies)
                atomic_write(self.path, json.dumps(cookies, ensure_ascii=False).encode("utf-8"))
                result = read_cookies(self.path)
                atomic_write(self.state / "session-established", b"1")
                failure.unlink(missing_ok=True)
                log.info("Đã xác minh và lưu cookie SPX riêng của SDD Web Data.")
                return result
            except Exception as error:
                atomic_write(failure, b"{}")
                # Selenium/HTTP exceptions can contain submitted values: never log them.
                if isinstance(error, (ImportError, ModuleNotFoundError)):
                    raise RuntimeError("Thiếu Selenium. Chạy setup.cmd của SDD Web Data.") from None
                raise RuntimeError("Chưa làm mới được phiên SPX. Bấm Đăng nhập SPX trong SDD Web Data; kiểm tra tài khoản hoặc OTP/CAPTCHA.") from None

    def note_rejected(self):
        with self.locked():
            atomic_write(self.state / "failed-refresh.json", b"{}")

    def verify(self, cookies):
        """Confirm a real API session before replacing the last cookie file."""
        with requests.Session() as session:
            for cookie in cookies:
                domain = str(cookie.get("domain") or "").lstrip(".").lower()
                if domain in ("spx.shopee.vn", "shopee.vn"):
                    session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path") or "/")
            session.headers.update({"x-csrftoken": next((c["value"] for c in cookies if c.get("name") == "csrftoken"), ""),
                "app": "FMS Portal", "x-api-source": "pc", "origin": "https://spx.shopee.vn", "referer": "https://spx.shopee.vn/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"})
            now = int(time.time())
            response = session.post("https://spx.shopee.vn/api/fleet_order/order/tracking_list/search",
                json={"count":1,"page_no":1,"pickup_station_ids":self.settings.station_id,
                      "create_time":f"{now-3600},{now}","order_account":"21,22"}, timeout=30, allow_redirects=False)
            if auth_failure(response) or response.status_code != 200:
                raise RuntimeError("Phiên mới chưa được SPX chấp nhận.")
            body = response.json()
            if body.get("retcode", body.get("error", 0)) not in (None, 0, "0") or not isinstance(body.get("data", {}).get("list"), list):
                raise RuntimeError("Không xác minh được API bằng cookie mới.")

    def browser_login(self, *, interactive=False):
        from selenium import webdriver
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.chrome.service import Service
        credentials = self.credentials()
        if not interactive and not credentials and not (self.state / "session-established").is_file():
            raise RuntimeError("Chưa đăng nhập SPX trong SDD Web Data.")
        options = webdriver.ChromeOptions()
        # A newly saved account gets its own profile, avoiding reuse of another account's session.
        profile_name = 'chrome-profile-' + hashlib.sha256(self.credentials_path.read_bytes()).hexdigest()[:16] if credentials else 'chrome-profile'
        profile = (self.state / profile_name).resolve()
        profile.mkdir(parents=True, exist_ok=True)
        options.add_argument(f"--user-data-dir={profile}")
        options.add_argument("--disable-notifications")
        if not interactive:
            options.add_argument("--headless=new")
        options.add_experimental_option("prefs", {"credentials_enable_service":False,"profile.password_manager_enabled":False})
        # Selenium Manager only locates the matching ChromeDriver; no legacy imports.
        driver = webdriver.Chrome(options=options, service=Service(log_output=os.devnull))
        submitted = False
        try:
            driver.set_page_load_timeout(45)
            driver.get("https://spx.shopee.vn")
            deadline = time.monotonic() + (300 if interactive else 60)
            while time.monotonic() < deadline:
                host = urlparse(driver.current_url).hostname or ""
                passwords = [e for e in driver.find_elements(By.CSS_SELECTOR, 'input[type="password"]') if e.is_displayed()]
                cookies = driver.get_cookies() if host == "spx.shopee.vn" else []
                if not passwords and any(c.get("name") == "fms_user_skey" and c.get("value") for c in cookies):
                    return cookies
                # Only submit saved credentials on Shopee-owned login origins.
                trusted = host == "shopee.vn" or host.endswith(".shopee.vn")
                if credentials and passwords and trusted and not submitted:
                    usernames = [e for e in driver.find_elements(By.CSS_SELECTOR,
                        'input[autocomplete="username"], input[name="username"], input[type="text"]') if e.is_displayed()]
                    if usernames:
                        usernames[0].clear()
                        usernames[0].send_keys(credentials[0])
                        passwords[0].clear()
                        passwords[0].send_keys(credentials[1])
                        passwords[0].send_keys(Keys.ENTER)
                        submitted = True
                time.sleep(1)
            raise RuntimeError("SPX yêu cầu kiểm tra đăng nhập trực tiếp.")
        finally:
            driver.quit()


class SpxSession(requests.Session):
    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    def request(self, method, url, **kwargs):
        if urlparse(url).hostname != "spx.shopee.vn":
            raise ValueError("Phiên SPX chỉ dùng cho spx.shopee.vn.")
        cookies = self.manager.cookies()
        rejected = self.manager.fingerprint()
        self.cookies.clear()
        self.cookies.update(cookies)
        self.headers["x-csrftoken"] = cookies.get("csrftoken", "")
        response = super().request(method, url, **kwargs)
        if auth_failure(response):
            response.close()
            cookies = self.manager.refresh(rejected=rejected)
            self.cookies.clear()
            self.cookies.update(cookies)
            self.headers["x-csrftoken"] = cookies.get("csrftoken", "")
            response = super().request(method, url, **kwargs)
            if auth_failure(response):
                self.manager.note_rejected()
        return response
