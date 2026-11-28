# Tác vụ snapshot SDD

Chạy các tác vụ trên một máy Windows vận hành worker có Python trong `.venv`, phiên SPX còn hiệu lực, Node.js và quyền ghi Cloudflare D1. Các tác vụ dùng phiên SPX của worker và tài khoản Wrangler trên máy. Cài Node.js từ phiên bản 22 trở lên.

```powershell
npm.cmd ci --prefix snapshot_jobs
powershell.exe -NoProfile -ExecutionPolicy Bypass -File snapshot_jobs/register.ps1
```

| Giờ Việt Nam | Tác vụ |
| --- | --- |
| 00:15 | Báo cáo mã đơn Pickup của ngày trước. File local ở `data/pickup-daily/` và bản lưu web ở Cloudflare D1 `sdd-pickup-daily`. |
| 13:00 và 17:00 | Pickup On Hold COT1 và COT2. File local ở `data/pickup-onhold/`. Outbox chuyển các bản đã chốt sang D1 `sdd-backlog-history`. |
| 17:45 | Cân nặng Delivery từ hai bản chốt 13:30 và 17:30, lưu ở D1 `sdd-backlog-history`. |
| Mỗi 10 phút, 06:00–22:59 | Bổ sung cân nặng đơn tồn Delivery hiện tại từ API danh sách SPX. |

Chạy `register.ps1` dưới tài khoản Windows vận hành tác vụ. Lịch dùng phiên Windows đang đăng nhập. Chỉ đăng ký trên một máy để tránh hai máy cùng tạo bản chốt. Repo chứa mã và lịch. Cookie, file báo cáo và cấu hình đăng nhập nằm ngoài Git.
