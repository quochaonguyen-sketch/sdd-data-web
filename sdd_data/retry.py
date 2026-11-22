"""Retry helpers shared by the central source workers and HTTP readers."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import re
import time

import requests


RETRY_EXIT = 75
ROLLED_BACK_EXIT = 76
RETRYABLE_HTTP = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
ROLLED_BACK_SQL = {'57014', '55P03', '40001', '40P01'}
ATOMIC_PUBLICATIONS = {
    'publish_central_pickup', 'publish_central_returns', 'publish_linehaul',
    'publish_central_delivery_realtime', 'publish_wh_tracking',
}


class ControlError(RuntimeError):
    def __init__(self, operation, status, code):
        self.operation, self.status, self.code = operation, status, code
        super().__init__(f'Control RPC {operation}: HTTP {status}, mã {code}')


class SourceBusy(RuntimeError):
    pass


def wait_before_retry(attempt, response=None):
    delay = min(30, 2 ** (attempt + 1)) + random.uniform(0, 2)
    if response is not None:
        hint = getattr(response, 'headers', {}).get('Retry-After', '')
        try:
            delay = max(delay, int(hint))
        except (ValueError, TypeError):
            try:
                date = parsedate_to_datetime(hint)
                if date.tzinfo is not None:
                    delay = max(delay, (date - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError):
                pass
        if callable(getattr(response, 'close', None)):
            response.close()
    time.sleep(min(delay, 90))


def retry_reason(error, publishing=False):
    # Even when a source hides a requests error with "from None", its context is retained.
    if publishing:
        if (isinstance(error, ControlError) and error.operation in ATOMIC_PUBLICATIONS
                and error.code in ROLLED_BACK_SQL):
            return 'rolled_back'
        return None
    from .clients import AuthenticationError

    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (AuthenticationError, requests.exceptions.SSLError)):
            return None
        if isinstance(error, SourceBusy):
            return 'busy'
        if isinstance(error, (requests.exceptions.Timeout, TimeoutError)):
            return 'timeout'
        if isinstance(error, requests.exceptions.ConnectionError):
            return 'network'
        if isinstance(error, ControlError):
            if error.status in RETRYABLE_HTTP or error.code in ROLLED_BACK_SQL | {'PGRST003'}:
                return 'upstream'
            return None
        if isinstance(error, requests.exceptions.HTTPError) and error.response is not None:
            return 'upstream' if error.response.status_code in RETRYABLE_HTTP else None
        message = str(error)
        status = re.search(r'\bHTTP (\d{3})\b', message)
        if (status and int(status[1]) in RETRYABLE_HTTP) or re.search(r'\b90309999\b', message):
            return 'upstream'
        error = error.__cause__ or error.__context__
    return None
