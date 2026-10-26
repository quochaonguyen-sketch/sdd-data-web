"""Read-only Delivery benchmark. Never launches login UI or publishes inventory."""
import json
import logging
import time
from unittest.mock import patch
from sdd_data.config import Settings, ROOT
from sdd_data.clients import read_cookies
from sdd_data.auth import CookieManager
from sdd_data.delivery_inventory import prepare_inventory
logging.basicConfig(level=logging.INFO,format='%(asctime)s | %(message)s')
s=Settings.load();read_cookies(s.cookie_file)
with patch.object(CookieManager,'refresh',side_effect=RuntimeError('Phiên SPX hết hạn; không mở trình duyệt')):
 start=time.perf_counter()
 rows,stats=prepare_inventory(s,root=ROOT)
 print(json.dumps({'elapsed_ms':round((time.perf_counter()-start)*1000),'rows':len(rows),'stats':stats},ensure_ascii=False))
