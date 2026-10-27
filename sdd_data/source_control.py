"""Private RPC client for the allowlisted source control plane. Never accepts a shell command."""
import requests

SOURCES = frozenset(('pickup_inventory', 'delivery_inventory', 'pickup_realtime', 'delivery_realtime',
                     'pickup_volume', 'delivery_volume', 'performance', 'return_inventory', 'onhold_daily', 'linehaul'))
RPCS = frozenset(('sync_pickup_point_catalog', 'publish_pickup_shop_volume', 'publish_central_delivery_realtime', 'claim_delivery_realtime_once', 'advertise_spx_session_key', 'consume_spx_credentials', 'pulse_spx_session', 'finish_spx_session_refresh', 'control_data_source', 'pulse_data_source_worker', 'claim_data_source_job',
                 'pulse_data_source_job', 'begin_data_source_publish', 'begin_data_source_inventory',
                 'finish_data_source_job', 'publish_central_pickup', 'bridge_combined_inventory', 'publish_central_returns', 'enqueue_due_data_sources', 'publish_central_onhold', 'record_central_onhold', 'finalize_onhold_days', 'overnight_hold_capture_due', 'publish_overnight_hold', 'publish_linehaul'))


class Control:
    def __init__(self, settings):
        self.url = settings.supabase_url + '/rest/v1/'
        self.session = requests.Session()
        self.session.headers.update({'apikey': settings.supabase_key,
            'Authorization': 'Bearer ' + settings.supabase_key, 'Content-Type': 'application/json'})

    def rpc(self, name, **payload):
        if name not in RPCS:
            raise ValueError('RPC not allowed')
        response = self.session.post(self.url + 'rpc/' + name, json=payload,
                                     timeout=90 if name == 'publish_central_pickup' else 20,
                                     allow_redirects=False)
        if not response.ok:
            # Keep service keys, request URLs and remote exception payloads out of browser logs.
            import re
            try:
                code = str(response.json().get('code', 'unknown'))
            except (ValueError, AttributeError):
                code = 'unknown'
            code = code if re.fullmatch(r'[A-Z0-9]{5,12}', code) else 'unknown'
            raise RuntimeError(f'Control RPC {name}: HTTP {response.status_code}, mã {code}')
        # PostgREST returns an empty body for void RPCs (including HTTP 200).
        # A successful acknowledgement must not stall subsequent worker heartbeats.
        return response.json() if response.content.strip() else None

    def pending_inventory(self):
        response = self.session.get(self.url + 'lmhub_fetch_jobs', params={
            'status': 'eq.PENDING', 'select': 'id,params,requested_by',
            'order': 'requested_at.asc', 'limit': '10'}, timeout=20, allow_redirects=False)
        response.raise_for_status()
        return response.json()

    def close(self):
        self.session.close()
