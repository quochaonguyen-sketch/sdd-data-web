# Worker nguồn dữ liệu

## Khung giờ và số luồng

Lịch theo chu kỳ có giờ **Bắt đầu/Kết thúc** riêng từng nguồn trên web, theo giờ Việt Nam. Bắt đầu được tính vào khung; kết thúc không nhận lượt mới. Khung qua đêm được hỗ trợ; hai giờ bằng nhau là cả ngày. Tác vụ đang chạy được hoàn tất, tác vụ tự động còn xếp hàng khi hết khung bị hủy và chờ khung sau. Chạy một lần luôn có thể chạy ngoài khung. Lịch giờ cố định mỗi ngày dùng trực tiếp giờ đã chọn, không áp dụng khung của chế độ chu kỳ.

Cấu hình theo yêu cầu hiện tại: Pickup Realtime 07:00–17:00, giữ chu kỳ 15 phút; OH Delivery 07:00–23:00, giữ chu kỳ 5 phút. Các nguồn còn lại giữ lịch hiện có. Lượt tiếp theo ngoài khung được đưa về giờ mở khung tiếp theo. Chốt ngày OH lúc nửa đêm hoạt động riêng với khung thu thập.

Worker trung tâm mặc định chạy 4 nguồn song song. FETCH_WORKERS hiện là 16; tiến trình con tôn trọng cấu hình tối đa 32 luồng fetch và 24 luồng inventory. Giữ cơ chế retry/backoff cho HTTP 429/5xx. Với sản lượng Pickup, chỉ tra các khung giờ đã tới; index pickup point không có list với total=0 là rỗng hợp lệ. Index thiếu dữ liệu được giữ các kết quả hợp lệ và tra chi tiết mã còn thiếu. Tập mã tracking vẫn là nguồn quyết định sản lượng; lỗi xác thực và dữ liệu chi tiết sai mã vẫn chặn ghi.

Mở `run_worker.cmd` hoặc `run_sources_worker.cmd` trên máy chủ: worker chạy nền, cửa sổ CMD tự đóng khi bật thành công. Bấm lại khi đã chạy sẽ không tạo thêm bản trùng. Có thể bật **Worker trung tâm** trong launcher; cách này worker thuộc tiến trình launcher. `launcher.py --worker` chạy trong cửa sổ hiện tại, hỗ trợ Ctrl+C. `--worker-once` giữ chế độ kiểm tra tồn cũ.

Để tắt hẳn worker nền, mở `stop_worker.cmd` (hoặc `python -m sdd_data.source_worker --stop`). Worker kiểm tra yêu cầu mỗi vòng lặp, ngừng nhận tác vụ, yêu cầu dừng phần đang đọc và hoàn tất phần đang ghi rồi thoát. Nếu đang chạy trong cửa sổ CMD, có thể nhấn Ctrl+C một lần. Worker không tự bật lại; muốn chạy tiếp phải mở `run_worker.cmd`. Thiết lập lịch tự động trên web được giữ lại cho lần bật sau.

Trên web mở **Excel & dữ liệu → Nguồn dữ liệu** (`/data-sources`). Admin/Leader có quyền nguồn tương ứng được chạy một lần, chọn ngày cho sản lượng/Performance và cấu hình lịch tự động. Lịch có hai kiểu: **Theo chu kỳ** (khung giờ riêng do người dùng chọn) hoặc **Giờ cố định mỗi ngày** (00:00–23:59, múi giờ Asia/Ho_Chi_Minh/UTC+7, kể cả trước 08:00). Sản lượng/Performance được chọn dữ liệu **ngày hiện tại** hoặc **ngày hôm trước**; ô ngày trên thẻ chỉ dùng cho Chạy một lần. Chỉnh xong bấm **Lưu lịch**; thay đổi chưa lưu được đánh dấu trên thẻ.

Lượt tiếp theo tính trên máy chủ, không theo múi giờ trình duyệt. Bật lịch sau giờ đã chọn sẽ bắt đầu từ ngày kế tiếp; muốn chạy ngay dùng Chạy một lần. Hoàn tất tác vụ không làm lệch giờ cố định. Nếu nguồn đang bận, lượt đến hạn chờ nguồn rảnh; không bị mất vì có tác vụ thủ công. Nếu worker ngoại tuyến, khi lên lại chỉ chạy bù một lượt gần nhất, không dồn nhiều ngày; ngày dữ liệu theo ngày của lượt được bù. Worker xếp lịch ngay cả khi các slot thực thi đang bận. Dừng tác vụ không tắt lịch tự động; tắt lịch cũng không hủy tác vụ đã xếp/chạy. Máy chủ phải có worker online để nhận và thực hiện lịch.

Bảy nguồn: Tồn Pickup, Tồn Delivery, Pickup Realtime, sản lượng Pickup, sản lượng Delivery, Performance và Hàng trả. Mặc định tối đa 4 tiến trình con, cấu hình `python -m sdd_data.source_worker --concurrency 1..4`. Mỗi nguồn chỉ có một lượt đang chờ/chạy trong DB, kể cả nhiều worker. Tồn Pickup chỉ ghi Assigned. Lượt ghi Pickup thay snapshot và xác nhận hàng đợi trong cùng giao dịch. Các adapter còn lại dùng cơ chế ghi hiện có của nguồn đó.

Hàng trả lấy đủ đơn tại hub cấu hình, trạng thái 67 (Return FMHub received), 10 (Return LMHub received), 72 (Return FMHub returning). Chia KV1–KV5 theo địa bàn hiện tại của web; không giới hạn KV5–KV6 như luồng cũ. Lấy mã tuyến qua pickup point, đối chiếu rider active/COT theo tuyến, giữ riêng driver đang đi trả. Đơn chưa xác định địa bàn được giữ trong nhóm Chưa rõ KV; không dùng zone người nhận để đoán địa bàn seller. Phân trang được kiểm tổng/ID trùng và kiểm lại tối đa một lần nếu nguồn thay đổi; nếu chưa đủ thì giữ snapshot cũ. RPC ghi dữ liệu thay toàn bộ snapshot trong một giao dịch và giữ nguyên các bảng gán/bàn giao thủ công. Chạy lần đầu từ thẻ Hàng trả trên /data-sources; lịch tự động ban đầu tắt, bật trên web nếu muốn tự cập nhật. Khi nâng cấp worker đang chạy từ phiên bản chỉ có 6 nguồn, chờ tác vụ xong rồi khởi động lại worker để nạp nguồn mới.

Tối ưu Tồn Pickup: lấy danh sách đơn, task rider và danh mục tuyến song song; vẫn kiểm đủ trang và kiểm cờ Onhold/Picked. Tiến trình con dùng FETCH_WORKERS/INVENTORY_WORKERS trong cấu hình, giới hạn tối đa 32 luồng fetch và 24 luồng inventory. Bước ghi Pickup dùng upsert theo tracking_id rồi xóa dòng thuộc snapshot cũ trong cùng giao dịch; không xóa toàn bộ trước khi chèn. Riêng RPC ghi Pickup có ngân sách 120 giây (HTTP worker 150 giây) để tránh timeout API mặc định khi có hàng nghìn đơn; giới hạn các truy vấn khác được giữ nguyên.

Worker nhận lệnh từ Supabase bằng service key phía máy chủ; trình duyệt không nhận key/cookie và không gửi lệnh shell. Cookie lấy từ cấu hình riêng `sdd-web-data/config/cookies/spx.json`, tiếp tục dùng cơ chế làm mới cookie đã có. Cookie hết hạn vẫn có thể cần đăng nhập SPX lại.

Nhật ký web chỉ có sự kiện điều khiển. Chi tiết lỗi kỹ thuật ở `logs/source-<job UUID>.log` trên máy chủ. Yêu cầu dừng được kiểm tra trước các lượt gọi API; khi đã bắt đầu ghi thì hoàn tất lượt ghi để tránh ngắt giữa các batch. Tác vụ đọc quá 30 phút được yêu cầu dừng, quá thêm 90 giây có thể kết thúc tiến trình con; tác vụ đang ghi không bị cưỡng bức tắt.

Đóng web không dừng worker. Máy chủ cần luôn bật worker để nhận lệnh; sau khởi động lại máy chạy lại CMD (hoặc cấu hình Windows Task Scheduler tại máy chủ). Worker không tự cài dịch vụ khởi động cùng Windows. Không chạy các worker tồn/realtime cũ song song với worker trung tâm; khóa nguồn sẽ báo lỗi nếu chương trình cũ đang giữ nguồn. Nút Fetch cũ trên trang tồn vẫn được nối vào hàng đợi mới.

Tiến trình con cũng giữ heartbeat và xác nhận kết quả riêng: nếu launcher trung tâm bị khởi động lại, tác vụ đang chạy vẫn có thể hoàn tất. Nếu cả tác vụ mất heartbeat, lượt chạy hết lease được đánh dấu thất bại, không tự phát lại tác vụ đó. Kiểm tra tiến trình trên máy chủ trước khi bấm chạy lại. Các lịch tự động vẫn hoạt động theo thiết lập riêng; tắt lịch khi bảo trì. Nút Fetch cũ cho cả Pickup + Delivery được tách thành hai nguồn, chỉ báo hoàn tất khi cả hai thành công.

Web cần áp dụng `prisma/data-source-worker.sql`, `prisma/return-order-snapshots.sql` (nếu chưa có bảng hàng trả), rồi `prisma/return-source-worker.sql` bằng tài khoản DB quản trị trước khi chạy. Nâng cấp từ worker cũ: đợi hàng đợi tồn hết RUNNING, dừng đúng worker tồn và realtime cũ, sau đó bật worker trung tâm. Không tắt chương trình GUI/người dùng đang thao tác.


## OH Delivery — lũy kế mã duy nhất trong ngày
Nguồn `onhold_daily` → `/onhold`, quyền Performance. Cài theo thứ tự: `prisma/data-source-worker.sql`, `prisma/onhold-daily.sql`, `prisma/onhold-cumulative.sql`, `prisma/onhold-close-cron.sql`. Khi nâng cấp chạy lại source-control SQL rồi cumulative SQL (snapshot API cũ bị khóa). Khởi động lại worker lúc rảnh để nạp tác vụ chốt ngày dự phòng.

Bộ lọc giống launcher: delivering_time trong ngày Việt Nam, order_status 5/1, on_hold_times 1–10. Cùng quy tắc hub 4232 và trạm khác có received_time sau OH trong ngày; Coverage SA quy đổi KV1–KV5 hiện tại. Chia các khung giờ không chồng nhau đến khi mỗi khung vừa một response để tránh offset pagination khi trạng thái thay đổi.

Worker quét mỗi 5 phút trong khung giờ đã cấu hình cho nguồn OH; có thể đặt cả ngày bằng hai mốc giờ bằng nhau. Không cho backfill vì trạng thái hiện tại không tái dựng được tập mã từng OH trước đây. Mỗi mã hợp lệ tạo SHA-256 của (phiên bản, ngày, mã viết hoa). Chỉ gửi digest cùng các chiều pivot; không gửi mã vận đơn hay payload raw lên web, không tạo file raw. `onhold_daily_seen` chỉ giữ ngày + digest, không chứa địa chỉ, rider hoặc raw đơn. Database insert keys ON CONFLICT DO NOTHING và tăng các ô pivot chỉ cho keys mới trong một transaction có khóa nguồn. Retry, chạy lại, restart và đơn biến mất khỏi SPX không làm mất hoặc cộng lại mã đã ghi nhận.

Địa bàn, rider, lý do, giờ OH và trạng thái lấy tại lần đầu ghi nhận. Lý do dùng trường SPX `on_hold_reason__desc` nếu có, hoặc nhãn tiếng Anh nguyên bản từ launcher; mã chưa có nhãn để Unknown (code), không tự dịch. Pivot lưu quận, phường, mã/tên rider để xếp hạng trong khu vực. Rider không có dữ liệu giữ nhóm chưa rõ, không suy đoán. Đây là số mã OH, không phải tỷ lệ OH trên sản lượng rider.

Tác vụ database `sdd-onhold-close-day` chạy 17:00 GMT (00:00 Việt Nam), đánh dấu chốt ngày cũ và xóa keys của ngày cũ, giữ nguyên pivot. Vẫn chốt khi PC worker tắt; worker cũng gọi finalize idempotent để bắt kịp nếu cron chậm. Không thể ghi lại ngày đã chốt hoặc ngày trước. Sau chốt lịch sử chỉ có các ô tổng hợp và metadata. Snapshot cũ được giữ ở archive aggregate, không cộng vào lũy kế do không có tập keys để khử trùng.

Trang hiển thị thời điểm bắt đầu thu thập, số lần quét, số mã mới của lượt gần nhất và trạng thái chốt. Không thể bảo đảm bắt được mã xuất hiện rồi biến mất hoàn toàn giữa hai lần quét hoặc trước lúc bắt đầu chạy; cần worker online và phiên SPX hợp lệ. Lịch sử bắt đầu từ lúc bật cơ chế thu thập. Bảng phường có thể chuyển sang quận; xếp hạng rider và lý do đều theo KV/ngày/bộ lọc trạng thái lúc ghi nhận.

RPC control dùng JSON UTF-8 gọn, không mở rộng tên phường/địa chỉ thành Unicode escape. Khi triển khai bản sửa timeout, chỉ alter `publish_central_pickup` lên 120s và reload schema; không đổi các API khác. Tiến trình con source_job nạp code mới ở mỗi job nên không cần dừng worker đang chạy.


## Đồng bộ trạng thái không ghi trùng
Giữ hàng qua đêm: SPX chỉ kiểm tra các đơn chưa Delivered/Returned trong danh sách đã chốt. Chỉ cập nhật dòng đơn khi trạng thái thực sự đổi; kiểm tra không đổi vẫn cập nhật thời gian kiểm tra chung của ngày. Không thêm đơn vào danh sách đã chốt 23h. Nâng cấp bằng `node --env-file=.env --env-file=.env.local scripts/apply-overnight-status-sync.cjs` trong project web.

Hàng trả: vẫn đọc đủ nguồn hiện tại để nhận trạng thái/tài xế mới và đơn đã rời tồn. Unique index theo shipment_id cùng upsert giữ nguyên khóa dòng đã có, chỉ chèn mã mới. Snapshot metadata vẫn đổi cho mọi dòng còn trong nguồn để tương thích bộ đọc snapshot hiện tại; loại đơn rời nguồn sau khi xác nhận đủ nguồn, trong cùng giao dịch. Tái sử dụng pickup_point_id trong Supabase và cache cục bộ; chỉ gọi pickup_info cho mã chưa có thông tin điểm. Áp dụng bằng `node --env-file=.env --env-file=.env.local scripts/apply-return-source-worker.cjs` trong project web, thêm `--test` để kiểm thử có rollback. Code enrichment Hàng trả được nạp ở tác vụ kế tiếp; nhánh tránh mở SPX khi danh sách kiểm tra rỗng được nạp khi worker trung tâm khởi động lại.


Overnight nhiều ngày: lần quét chốt 23h đọc Delivering/Onhold tại hub qua toàn bộ khoảng delivering_time từ trước đến thời điểm quét, bao gồm đơn phát các ngày trước còn trên rider; không giới hạn riêng ngày chốt. OH lũy kế ban ngày vẫn chỉ đếm ngày hiện tại. Cài `scripts/apply-overnight-history.cjs` trong project web để nâng cấp riêng publisher, tránh ghi đè RPC đồng bộ SPX. Snapshot đã khóa giữ nguyên, không tái dựng lịch sử bằng trạng thái hiện tại. Worker source_job nạp logic mới ở lượt chạy kế tiếp.


Bổ sung tức thời theo yêu cầu vận hành: `scripts/supplement-overnight-history.cjs` tại project web quét SPX mới chỉ đến ngày chốt đang hiển thị, thêm các mã cũ còn trên rider vào snapshot đã khóa gần nhất. Dùng `--test` để chạy đầy đủ trong giao dịch rollback trước khi áp dụng. Mã trùng bỏ qua hoàn toàn; ghi chú, trạng thái và thời gian chốt ban đầu không đổi. Dòng mới có added_after_cutoff_at; snapshot có supplemented_at/supplemented_count để web và CSV phân biệt trạng thái ghi nhận sau chốt với trạng thái 23h. Đây là thao tác bổ sung được người dùng yêu cầu riêng, không thay đổi quy tắc đóng băng tự động lúc 23h. Không lưu file raw nguồn.
