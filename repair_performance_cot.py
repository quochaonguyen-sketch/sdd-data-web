"""Repair rider COT using real SPX orders; explicitly opt into publication."""
import argparse
from unittest.mock import patch
from sdd_data.auth import CookieManager
from sdd_data.clients import read_cookies
from sdd_data.config import Settings
from sdd_data.runner import run,configure_logging
p=argparse.ArgumentParser();p.add_argument('day');p.add_argument('--publish',action='store_true');args=p.parse_args()
s=Settings.load();read_cookies(s.cookie_file);configure_logging()
with patch.object(CookieManager,'refresh',side_effect=RuntimeError('Phiên SPX hết hạn; không tự mở trình duyệt')):
 result=run(s,'performance',args.day,dry_run=not args.publish)
 print({'day':args.day,'status':result[0]['status'],'riders':result[0]['stats']['riders']})
