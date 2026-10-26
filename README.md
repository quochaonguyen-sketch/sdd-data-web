# SDD Web Data

Project riêng phục vụ website `sdd-performance-project`, gồm **Volume Pickup**, **Volume Delivery** và **Performance rider**.

## Zone ID cũ và mới

- `config/mappings/coverage-old-new.csv` là bản danh mục người dùng cung cấp ngày 24/10/2026. Cột `Zone ID`, `Zone ID v2` và mã trong `Zone ID+Hub_Code_NDD` trên cùng một dòng cùng trỏ về địa bàn hiện tại. File được đọc bổ sung và ưu tiên hơn Coverage SA khi chạy Volume hoặc worker tồn Delivery; có thể đổi đường dẫn bằng `COVERAGE_ALIAS_FILE`.
- Ví dụ `Z5.01.NL.01` và `Z4.01.NL.01` cùng thuộc KV4 / Quận 3 / Phường 9 cũ. Mã Z6 được đưa về KV hiện tại theo địa bàn trong từng dòng, không thay tiền tố hàng loạt. Hỗ trợ mã có hậu tố Hub và ký hiệu danh mục `.#`.
- Mã trùng giữa hai phiên bản nhưng khác địa bàn phải đối chiếu phường/quận người nhận. Nếu vẫn còn nhiều địa bàn, tiếp tục ghi vào báo cáo chưa mapping. Không lấy địa chỉ người gửi để xác định Delivery.
- Delivery lưu thêm `data/delivery-unmapped-*.json` khi còn đơn chưa xác định địa bàn, gồm mã đơn, tuyến và thông tin phường/quận cần đối chiếu; không lưu địa chỉ đường nhà hay số điện thoại. Mở lại launcher sau khi cập nhật code để nạp mapping mới.

## Cookie SPX riêng và tự làm mới

- Mở `run.cmd`, bấm **Lưu tài khoản SPX** một lần. Username/password được Windows DPAPI mã hóa theo người dùng hiện tại; không lưu mật khẩu vào `.env` hay log. Có thể dùng **Đăng nhập SPX** để đăng nhập thủ công/OTP/CAPTCHA trong Chrome riêng.
- Cookie mặc định ở `config/cookies/spx.json`; profile và tài khoản ở `config/auth/`. Hai thư mục đều được gitignore. Không đọc lại cookie từ launcher khác khi chạy job.
- Pickup, Delivery, Performance và worker tồn Delivery tự làm mới khi cookie hết hạn hoặc SPX trả lỗi phiên. Chỉ thử lại API một lần; các request/tiến trình đồng thời dùng khóa chung để tránh đăng nhập hàng loạt. Lỗi làm mới có thời gian chờ 60 giây; nút đăng nhập thủ công vẫn hoạt động ngay.
- Chỉ ghi cookie sau khi API tracking xác nhận phiên hợp lệ; giữ file cũ nếu thất bại. OTP/CAPTCHA cần thao tác người dùng, không tự vượt qua.
- CLI: `run.cmd --refresh-cookie` làm mới nền; `run.cmd --login` mở Chrome đăng nhập; `run.cmd --migrate-cookie` chuyển cookie/tài khoản đã mã hóa cũ một lần nếu còn file. Không mở profile Chrome cá nhân của người dùng.
- Cần Google Chrome trên máy và Selenium theo phiên bản đã chốt trong `requirements.lock.txt`; Selenium Manager lấy ChromeDriver phù hợp lần đầu.

## Worker tồn Delivery qua Supabase

Mở lại `run.cmd`, bấm **Bật worker tồn Deli**. Worker nhận yêu cầu từ nút **Lấy dữ liệu mới** tại `/inventory-delivery` qua bảng `lmhub_fetch_jobs`. Có nút **Dừng worker**; khi đang fetch, worker hoàn tất job hiện tại trước khi dừng. Giữ launcher chạy để nhận việc.

- Bật sẵn tự fetch mỗi 30 phút từ 08:00 đến hết 23:59, giờ Việt Nam, giống launcher cũ. Tắt ô tự động để chỉ nhận yêu cầu từ web; yêu cầu web được xử lý cả ngoài khung giờ này.
- Có thể mở riêng `run_delivery_worker.cmd`, hoặc dùng các lệnh:

```powershell
.\run.cmd --inventory-worker
.\run.cmd --inventory-worker --queue-only
.\run.cmd --inventory-once
.\run.cmd --inventory-dry-run
.\run.cmd --inventory-worker --no-allow-unmapped
```

- Giống `lmhub_inventory_supabase_worker.py` / `order_tracking_lmhub_received.py`: API tracking, `order_status=1` (`LMHub_Received`), `current_station_ids=4232`, lọc `create_time` trong 14 ngày gần nhất; có thể đổi bằng `--lookback-days 1..90`. Không dùng `order_account=21,22` của Volume.
- Chỉ giữ đơn chưa gán rider (kiểm tra cả ID lẫn tên), chia COT MP trước 12:30 / từ 12:30 / từ 23:00 sang COT1 ngày sau; WH trước 12:00 / từ 12:00. Timestamp UTC+7, ngày báo cáo lấy từ ngày nhận thực tế.
- Mapping địa bàn người nhận theo Coverage SA và danh mục `ward_new_upload`; dùng 5 KV đã chốt cho website. Khi phường mới gồm nhiều phường cũ, chỉ gán khi các tuyến khớp đồng ý cùng quận và KV. Giữ tên phường mới nếu không xác định được một phường cũ duy nhất. Không dùng tuyến/người gửi để suy ra người nhận.
- Mặc định bỏ qua đơn chưa rõ địa bàn, ghi số lượng/mã tuyến trong log và báo cáo JSON. Dùng `--no-allow-unmapped` (hoặc bỏ chọn ô cho phép chưa rõ địa bàn trong GUI) để dừng toàn bộ khi thiếu mapping. Snapshot rỗng và lỗi nguồn luôn giữ nguyên dữ liệu web.
- Worker này **chỉ nhận job Delivery**. Job `pickup` hoặc `all` dành cho worker tương ứng, không bị nhận rồi báo hoàn tất nhầm. Không chạy song song worker launcher cũ trên cùng đích; worker mới có khoá theo máy, claim nguyên tử và heartbeat để chống nhiều worker mới nhận cùng job.
- Snapshot và trạng thái DONE được ghi trong cùng một transaction qua RPC: lỗi ở bất kỳ bước nào sẽ rollback, web không đọc thấy snapshot đang ghi dở. Chỉ thay tồn trong các KV cấu hình, không xoá dữ liệu ngoài phạm vi.
- Các RPC đã được cài cho database website hiện tại. Khi triển khai database khác, tạo hai bảng từ `sdd-performance-project/prisma/lmhub-inventory.sql` và `lmhub-fetch-jobs.sql`, sau đó chạy `lmhub-delivery-worker.sql` trong cùng thư mục. RPC dùng quyền invoker, chỉ `service_role` được gọi. Cookie SPX vẫn ở máy local.
- Worker không tự đăng ký khởi động cùng Windows. Sau khi tắt máy/launcher, cần mở lại. Phiên hết hạn được tự làm mới bằng tài khoản/profile riêng của SDD Web Data; khi cần OTP/CAPTCHA, dùng nút Đăng nhập SPX.

## Chạy ngay trên máy hiện tại

Mở `C:\code\sdd-web-data\run.cmd` để mở launcher. Chọn ngày `YYYY-MM-DD`, chọn Pickup/Delivery/Performance (có thể chọn nhiều job) và dùng:

- **Chạy thử — không ghi:** đọc SPX (Volume dùng thêm Coverage SA), tổng hợp và lưu JSON, không ghi/xoá web.
- **Đồng bộ lên web:** ghi dữ liệu vào các bảng của website tương ứng với job đã chọn.
- **Kiểm tra kết nối:** chỉ kiểm tra quyền đọc, không ghi/xoá.

`run.cmd` ưu tiên Python trong `.venv` riêng. Nếu chưa cài, nó có thể dùng Python đang có ở `..\spx_py_laucher\.venv`.

## Cài độc lập

Chạy `setup.cmd`. Yêu cầu Python 3.11+ có tkinter (bản Python Windows thông thường). Script tạo `.venv`, ưu tiên bộ phiên bản đã chốt trong `requirements.lock.txt` (các thư viện trực tiếp nằm trong `requirements.txt`) và tạo `.env` từ mẫu.

Project không import code launcher cũ. Selenium chỉ dùng để đăng nhập/làm mới cookie; các API vẫn dùng requests. Không cần gspread hay oauth2client. Có thể chuyển sang máy khác bằng cách sửa đường dẫn trong `.env`.

## Nguồn và đích

| Job | Cách chọn đơn SPX | Bảng website | Cấp dữ liệu |
| --- | --- | --- | --- |
| Pickup | `create_time`, `pickup_station_ids=4232` | `pickup_volume` | Ngày / COT / KV / quận / phường mới / tuyến |
| Delivery | `current_station_received_time`, `current_station_ids=4232` | `delivery_order` | Ngày / COT / KV / quận / phường cũ |
| Performance | Báo cáo rider, `frequency=1`, `start_date`, `function_type=0` và `2` | `driver_performance_daily` | Ngày / mã rider |

Website thực tế đọc `delivery_order`, **không phải** `delivery_volume` của API import cũ. Project không gọi API import delivery cũ, không ghi Supabase 1/2 của launcher2 và không ghi Google Sheet.

### Performance rider

- Giống `spx_py_laucher/jobs/volume_rider_performance.py`: POST `/api/driverservice/admin/performance/pickup/report/list`, lấy đủ trang cho Delivery (`function_type=0`) và Pickup (`function_type=2`), ghép theo `driver_id`.
- Khoá `performance_id` là `YYYY-MM-DD|driver_id`; giữ các chỉ số assigned/completed, tên rider, loại hợp đồng và tỷ lệ 0–1 như job gốc. Mẫu số 0 có tỷ lệ `null`.
- Đọc cookie riêng của SDD Web Data và ghi **Supabase của website**. Không cần Coverage SA/Google service account khi chỉ chạy Performance. API Performance giữ nguyên phạm vi báo cáo của phiên SPX như launcher2, không dùng tham số trạm của API volume; website ghép `riders` để lọc KV1–KV5.
- Thiếu trang, total thay đổi, trùng/thiếu mã rider hoặc thiếu/sai chỉ số sẽ dừng trước khi ghi. Snapshot rỗng giữ nguyên dữ liệu web. Kiểm tra đủ cột bảng trước khi upsert; chỉ xoá ID cũ của ngày đã chọn sau khi mọi batch ghi thành công. Không tự xoá lịch sử hơn 30 ngày.
- GUI có ô **Performance** (mặc định chưa chọn). `--job both` vẫn là hai job volume; `--job all` chạy cả ba. Đóng và mở lại launcher để thấy lựa chọn mới.
- Performance chạy riêng vẫn tự lấy đủ nguồn phân loại COT của Delivery và Pickup. Các job Volume lưu aggregate theo rider vào `data/performance-cot/YYYY-MM-DD-{delivery,pickup}.json` độc lập; không còn phụ thuộc vào việc chạy chung `all` để sinh file COT. Cache gắn project/trạm/ngày/nguồn, tối đa 10 phút cho ngày hiện tại, 1 giờ cho ngày cũ. Thiếu, lỗi định dạng hoặc quá hạn thì đọc lại nguồn; đọc lỗi dừng trước khi ghi Performance. Cache không lưu mã vận đơn hay thông tin địa chỉ khách hàng. Giữ nguyên quy tắc phân loại COT Performance hiện có.

Phạm vi mặc định là **KV1–KV5**, mã lưu là `KV1`...`KV5` để khớp filter của website. Phân vùng theo quận/phường do người dùng chốt, thay cho cột Zone cũ của Coverage SA:

- KV1: Bình Chánh chỉ gồm Bình Hưng và Phong Phú; Quận 10, 11, 5, 6, 8.
- KV2: Quận 4, 7, Bình Thạnh, Quận 2 và Quận 9 cũ.
- KV3: phần Bình Chánh còn lại, Bình Tân, Tân Phú.
- KV4: Quận 1, 3, Tân Bình, Phú Nhuận.
- KV5: Hóc Môn, Quận 12, Gò Vấp, Quận Thủ Đức cũ.

Với TP. Thủ Đức, dùng phường cũ để tách Quận 2/9 và Quận Thủ Đức cũ. Trường hợp không xác định được thì thống kê chưa mapping, không đoán KV từ mã tuyến. `stats.remapped_area` đếm số đơn đã đổi khỏi KV nguồn cũ. Station mặc định giữ `4232` theo launcher2; đổi `STATION_ID` nếu trạm nguồn khác.

### Giữ nguyên nghiệp vụ COT từ launcher2

- Múi giờ Việt Nam UTC+7, không phụ thuộc múi giờ Windows.
- Pickup: trước 11:15 là COT 1; 11:15 đến trước 15:30 là COT 2; từ 15:30 là **COT 1 của ngày kế tiếp**.
- `report_date` pickup vẫn là **ngày tạo đơn**, giống job gốc; ngày trong chuỗi COT có thể là ngày kế tiếp.
- Delivery: trước 08:00 là COT 1.1; 08:00 đến trước 13:00 là COT 1.2; từ 13:00 là COT 2.
- Loại đơn trùng theo `shipment_id`; hỗ trợ timestamp giây/millisecond.
- SPX thực tế có thể lặp đơn giữa các trang dù lọc giờ cũ. Bộ lấy dữ liệu probe một dòng để biết tổng số, chia khoảng dựa theo mật độ đơn và lấy các khoảng vừa một trang; giới hạn 8 request song song. Mỗi worker tái sử dụng kết nối riêng. Nếu có hơn 200 đơn trong cùng một giây thì fallback phân trang được kiểm tra nghiêm ngặt, thiếu/trùng thì dừng. Với ngày hôm nay, chốt mốc cuối ở thời điểm bắt đầu lấy dữ liệu, trừ 2 giây cho độ trễ. Chỉ giữ các trường cần tính volume trong bộ nhớ, bỏ thông tin người gửi/người nhận khỏi kết quả thu thập.
- Mapping lấy từ tab `Coverage SA`. Khớp Zone ID chính xác rồi tìm tuyến cha tại ranh giới `-`, không ghép đại tuyến chỉ vì giống ký tự đầu.
- Pickup phân theo **seller/người gửi**: ưu tiên `seller_zone_id`, `pickup_zone_id`, `sender_zone_id`; tuyệt đối không lấy `zone_id`, `receiver_zone_id`, `buyer_zone_id` làm tuyến seller. Khi SPX chỉ trả `lowest_seller_address_id`, tra tab `ward_new_upload`, đối chiếu phường mới với Coverage SA và chỉ nhận vị trí xác định duy nhất. Nếu đã xác định được địa bàn nhưng không có mã tuyến seller, vẫn tính volume và để mã tuyến trống (web hiển thị “Chưa có mã tuyến”). Phường sáp nhập thuộc nhiều quận mà không có thông tin phân biệt được giữ trong nhóm Chưa rõ địa bàn seller. Địa chỉ đường/số nhà không lưu vào báo cáo.
- **Mã tuyến pickup qua pickup point:** trước khi tổng hợp, đọc `/api/admin/pickup/order/detail/pickup_info` theo từng vận đơn, chọn pickup point ở sự kiện mới nhất, rồi tra danh mục `/api/admin/pickup/pickup_point_group/search` (`group_type=4`, đúng station). Mã nhóm thực được ghi vào `ma_tuyen` và là một phần khóa tổng hợp, nên nhiều tuyến cùng phường/COT được giữ riêng. Không dùng nhóm ảo `all`/`no group`; điểm thuộc nhiều tuyến hoặc sự kiện cùng thời điểm khác điểm được giữ chưa rõ. Tuyến đã xác định nhưng địa bàn chưa rõ vẫn xuất hiện trong nhóm chưa rõ địa bàn.
- Với hơn 200 vận đơn chưa có cache, đọc trước `/api/admin/pickup/fm_hub/pickup_order/search` theo ngày tạo, ghép **đúng pickup_order_id = shipment_id** và đúng station để lấy điểm theo lô. Thiếu/trùng điểm khác nhau thì gọi API chi tiết riêng. API này chỉ bổ sung điểm; tập vận đơn/sản lượng vẫn lấy từ tracking, không cộng thêm đơn hay suy diễn theo seller.
- Phường mới có thể gồm nhiều quận cũ. Nếu mọi dòng Coverage SA khớp phường đều thuộc cùng một KV, giữ KV đó và phường mới, chỉ để quận là “Chưa rõ quận cũ”. Chỉ đưa vào `UNRESOLVED` khi chưa xác định duy nhất được cả KV. Website cho chọn KV trước quận/phường/tuyến; số tổng và COT bám theo KV đang chọn.
- Quy ước người dùng xác nhận: tuyến pickup `BT_` thuộc quận Bình Tân cũ, `TP_` thuộc quận Tân Phú cũ (đều KV3); phường Bình Hưng Hòa thuộc quận Bình Tân cũ. Áp dụng sau mapping địa chỉ để xử lý phường liên quận. Khi thiếu phường, đối chiếu phần tên/viết tắt phường của tuyến với Coverage SA trong đúng quận, chỉ nhận phường mới khi kết quả duy nhất; không đoán phường nếu mã chưa có mapping. Các prefix khác chưa có quy tắc riêng vẫn dùng mapping nguồn.
- Mở rộng theo xác nhận người dùng: `BTH_` → Bình Thạnh / KV2; `Q1_`…`Q12_` → quận tương ứng, KV theo phân vùng website. Các viết tắt khác (GV/TB/PN/HM…) chỉ nhận khi khớp duy nhất tên quận/huyện trong Coverage SA; quy tắc BT/TP/BTH được ưu tiên để tránh nhập nhằng. Khi thiếu phường, `P1301` đối chiếu phường 13, `P03` đối chiếu phường 3, `PDHT01` đối chiếu chữ đầu của Đông Hưng Thuận; chỉ chuyển sang phường mới khi catalog trong đúng quận cho kết quả duy nhất.
- Cache `data/pickup-points-<station>-<ngày>.json` chỉ lưu vận đơn → ID điểm đã tra thành công; mỗi lần chạy đều lấy lại danh mục tuyến hiện tại. Điểm/tuyến là thông tin tại thời điểm đối chiếu, không phải lịch sử phân tuyến của ngày tạo đơn. Không có pickup point không được đoán từ điểm của đơn khác cùng seller. Cookie/API lỗi làm dừng trước khi đồng bộ, cache cho phép chạy lại tiếp phần còn thiếu. Dữ liệu offline `--orders-json --dry-run` không gọi bổ sung SPX.
- Coverage SA thật có một số Zone ID trùng. Mặc định lấy **dòng cuối**, đúng như launcher2, và ghi cảnh báo số tuyến trùng vào log. Hàm mapping cũng có chế độ `duplicate_policy="error"` để kiểm toán dữ liệu nguồn.

## Cấu hình, không sao chép secret

Khi hai project nằm cạnh nhau trong `C:\code`, mặc định dùng:

- Cookie: `config/cookies/spx.json` của chính SDD Web Data; tự làm mới khi hết hạn/bị SPX từ chối.
- Google service account: `..\spx_py_laucher\config\credentials\service_account_assign_pick.json` — chỉ đọc Coverage SA với scope spreadsheets.readonly.
- Supabase: chỉ lấy `NEXT_PUBLIC_SUPABASE_URL` và `SUPABASE_SERVICE_ROLE_KEY` từ `..\sdd-performance-project\.env`, sau đó `.env.local` nếu có.

Không đọc `config/app.env` chứa Supabase cũ. Cookie/profile và tài khoản Windows mã hóa nằm trong config/cookies, config/auth, đã được gitignore. Không đưa service-account key vào repo. Có thể đặt đường dẫn/đích riêng trong `.env`; các biến môi trường không rỗng được ưu tiên. `.env`, `logs`, `data` và `.venv` đã nằm trong `.gitignore`.

Nếu `.env` của website có URL Supabase, URL ghi dữ liệu bắt buộc phải khớp để không vô tình trỏ vào database cũ do một biến môi trường Windows.

Khi cookie hết hạn, project tự thử làm mới bằng Chrome riêng và tài khoản đã mã hóa, rồi gọi lại API một lần. Nếu vẫn bị từ chối hoặc cần OTP/CAPTCHA, bấm **Đăng nhập SPX** trong SDD Web Data. Không diễn giải lỗi thành “0 đơn”.

## Lệnh PowerShell

```powershell
cd C:\code\sdd-web-data
.\run.cmd --check
.\run.cmd --check-online
.\run.cmd --job both --date 2026-10-23 --dry-run
.\run.cmd --job pickup --date 2026-10-23
.\run.cmd --job delivery --date 2026-10-23
.\run.cmd --job both --date 2026-10-23
.\run.cmd --job performance --date 2026-10-23 --dry-run
.\run.cmd --job performance --date 2026-10-23
.\run.cmd --job all --date 2026-10-23
```

Mỗi lần chạy ghi log riêng trong `logs/`, báo cáo tổng hợp trong `data/`. Báo cáo không chứa cookie/secret hay toàn bộ dữ liệu cá nhân của đơn.

### Chạy demo hoàn toàn offline

```powershell
.\run.cmd --job both --date 2026-10-23 --dry-run --mapping-csv examples/coverage.csv --orders-json examples/orders.json
.\run.cmd --job performance --date 2026-10-23 --dry-run --orders-json examples/performance.json
```

Dữ liệu `--orders-json` bắt buộc có `--dry-run`, không được dùng để ghi lên website.

## Cách thay snapshot theo ngày

Mỗi job có khoá chống chạy trùng theo ngày. Với hai job Volume, sau khi xác minh đủ trang SPX và mapping, pipeline đọc các ID cũ trong phạm vi ngày + KV (Pickup gồm cả nhóm UNRESOLVED), upsert toàn bộ nhóm mới theo batch, **chỉ sau khi mọi batch thành công** mới xoá những ID cũ không còn trong snapshot. Không xoá ngày/KV khác. Không ghi/xoá nếu kết quả rỗng hoặc có thời gian/ID lỗi. Pickup chưa rõ địa bàn vẫn tính vào tổng và ghi riêng; Delivery chưa có mapping tiếp tục bỏ qua theo lựa chọn của người dùng.

Pickup chưa xác định được địa bàn được giữ trong nhóm **Chưa rõ địa bàn seller** với mã khu vực riêng `UNRESOLVED`, không gán vào KV1–KV5. Những đơn này vẫn được tính vào tổng Pickup trên trang Volume. Mỗi lượt chạy lưu danh sách mã đơn, ID địa chỉ seller, phường đã biết và lý do trong `data/pickup-unresolved-<ngày>-<thời điểm>.json` để đối chiếu/fetch lại. Chạy lại Pickup cùng ngày sau khi có thông tin mới sẽ tự thay snapshot, xoá nhóm Chưa rõ cũ nếu đã xử lý hết. Không lưu địa chỉ đường/số nhà, số điện thoại.

Trong GUI, lựa chọn cho phép đơn chưa rõ địa bàn bật sẵn: Pickup giữ nhóm Chưa rõ, Delivery vẫn bỏ qua như trước. Đóng và mở lại launcher để nhận mặc định mới. CLI dùng `--no-allow-unmapped` để yêu cầu dừng khi thiếu mapping.
Mục `stats.unmapped_zones` trong JSON liệt kê Zone ID chưa có mapping và số đơn tương ứng để bổ sung Coverage SA.

Đây là quy trình REST nhiều batch, **không phải transaction nguyên khối**: nếu mất mạng giữa các batch, có thể đã cập nhật một phần nhóm; chạy lại cùng ngày để hoàn tất. Dữ liệu cũ không bị xoá khi batch ghi lỗi. Không chạy một tool khác ghi cùng bảng/ngày trong lúc job đang chạy.

## Kiểm thử

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Test bao gồm ranh giới COT, ngày UTC+7, loại trùng, phân vùng quận/phường theo 5 KV (kể cả KV6 nguồn cũ được chuyển về KV5), sai mapping, mất trang API, đúng bảng delivery_order, dry-run không ghi DB, cookie hết hạn và không xoá snapshot cũ khi batch ghi thất bại.

## Nguồn tham khảo khi tách

Đã đối chiếu `laucher2.py`, `jobs/volume_pickup.py`, `jobs/volume_delivery.py`, `jobs/volume_rider_performance.py`, `core/cookie_login.py` và `paths.py` của project cũ; đối chiếu các màn hình volume/dashboard và truy vấn Performance của website. Mã mới dùng các client độc lập, không import hoặc chạy launcher cũ.


## Pickup Realtime KV1–KV5

`run_pickup_realtime.cmd` chạy đồng bộ sản lượng pickup từ task SPX và đơn pickup đang chờ/Onhold. Dùng `--once` để chạy một lượt; mặc định chạy 08:00–23:59 và nghỉ 300 giây sau mỗi lượt. Worker tổng hợp (`run_worker.cmd`) cũng khởi động luồng này khi mở lại chương trình; khóa tiến trình ngăn hai publisher chạy trùng.

Dùng cookie và cơ chế làm mới cookie của chính sdd-web-data. Không import/chạy launcher cũ. Cài bảng riêng bằng `../sdd-performance-project/prisma/pickup-realtime-current.sql`. Snapshot JSON được ghi nguyên khối vào bảng service-only `pickup_realtime_current`; job KV5 cũ không ghi đè bảng này. Website tự lấy snapshot mới qua API có kiểm tra quyền.

Sản lượng lấy = tổng `pickup_quantity` của task trong ngày UTC+7. Chờ lấy/Onhold đọc trực tiếp từ SPX status 0/2, gồm `pickup_attempts` và các cờ Onhold. Gán rider qua task của pickup point; đơn mới COT2 sau 12:00 ưu tiên rider task COT2 giống quy tắc hiện hành. KV/COT hiển thị theo hồ sơ web. Hồ sơ không có hoạt động sau khi kiểm đủ nguồn có số 0; nguồn lỗi/thiếu/trùng trang không thay snapshot cuối. Bảng tồn chỉ cung cấp thông tin địa bàn, không dùng các số tồn cũ làm sản lượng realtime.

Kiểm tra: `python -m unittest tests.test_pickup_realtime -v`. Nhật ký ở `logs/`, báo cáo từng lượt ở `data/pickup-realtime-*.json`.
#   s d d - d a t a - w e b  
 