"""Start the central worker without leaving a console window open."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import subprocess
import sys
import time

from .config import ROOT
from .runner import RunLock


def is_running(root):
    try:
        with RunLock(Path(root) / 'data' / 'source-worker' / 'daemon.lock'):
            return False
    except RuntimeError:
        return True


def start_background(root=ROOT, concurrency=4):
    root = Path(root)
    if not 1 <= concurrency <= 4:
        raise ValueError('Concurrency 1..4')
    work = root / 'data' / 'source-worker'
    with RunLock(work / 'start.lock'):
        if is_running(root):
            print('Worker đã chạy nền. Không cần bật thêm bản khác.')
            return 0
        logs = root / 'logs'
        logs.mkdir(parents=True, exist_ok=True)
        output_path = logs / f'source-worker-background-{datetime.now():%Y%m%d-%H%M%S-%f}.log'
        with output_path.open('ab') as output:
            child = subprocess.Popen([sys.executable, '-u', '-m', 'sdd_data.source_worker',
                                      '--concurrency', str(concurrency)], cwd=root,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                start_new_session=os.name != 'nt', env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
            for _ in range(40):
                if child.poll() is not None:
                    print(f'Worker chưa khởi động được. Xem nhật ký: {output_path}')
                    return 1
                if is_running(root):
                    print('Đã bật worker chạy nền. Bạn có thể đóng cửa sổ này.')
                    print('Điều khiển nguồn tại web → Excel & dữ liệu → Nguồn dữ liệu.')
                    print('Muốn tắt hẳn: mở stop_worker.cmd.')
                    return 0
                time.sleep(0.25)
        print(f'Worker đang khởi động. Kiểm tra trạng thái trên web; nhật ký: {output_path}')
        return 0


def main():
    parser = argparse.ArgumentParser(description='Bật worker trung tâm chạy nền')
    parser.add_argument('--concurrency', type=int, choices=range(1, 5), default=4)
    args = parser.parse_args()
    try:
        return start_background(concurrency=args.concurrency)
    except (RuntimeError, OSError) as error:
        print(f'Không bật được worker: {error}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
