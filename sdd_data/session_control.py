"""Cookie maintenance without sending credentials/cookie values to the control plane."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import json
import math

from .auth import CookieManager

log = logging.getLogger(__name__)


def cookie_expiry(path):
    """Read expiry metadata only; unrelated tracking-cookie lifetimes are irrelevant."""
    if not path.is_file():
        return None, 'missing'
    try:
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        rows = data.get('cookies', []) if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return None, 'unknown'
        auth = [c for c in rows if isinstance(c, dict) and c.get('name') in ('fms_user_skey', 'csrftoken')
                and str(c.get('domain') or 'spx.shopee.vn').lstrip('.').lower() in ('spx.shopee.vn', 'shopee.vn') and c.get('value')]
        tokens = [c for c in auth if c['name'] == 'fms_user_skey']
        if not tokens:
            return None, 'missing'
        def expiry(c):
            value = c.get('expiry', c.get('expires'))
            return value if type(value) in (int, float) and math.isfinite(value) and value > 0 else None
        if any(expiry(c) is None for c in tokens):
            return None, 'session'
        expires = min(expiry(c) for c in auth if expiry(c) is not None)
        return datetime.fromtimestamp(expires, timezone.utc).isoformat(), 'fixed'
    except (OSError, ValueError, TypeError, OverflowError):
        return None, 'unknown'


class SessionControl:
    def __init__(self, settings):
        self.manager = CookieManager(settings)
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='spx-session')
        self.future = None
        self.request = None
        self.transport = None
        self.advertised = False

    def metadata(self):
        path = self.manager.path
        expires, kind = cookie_expiry(path)
        return dict(p_cookie=path.is_file(),
                    p_account=self.manager.credentials_path.is_file(),
                    p_profile=(self.manager.state / 'session-established').is_file(),
                    p_updated=datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
                    if path.is_file() else None, p_expires=expires, p_expiry_kind=kind)

    def refresh(self, envelope=None, worker=None):
        try:
            if envelope is not None:
                username, password = self.transport.decrypt(envelope, worker)
                with self.manager.locked():
                    self.manager.save_credentials(username, password)
                    (self.manager.state / 'failed-refresh.json').unlink(missing_ok=True)
            # Share the existing cross-process lock and verify before replacing cookies.
            # Coalesce with a source job that already refreshed this cookie generation.
            self.manager.refresh(rejected=None if envelope is not None else self.manager.fingerprint())
            return True
        except Exception:
            # Never publish Selenium exceptions or submitted values.
            log.warning('SPX session refresh failed; use local SPX login to check account/OTP.')
            return False

    def tick(self, control, worker):
        if self.future is not None and self.future.done():
            control.rpc('finish_spx_session_refresh', p_worker=worker,
                        p_request=self.request, p_success=self.future.result())
            self.future = self.request = None
        request = control.rpc('pulse_spx_session', p_worker=worker, **self.metadata())
        if not self.advertised:
            try:
                from .credential_transport import CredentialTransport
                if self.transport is None:
                    self.transport = CredentialTransport(self.manager)
                control.rpc('advertise_spx_session_key', p_worker=worker,
                            p_key=self.transport.public_key, p_key_id=self.transport.key_id)
                self.advertised = True
            except Exception:
                log.warning('SPX web credential setup unavailable; existing cookie refresh remains enabled.')
        if request and self.future is None:
            self.request = request
        if self.request and self.future is None:
            envelope = control.rpc('consume_spx_credentials', p_worker=worker, p_request=self.request)
            self.future = self.pool.submit(self.refresh, envelope, worker)

    def close(self):
        self.pool.shutdown(wait=True)
