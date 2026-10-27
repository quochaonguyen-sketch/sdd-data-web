"""No arguments: desktop launcher. Arguments: CLI for automation/manual runs."""
import argparse
from datetime import datetime
import logging
from pathlib import Path
import queue
import sys
import threading

from sdd_data.config import Settings
from sdd_data.runner import check, configure_logging, run, validate_day
from sdd_data.transform import TZ


def open_gui(settings):
    import tkinter as tk
    from tkinter import messagebox, ttk
    from tkinter.scrolledtext import ScrolledText
    root = tk.Tk()
    root.title("SDD Web Data — Volume / Performance / Tồn Delivery")
    root.geometry("960x700")
    root.minsize(900, 620)
    root.configure(bg="#f3f6fa")
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure("TFrame", background="#f3f6fa")
    style.configure("TLabel", background="#f3f6fa", foreground="#243449", font=("Segoe UI", 10))
    style.configure("TCheckbutton", background="#f3f6fa", font=("Segoe UI", 10))
    style.configure("TButton", font=("Segoe UI", 10), padding=8)
    container = ttk.Frame(root, padding=24)
    container.pack(fill="both", expand=True)
    ttk.Label(container, text="SDD WEB DATA", font=("Segoe UI", 19, "bold")).pack(anchor="w")
    ttk.Label(container, text="Volume Pickup · Volume Delivery · Performance rider").pack(anchor="w", pady=(3, 6))
    ttk.Label(container, text=f"Đích: {settings.supabase_url or '(chưa cấu hình)'}", wraplength=750).pack(anchor="w")
    controls = ttk.Frame(container)
    controls.pack(fill="x", pady=18)
    ttk.Label(controls, text="Ngày báo cáo:").pack(side="left")
    selected_date = tk.StringVar(value=datetime.now(TZ).date().isoformat())
    ttk.Entry(controls, textvariable=selected_date, width=13).pack(side="left", padx=(8, 25))
    pickup, delivery = tk.BooleanVar(value=True), tk.BooleanVar(value=True)
    ttk.Checkbutton(controls, text="Pickup", variable=pickup).pack(side="left", padx=8)
    ttk.Checkbutton(controls, text="Delivery", variable=delivery).pack(side="left", padx=8)
    performance = tk.BooleanVar(value=False)
    ttk.Checkbutton(controls, text="Performance", variable=performance).pack(side="left", padx=8)
    skip_unmapped = tk.BooleanVar(value=True)
    ttk.Checkbutton(container, text="Cho phép đơn chưa rõ địa bàn: Pickup ghi nhóm Chưa rõ; Delivery bỏ qua", variable=skip_unmapped).pack(anchor="w", pady=(0, 10))
    status = tk.StringVar(value="Sẵn sàng. Nên chạy thử trước khi đồng bộ.")
    ttk.Label(container, textvariable=status, wraplength=750).pack(anchor="w", pady=(0, 8))
    messages = queue.Queue()
    busy = False

    class QueueLog(logging.Handler):
        def emit(self, record):
            messages.put(("log", self.format(record)))

    handler = QueueLog()
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S"))
    logging.getLogger().addHandler(handler)
    actions = ttk.Frame(container)
    actions.pack(fill="x", pady=(0, 12))
    output = ScrolledText(container, wrap="word", font=("Consolas", 10), bg="#172638", fg="#edf3fb", insertbackground="#edf3fb", state="disabled")
    output.pack(fill="both", expand=True)
    buttons = []

    def launch(mode):
        nonlocal busy
        if busy:
            return
        try:
            day = validate_day(selected_date.get().strip())
            job = ",".join(name for name, selected in (("pickup", pickup), ("delivery", delivery),
                                                     ("performance", performance)) if selected.get())
            if mode not in ("check", "login", "refresh") and not job:
                raise ValueError("Chọn ít nhất một job.")
            allow_unmapped = skip_unmapped.get()
        except ValueError as exc:
            messagebox.showerror("Kiểm tra lựa chọn", str(exc))
            return
        busy = True
        status.set("Đang xử lý…")
        for button in buttons:
            button.configure(state="disabled")

        def worker():
            try:
                if mode in ("login", "refresh"):
                    from sdd_data.auth import CookieManager
                    CookieManager(settings).refresh(interactive=mode == "login")
                elif mode == "check":
                    check(settings, online=True, report_date=day)
                else:
                    run(settings, job, day, dry_run=mode == "dry", allow_unmapped=allow_unmapped)
                messages.put(("done", "Hoàn tất."))
            except Exception as exc:
                messages.put(("error", str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    for caption, mode in (("Chạy thử — không ghi", "dry"), ("Đồng bộ lên web", "sync"), ("Kiểm tra kết nối", "check")):
        button = ttk.Button(actions, text=caption, command=lambda action=mode: launch(action))
        button.pack(side="left", padx=(0, 8))
        buttons.append(button)

    auth_controls = ttk.Frame(container)
    auth_controls.pack(fill="x", before=output, pady=(0, 10))
    def save_spx_account():
        if busy:
            return
        from tkinter import simpledialog
        from sdd_data.auth import CookieManager
        username = simpledialog.askstring("Tài khoản SPX", "Tên đăng nhập SPX (lưu mã hóa bằng Windows):", parent=root)
        if not username:
            return
        password = simpledialog.askstring("Tài khoản SPX", "Mật khẩu SPX:", show="*", parent=root)
        if not password:
            return
        try:
            CookieManager(settings).save_credentials(username, password)
            status.set("Đã lưu tài khoản mã hóa. Đang làm mới phiên SPX riêng…")
            launch("refresh")
        except Exception:
            messagebox.showerror("Tài khoản SPX", "Không lưu được tài khoản mã hóa. Kiểm tra quyền thư mục config/auth.")
    for caption, action in (("Lưu tài khoản SPX", save_spx_account),
                            ("Đăng nhập SPX", lambda: launch("login")),
                            ("Làm mới cookie", lambda: launch("refresh"))):
        button = ttk.Button(auth_controls, text=caption, command=action)
        button.pack(side="left", padx=(0, 8))
        buttons.append(button)
    ttk.Label(auth_controls, text="Cookie riêng · tự làm mới khi hết phiên").pack(side="left", padx=8)

    # Unified Worker controls (Deli + Pickup)
    worker_stop = threading.Event()
    worker_thread = None
    worker_controls = ttk.Frame(container)
    worker_controls.pack(fill="x", before=output, pady=(0, 10))

    ttk.Label(worker_controls, text="9 nguồn · Tự động / chu kỳ quản lý tại web → Nguồn dữ liệu").pack(side="left")

    worker_status = tk.StringVar(value="Worker trung tâm: đã dừng")
    ttk.Label(container, textvariable=worker_status).pack(anchor="w", before=output, pady=(0, 8))

    def start_worker():
        nonlocal worker_thread
        if worker_thread and worker_thread.is_alive():
            return
        from sdd_data.source_worker import run_worker as run_unified
        worker_stop.clear()
        start_worker_button.configure(state="disabled")
        stop_worker_button.configure(state="normal")
        worker_status.set("Worker trung tâm: đang chạy | 9 nguồn · tối đa 2 tác vụ song song")

        def worker_task():
            try:
                run_unified(settings, stop_event=worker_stop, concurrency=2)
                messages.put(("worker_done", "Worker tồn: đã dừng"))
            except Exception as exc:
                messages.put(("worker_done", f"Worker tồn: {exc}"))

        worker_thread = threading.Thread(target=worker_task, daemon=True)
        worker_thread.start()

    def stop_worker():
        worker_stop.set()
        worker_status.set("Worker tồn: đang dừng, đợi job hiện tại hoàn tất…")
        stop_worker_button.configure(state="disabled")

    start_worker_button = ttk.Button(worker_controls, text="Bật Worker trung tâm", command=start_worker)
    start_worker_button.pack(side="left", padx=8)
    stop_worker_button = ttk.Button(worker_controls, text="Dừng Worker", command=stop_worker, state="disabled")
    stop_worker_button.pack(side="left")

    def poll():
        nonlocal busy
        while True:
            try:
                event, text = messages.get_nowait()
            except queue.Empty:
                break
            output.configure(state="normal")
            output.insert("end", text + "\n")
            output.see("end")
            output.configure(state="disabled")
            if event in ("worker_done", "inventory_done", "pickup_done"):
                worker_status.set(text)
                start_worker_button.configure(state="normal")
                stop_worker_button.configure(state="disabled")
            if event in ("done", "error"):
                busy = False
                status.set(text)
                for button in buttons:
                    button.configure(state="normal")
        root.after(150, poll)

    def close():
        if worker_thread and worker_thread.is_alive():
            stop_worker()
            messagebox.showinfo("Worker đang dừng", "Đợi worker hoàn tất job hiện tại rồi đóng launcher.")
        elif busy:
            messagebox.showinfo("Đang xử lý", "Đợi tác vụ hoàn tất để không ngắt ngang quá trình đồng bộ.")
        else:
            logging.getLogger().removeHandler(handler)
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    poll()
    root.mainloop()


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Dữ liệu SDD web: Volume Pickup, Delivery và Performance rider.")
    parser.add_argument("--job", choices=("pickup", "delivery", "performance", "both", "all"),
                        help="both: hai job volume; all: volume và Performance.")
    parser.add_argument("--date", default=datetime.now(TZ).date().isoformat(), type=validate_day)
    parser.add_argument("--dry-run", action="store_true", help="Đọc và tổng hợp; không ghi/xoá web.")
    parser.add_argument("--check", action="store_true", help="Kiểm tra cấu hình offline.")
    parser.add_argument("--check-online", action="store_true", help="Kiểm tra quyền đọc ba bảng, mapping và API; không ghi.")
    cookie_mode = parser.add_mutually_exclusive_group()
    cookie_mode.add_argument("--login", action="store_true", help="Mở Chrome riêng để đăng nhập SPX, hỗ trợ OTP/CAPTCHA.")
    cookie_mode.add_argument("--refresh-cookie", action="store_true", help="Làm mới cookie SPX nền bằng profile/tài khoản riêng.")
    cookie_mode.add_argument("--migrate-cookie", action="store_true", help="Chuyển một lần cookie/tài khoản đã mã hóa sang SDD Web Data.")
    parser.add_argument("--mapping-csv", type=Path)
    parser.add_argument("--orders-json", type=Path, help="Dữ liệu offline, bắt buộc dùng --dry-run.")
    inventory_mode = parser.add_mutually_exclusive_group()
    inventory_mode.add_argument("--worker", action="store_true", help="Worker tồn TỔNG HỢP (Deli + Pickup): nhận yêu cầu web và tự cập nhật mỗi 30 phút.")
    inventory_mode.add_argument("--worker-once", action="store_true", help="Fetch và đồng bộ cả tồn Deli và Pickup một lần.")
    inventory_mode.add_argument("--inventory-worker", action="store_true", help="Worker tồn Deli: nhận yêu cầu web và tự cập nhật 30 phút.")
    inventory_mode.add_argument("--inventory-once", action="store_true", help="Fetch và đồng bộ tồn Deli một lần.")
    inventory_mode.add_argument("--inventory-dry-run", action="store_true", help="Fetch tồn Deli, lưu báo cáo, không nhận job/ghi web.")
    inventory_mode.add_argument("--pickup-worker", action="store_true", help="Worker tồn Pickup: nhận yêu cầu web và tự cập nhật 30 phút.")
    inventory_mode.add_argument("--pickup-once", action="store_true", help="Fetch và đồng bộ tồn Pickup một lần.")
    inventory_mode.add_argument("--pickup-dry-run", action="store_true", help="Fetch tồn Pickup, lưu báo cáo, không nhận job/ghi web.")
    parser.add_argument("--queue-only", action="store_true", help="Worker chỉ nhận job từ web, tắt tự cập nhật.")
    parser.add_argument("--lookback-days", type=int, default=14, help="Cửa sổ ngày tạo đơn tồn Deli, mặc định 14 ngày như launcher cũ.")
    parser.add_argument("--job-timeout", type=int, default=600, help="Timeout (giây) cho mỗi job tồn; mặc định 600s = 10 phút.")
    parser.add_argument("--allow-unmapped", action=argparse.BooleanOptionalAction, default=None,
                        help="Mặc định bỏ qua đơn chưa có mapping / thiếu mã tuyến; dùng --no-allow-unmapped để chặn.")
    args = parser.parse_args(argv)
    configure_logging()
    try:
        settings = Settings.load()
        if args.login or args.refresh_cookie or args.migrate_cookie:
            if args.job or args.check or args.check_online or args.worker or args.worker_once or args.inventory_worker or args.inventory_once or args.inventory_dry_run:
                parser.error("Chạy chế độ cookie riêng, không ghép với job dữ liệu.")
            from sdd_data.auth import CookieManager
            manager = CookieManager(settings)
            if args.migrate_cookie:
                migrated = manager.migrate_once()
                logging.info("Chuyển sang cookie riêng: file=%s, tài khoản mã hóa=%s.", migrated["local_cookie"], migrated["saved_account"])
            else:
                manager.refresh(interactive=args.login)
            return 0
        has_inventory = (args.worker or args.worker_once or
                         args.inventory_worker or args.inventory_once or args.inventory_dry_run or
                         args.pickup_worker or args.pickup_once or args.pickup_dry_run)
        if has_inventory and (args.job or args.check or args.check_online):
            parser.error("Chọn chế độ worker tồn riêng, không kết hợp --job/--check.")
        if args.queue_only and not (args.worker or args.worker_once or args.inventory_worker or args.inventory_once or args.pickup_worker or args.pickup_once):
            parser.error("--queue-only yêu cầu worker mode.")
        if args.inventory_dry_run:
            from sdd_data.delivery_inventory import prepare_inventory
            from sdd_data.config import ROOT
            import json
            orders = json.loads(args.orders_json.read_text(encoding="utf-8-sig")) if args.orders_json else None
            prepare_inventory(settings, root=ROOT, lookback_days=args.lookback_days, mapping_csv=args.mapping_csv, orders=orders)
        elif args.pickup_dry_run:
            from sdd_data.pickup_inventory import prepare_pickup_inventory
            from sdd_data.config import ROOT
            import json
            orders = json.loads(args.orders_json.read_text(encoding="utf-8-sig")) if args.orders_json else None
            prepare_pickup_inventory(settings, root=ROOT, lookback_days=args.lookback_days, orders=orders)
        elif args.worker or args.worker_once:
            if args.orders_json or args.mapping_csv or args.dry_run:
                parser.error("Worker không nhận dữ liệu mẫu; dùng dry-run.")
            from sdd_data.unified_worker import run_worker as run_unified_worker
            from sdd_data.source_worker import run_worker as run_central_worker
            import signal
            worker_stop = threading.Event()
            previous_handler = signal.signal(signal.SIGINT, lambda *_: worker_stop.set())
            try:
                if args.worker_once:
                    run_unified_worker(settings, stop_event=worker_stop, queue_only=args.queue_only, once=True,
                                       delivery_lookback=args.lookback_days, pickup_lookback=min(7, args.lookback_days),
                                       allow_unmapped=args.allow_unmapped is not False, job_timeout=args.job_timeout)
                else:
                    run_central_worker(settings, stop_event=worker_stop, concurrency=4)
            finally:
                signal.signal(signal.SIGINT, previous_handler)
        elif args.inventory_worker or args.inventory_once:
            if args.orders_json or args.mapping_csv or args.dry_run:
                parser.error("Worker không nhận dữ liệu mẫu; dùng dry-run.")
            from sdd_data.delivery_worker import run_worker
            import signal
            inventory_stop = threading.Event()
            previous_handler = signal.signal(signal.SIGINT, lambda *_: inventory_stop.set())
            try:
                run_worker(settings, stop_event=inventory_stop, queue_only=args.queue_only, once=args.inventory_once,
                           lookback_days=args.lookback_days, allow_unmapped=args.allow_unmapped is not False,
                           job_timeout=args.job_timeout)
            finally:
                signal.signal(signal.SIGINT, previous_handler)
        elif args.pickup_worker or args.pickup_once:
            if args.orders_json or args.mapping_csv or args.dry_run:
                parser.error("Worker không nhận dữ liệu mẫu; dùng dry-run.")
            from sdd_data.pickup_worker import run_worker as run_pickup_worker
            import signal
            pickup_stop = threading.Event()
            previous_handler = signal.signal(signal.SIGINT, lambda *_: pickup_stop.set())
            try:
                run_pickup_worker(settings, stop_event=pickup_stop, queue_only=args.queue_only, once=args.pickup_once,
                                  lookback_days=args.lookback_days, allow_unmapped=args.allow_unmapped is not False,
                                  job_timeout=args.job_timeout)
            finally:
                signal.signal(signal.SIGINT, previous_handler)
        elif args.check or args.check_online:
            check(settings, online=args.check_online, report_date=args.date)
        elif args.job:
            run(settings, args.job, args.date, dry_run=args.dry_run, mapping_csv=args.mapping_csv,
                orders_json=args.orders_json, allow_unmapped=args.allow_unmapped is not False)
        else:
            if args.dry_run or args.orders_json or args.mapping_csv or args.allow_unmapped is not None:
                parser.error("Các tuỳ chọn chạy job yêu cầu --job pickup/delivery/performance/both/all.")
            open_gui(settings)
        return 0
    except Exception as exc:
        logging.error("Dừng: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
