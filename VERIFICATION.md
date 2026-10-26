# Kiểm tra SDD Web Data

## Lịch sử pivot Backlog — 26/10/2026

- Bổ sung `district` cho bản ghi Pickup từ Coverage SA, tuyến khớp chính xác hoặc tiền tố đã được xác nhận trong transform dùng chung. Phường nhập nhằng không bị đoán quận. Cache bản ghi đổi phiên bản để xây lại trường này.
- Delivery đánh dấu `source_complete` chỉ sau khi đọc/kiểm đủ các khoảng SPX; snapshot rỗng chỉ gửi `p_complete=true` nếu nguồn đầy đủ và không có unmapped. Hàm legacy 6 tham số giữ nguyên chặn empty; overload 7 tham số công bố zero đã xác nhận và kích hoạt lưu lịch sử trong cùng transaction. Cả adapter trung tâm và worker Delivery riêng đều dùng quy tắc này.
- Website cài `prisma/backlog-history.sql`: pivot bất biến sau mỗi publication DONE; lịch database 16:00/16:30 UTC+7. Không cần khởi động lại worker trung tâm: mỗi job chạy trong Python child mới. Worker phải online tại mốc; mốc bị lỡ không được giả lập dữ liệu quá khứ.
- Hai job thật đã SUCCEEDED, lưu được Pickup/Delivery pivot với tổng khớp snapshot. SQL integration rollback xác nhận zero, unique snapshot, UTC+7, scheduler, RLS/ACL. Kiểm thử Python gồm quận theo tuyến, phường nhập nhằng và empty source đã xác nhận.

## Tồn Pickup và Pickup Realtime: đọc song song có giới hạn — 26/10/2026

- Thêm `pickup_fetch.py`: đọc nhiều khoảng thời gian song song, dùng chung page executor để tái sử dụng kết nối; semaphore toàn client giới hạn 12 request đang chạy, không nhân số request theo số tầng thread pool.
- SPX đã được kiểm tra trực tiếp: cả `fm_hub/pickup_order/search` và `fm_hub/pickup_task/search` trả đủ 500 bản ghi với `count=500`. Bộ đọc chung dùng 500; chia nhỏ khoảng ngay khi vượt 500 để tránh tải nhiều trang có thứ tự thay đổi rồi tải lại do trùng/thiếu mã. Cache gộp các khoảng nhỏ khi tổng hiện tại vừa một trang; mỗi khoảng vẫn đọc trạng thái mới.
- Pickup Realtime đọc task/đơn/tuyến/reference song song. Cache địa bàn theo project, station và snapshot tồn; luôn đọc head trước/sau. Snapshot đổi thì tải lại; không cache sản lượng task hoặc trạng thái đơn. Hỗ trợ `--once --dry-run`, không ghi database.
- Benchmark đọc thật và xử lý (chưa tính publish DB), hai lượt mỗi nguồn: Tồn Pickup 16.896 / 15.084 ms; Pickup Realtime 16.316 / 15.821 ms khi cache địa bàn khớp snapshot. Tồn Pickup trước đó 94.212 ms; số dòng nguồn có thay đổi nên đây là quan sát vận hành, không phải A/B cùng snapshot. Chi tiết `data/pickup-parallel-500-live-benchmark.json`.
- 172 kiểm thử chạy thành công (171 pass, 1 skipped). Kiểm tra overlap bằng barrier, ngân sách HTTP, output theo thứ tự, chia trước phân trang, snapshot/project invalidation, snapshot đổi giữa lượt và dry-run/failure không publish.

## Cache Tồn Pickup — 26/10/2026

- `pickup_inventory` dùng trang 200 đơn/task; đã kiểm tra trực tiếp cả hai endpoint SPX trả `count=200` và đủ 200 dòng. Các client Realtime khác vẫn mặc định 100.
- Cache khoảng thời gian phân trang theo station giúp lượt sau không phải chia lại toàn bộ lịch sử. Mỗi khoảng, kể cả khoảng trước đó trống, vẫn đọc trạng thái mới để không bỏ sót đơn cũ vừa chuyển Assigned. Thêm khoảng đuôi cho đơn mới; cache lỗi/hở khoảng/sai station tự quay về đọc đầy đủ.
- Cache bản ghi chuẩn hóa theo mã đơn và fingerprint dữ liệu nguồn, chi tiết điểm, tuyến, rider, Coverage và phạm vi KV. Giữ tối đa 50.000 bản ghi và chỉ giữ đơn còn trong lượt đọc mới. Đơn chuyển Onhold/Picked/biến mất không được khôi phục từ cache; snapshot luôn dùng thời điểm hiện tại. Ghi file nguyên tử, lỗi cache không làm hỏng lượt đồng bộ.
- Chưa có endpoint delta SPX đã xác minh, vì vậy vẫn đọc danh sách trạng thái mới; không dùng số lượng bằng nhau để suy ra danh sách đơn không đổi.
- Benchmark replay 28.852 đơn, 3 lần, kết quả trước/sau giống nhau: trung vị xử lý cũ 2.394,20 ms; cache 1.375,43 ms (bao gồm đọc/ghi cache), giảm khoảng 42,6%. Đây là phần xử lý cục bộ, chưa phải thời gian đồng bộ SPX/Supabase toàn bộ.
- Bổ sung thống kê `orders_cached`, `orders_rebuilt`, `orders_ms`, `tasks_ms`, `routes_ms`, `point_details_ms`, `transform_ms` trong artifact để theo dõi lượt thực tế.
- Hai lượt đọc SPX thật, không ghi database: tạo cache 173.598 ms (30.013 dòng nguồn, 26.524 đơn tồn); dùng cache 94.212 ms (29.258 dòng nguồn, 25.886 đơn tồn). Lượt sau dùng lại 25.831 bản ghi, dựng lại 55; xử lý 937 ms; đọc đơn 90.984 ms; task 21.156 ms; tuyến 5.156 ms; điểm 31 ms. Các bước đọc chạy song song, không cộng trực tiếp. Dữ liệu nguồn thay đổi giữa hai lượt nên mức giảm khoảng 45,7% là quan sát thực tế, không phải benchmark đối chứng cùng snapshot. Cả hai lượt xác nhận `source_complete=true`.
- Toàn bộ 166 kiểm thử Python đạt (1 skipped); có kiểm tra giảm số request mà giữ đủ ID, khoảng từng trống vẫn đọc lại, cache bị hỏng, thay Coverage/rider/tuyến, đơn chuyển Onhold/Picked và đơn biến mất. Báo cáo đọc thật tại `data/pickup-optimized-live-benchmark.json` và `data/pickup-optimized-warm-live-benchmark.json`.

## Cookie SPX riêng, tự làm mới — 24/10/2026

- Cookie mặc định chuyển về `config/cookies/spx.json`; profile và tài khoản DPAPI nằm trong `config/auth/`. Đã chuyển file cookie cũ một lần và đặt đường dẫn local trong `.env`, không dùng cookie của project khác khi chạy job.
- Các SPX client dùng session chung cơ chế auth: phát hiện expiry/401/403/login JSON/HTML, làm mới một lần rồi retry; khóa đa luồng/đa tiến trình và cooldown chống đăng nhập lặp. Chỉ thay file sau khi API kiểm tra cookie thành công.
- Launcher có Lưu tài khoản SPX, Đăng nhập SPX, Làm mới cookie; CLI tương ứng. Selenium 4.51.0 và các phụ thuộc đã cài/chốt phiên bản, `pip check` đạt.
- 87 kiểm thử đạt, gồm DPAPI roundtrip trên Windows, retry một lần, CSRF mới, đồng thời chỉ một browser, thất bại giữ cookie cũ, không gửi cookie sang host khác. Chrome profile riêng đã khởi động và tới host đăng nhập SPX, có trường password.
- Chưa kiểm tra đăng nhập thật/tự refresh end-to-end vì chưa có tài khoản lưu trong project (không có tài khoản DPAPI cũ để chuyển). Đã mở launcher mới để người dùng lưu tài khoản hoặc đăng nhập trực tiếp một lần; OTP/CAPTCHA vẫn do người dùng xử lý.

## Mở rộng mã quận/tuyến — 24/10/2026

- Theo xác nhận tiếp theo: BTH = Bình Thạnh, Q1–Q12 = các quận số. Các viết tắt còn lại dùng tên địa bàn khớp duy nhất trong catalog. Không suy diễn các prefix nhập nhằng/ngoài danh mục.
- Bổ sung đối chiếu phường cũ có số trong tên tuyến (`P1301` → phường 13, `P03` → phường 3) và dạng viết tắt có tiền tố P (`PDHT01` → Đông Hưng Thuận), rồi tra phường mới trong đúng quận.
- 78 kiểm thử Python đạt. Dữ liệu ngày 23/10 xác định KV cho 6 đơn còn lại; tổng 51.015 đơn và phân bổ từng tuyến/COT được bảo toàn. Báo cáo cuối tại `data/pickup-confirmed-prefix-verification-2026-10-23.json`.

## Quy ước tuyến pickup người dùng xác nhận — 24/10/2026

- Bổ sung `BT_` → quận Bình Tân cũ / KV3, `TP_` → quận Tân Phú cũ / KV3; phường Bình Hưng Hòa → quận Bình Tân cũ. Có hiệu lực trong transform cho các lần fetch sau.
- Khi thiếu phường, `BT_BHHA1_SDD`/`BT_BHHA1_SDD-1` đối chiếu tên viết tắt phường cũ trong Coverage SA để đưa về phường mới Bình Hưng Hòa; `TP_Hoa Thanh02_SDD` tra phường Hòa Thạnh cũ trong quận Tân Phú. Chỉ điền phường mới khi khớp duy nhất.
- 77 kiểm thử Python đạt. Đợt sửa dữ liệu hiện có lưu bản sao và báo cáo tại `data/pickup-before-confirmed-prefix-2026-10-23.json` và `data/pickup-confirmed-prefix-verification-2026-10-23.json`; giữ nguyên tổng đơn theo mỗi tuyến/COT.

## Scope Pickup: KV trước quận/phường — 24/10/2026

- Theo yêu cầu sửa bằng dữ liệu hiện có, dùng Coverage SA để xác định KV cho phường mới liên quận, không gọi SPX khi cookie đã hết hạn và không suy đoán quận từ tên tuyến.
- Ngày 23/10: đưa 1.429 đơn về KV1, 4.518 đơn về KV3; còn 42 đơn chưa rõ KV. Tổng 51.015 đơn, giữ nguyên số đơn của từng mã tuyến/COT. Đọc lại toàn bộ 1.318 dòng sau ghi, khớp kết quả chuẩn bị. Báo cáo: `data/pickup-area-scope-verification-2026-10-23.json`.
- Phường Bình Hưng Hòa thuộc KV3; khi chưa phân biệt được quận cũ, nằm trong nhóm “Chưa rõ quận cũ” của KV3. Frontend chọn KV → quận → phường → tuyến; KPI/COT theo scope. Các quận cùng tên ở KV khác nhau không còn bị gộp.
- 76 kiểm thử Python và lint hai component Volume đạt. TypeScript chỉ báo lỗi `geojson`/implicit-any ở phần bản đồ đã có từ trước.

## Tuyến Pickup qua pickup point — 24/10/2026

- Đã đồng bộ lại Pickup ngày 23/10/2026: **51.015 đơn**, 1.318 dòng tổng hợp; **51.009 đơn có mã tuyến**, **390 tuyến riêng biệt**, 6 đơn còn thiếu tuyến. 5.989 đơn chưa xác định duy nhất quận/phường được giữ trong nhóm chưa rõ địa bàn, vẫn giữ mã tuyến đã xác minh.
- Nguồn: vận đơn tracking → pickup point từ pickup order search (ghép chính xác ID/station, chia theo giờ và tránh giới hạn 10.000 bản ghi; thiếu thì gọi pickup_info) → nhóm tuyến SPX group_type=4. Nhóm ảo và mapping nhiều tuyến không được dùng. Danh mục nhóm là cấu hình SPX hiện tại, không phải lịch sử tuyến ngày tạo đơn.
- Đã đọc lại toàn bộ dòng ngày 23/10 từ database website, đối chiếu từng trường với báo cáo và xác nhận tổng đơn. Chi tiết: `data/pickup-point-route-verification-2026-10-23.json`; snapshot cũ: `data/pickup-before-point-routes-2026-10-23.json`.
- Đối chiếu API chi tiết: mẫu 100 đơn ban đầu và mẫu ngẫu nhiên 30 đơn sau đồng bộ đều khớp pickup point. 75 kiểm thử Python đạt, gồm phân trang, giới hạn 10.000, station, điểm xung đột, cache, lỗi phiên và bảo toàn tổng theo tuyến.
- Mở lại launcher đang chạy để nạp phiên bản mới cho các lần đồng bộ tiếp theo. Chỉ chạy lại Pickup ngày 23/10 trong lượt này; không sửa dữ liệu Delivery/Performance.

## Worker tồn Delivery — 24/10/2026

- Đã cài bốn RPC vào Supabase website: claim Delivery riêng, heartbeat, báo lỗi và publish snapshot. Chỉ service_role được gọi; các hàm dùng security invoker. Không mở quyền cho anon/authenticated, không thay đổi bảng/rule đọc hiện tại.
- Kiểm thử tích hợp PostgreSQL trong transaction rollback: không lấy job Pickup/All; chỉ một job RUNNING; sai worker không heartbeat được; chặn snapshot rỗng; publish và DONE nguyên tử; retry cùng snapshot không ghi trùng. Các fixture đã rollback toàn bộ.
- Chạy thật lúc 00:24 giờ Việt Nam: đọc 293 đơn LMHub_Received trong cửa sổ tạo 14 ngày. Loại 2 đơn đã gán rider; tra danh mục phường mới và xác định thêm 9 đơn; giữ 289 đơn thuộc KV1–KV5.
- Hai đơn chưa xác định duy nhất địa bàn được bỏ qua theo mặc định: tuyến `Z1.02.PĐ.05` (1 đơn), `HUC-01` (1 đơn). Không gán khu vực theo mã tuyến phỏng đoán. Dùng chế độ strict để chặn đồng bộ nếu muốn xử lý hết mapping trước.
- Job `8bcd7a2e-8499-4227-8a86-2e1c8933fadb` DONE; snapshot `sdd-delivery-796787bb570c48ed8d23603ceeb405b8`. Đọc lại toàn bộ 289 bản ghi, đối chiếu từng trường với `data/delivery-inventory-20261024-002436-899006.json`: khớp.
- 64 kiểm thử Python đạt; kiểm tra khởi tạo GUI có nút bật/dừng worker, tuỳ chọn tự cập nhật và giữ các lựa chọn Volume/Performance. Python compileall đạt.
- Worker có heartbeat 30 giây, lease 5 phút, timeout fetch 10 phút; ngoài giờ tự động vẫn nhận yêu cầu Delivery từ web. GUI dừng sau khi job hiện tại hoàn tất.

## Thêm Performance rider — 24/10/2026

- GUI có lựa chọn Performance; CLI hỗ trợ `--job performance` và `--job all`. `both` tiếp tục chạy hai job volume.
- Đã chạy dry-run thật cho ngày 23/10/2026 bằng cookie SPX dùng chung: Delivery 2.095 rider / 21 trang; Pickup 2.093 rider / 21 trang; ghép được 2.095 rider.
- Tổng Delivery assigned/delivered: 64.427 / 59.764; Pickup assigned/picked: 64.507 / 50.112. Đây là toàn bộ phạm vi báo cáo SPX giống launcher2; website lọc khu vực qua bảng riders.
- Báo cáo thật: `data/performance-2026-10-23-20261024-000419-932181.json`. Lượt thử này không ghi hoặc xoá dữ liệu trên web.
- Đọc cấu trúc toàn bộ cột `driver_performance_daily` qua Supabase của website: thành công. Không thay đổi schema.
- 46 kiểm thử Python đạt, bao gồm ghép rider, kiểm tra phân trang, dữ liệu thiếu/sai, dry-run độc lập mapping, lỗi ghi batch không xoá snapshot, schema không khớp và cleanup đúng ngày.
- Biên dịch Python và kiểm tra khởi tạo GUI với lựa chọn Performance: thành công. Fixture offline có tại `examples/performance.json`.

# Kiểm tra dữ liệu ngày 23/10/2026

## Giữ đơn chưa rõ địa bàn seller, lúc 20:39

Theo yêu cầu mới, Pickup không loại đơn chưa xác định được địa bàn. Đã fetch lại và đồng bộ 49.814 đơn: 43.930 đơn xác định được địa bàn, 5.884 đơn trong nhóm Chưa rõ địa bàn seller (`area=UNRESOLVED`), tổng cộng 180 dòng tổng hợp. So với lượt trước, nhóm chưa rõ tăng thêm 2 đơn do snapshot nguồn mới.

Đã lưu danh sách chi tiết và mã đơn theo từng dòng tại `data/pickup-unresolved-2026-10-23-20261023-203912-595006.json` và `.txt`. Đã đọc lại database để đối chiếu toàn bộ bản ghi, tổng nhóm chưa rõ khớp số mã đơn duy nhất trong file. 35 kiểm thử Python đạt. Báo cáo: `data/unresolved-group-verification-20261023.json`. Lần chạy tiếp theo thay snapshot cùng ngày bao gồm cả nhóm UNRESOLVED, tránh cộng trùng và tự bỏ nhóm cũ khi đã phân loại được.

## Sửa Pickup theo seller, lúc 20:31

Pickup dùng địa chỉ seller qua `lowest_seller_address_id` và danh mục `ward_new_upload`, không lấy tuyến buyer làm dữ liệu thay thế. Đã sao lưu dữ liệu cũ trong `data/before-seller-fix-20261023.json`.

- Đọc 49.754 đơn nguồn; đồng bộ 43.872 đơn theo seller, tổng cộng 165 dòng tổng hợp.
- Bỏ qua 5.882 đơn chưa xác định duy nhất địa bàn seller (gồm phường mới sáp nhập qua nhiều quận cũ), theo lựa chọn bỏ qua đơn chưa mapping của người dùng.
- API tracking không trả mã tuyến seller riêng trong lượt chạy này. Toàn bộ mã tuyến trong kết quả để trống, web hiển thị “Chưa có mã tuyến”; không tự chia đơn vào tuyến giả định.
- Đã đối chiếu từng trường của toàn bộ bản ghi Pickup trên database với báo cáo; 34 kiểm thử Python đạt. Chi tiết trong `data/seller-fix-verification-20261023.json`.
- Lượt sửa này chỉ ghi Pickup; Delivery tiếp tục theo địa bàn nhận hàng.

## Lịch sử sửa phân vùng trước khi đổi Pickup sang seller

Đã áp dụng phân vùng 5 KV theo danh sách người dùng chốt, thay cho cột Zone cũ của Coverage SA. Đã sao lưu snapshot trước sửa tại `data/before-geography-fix-20261023.json`, chạy lại Pickup và Delivery ngày 23/10, rồi đọc database để đối chiếu từng trường của toàn bộ bản ghi với báo cáo nguồn.

| Job | Đơn nguồn | Đã đồng bộ | Thiếu mapping bỏ qua | Dòng tổng hợp | Đơn bị loại do KV nguồn cũ |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pickup | 49.529 | 49.517 | 12 | 780 | 0 |
| Delivery | 63.380 | 63.294 | 86 | 607 | 0 |

| KV5 theo quận | Pickup | Delivery |
| --- | ---: | ---: |
| Gò Vấp | 4.492 | 5.676 |
| Quận 12 | 3.730 | 4.758 |
| Hóc Môn | 2.107 | 2.653 |
| Thủ Đức cũ | 2.430 | 3.136 |
| Tổng KV5 | 12.759 | 16.223 |

29 kiểm thử Python đạt; lint file phân vùng website đạt. Các đơn chưa mapping tiếp tục được bỏ qua và thống kê theo lựa chọn người dùng. Nguồn SPX là snapshot tại thời điểm lấy, chưa phải số chốt cuối ngày. Chi tiết đối chiếu tại `data/geography-fix-verification-20261023.json`.

## Lịch sử chạy thử trước khi sửa phân vùng

Phần dưới là kết quả cũ trước khi sửa phân vùng, khi đó chỉ chạy thử, không ghi/xoá dữ liệu trên website.

- Đọc cấu trúc `pickup_volume` và `delivery_order` của database website: thành công.
- Đọc Coverage SA: 300 Zone ID; có 2 Zone ID trùng mapping, xử lý theo dòng cuối như launcher2.
- Cookie và API SPX pickup/delivery: đọc thành công.
- Unit tests: 25 kiểm thử; kiểm tra biên COT, scope KV, phân trang/split thời gian, bảo vệ snapshot và dry-run.
- `pip check`: không có thư viện xung đột.

## Dry-run dữ liệu thật

| Job | Đơn nguồn đã đọc | Đơn KV1–KV5 | Ngoài phạm vi KV | Chưa mapping | Dòng tổng hợp |
| --- | ---: | ---: | ---: | ---: | ---: |
| Pickup | 49.273 | 38.983 | 10.278 | 12 | 663 |
| Delivery | 63.695 | 50.447 | 13.160 | 88 | 509 |

Đây là snapshot tại thời điểm chạy thử, không phải chốt cuối ngày. Tổng `total_orders` trong từng báo cáo bằng số đơn được đưa vào KV1–KV5. Không có đơn có ID/thời gian lỗi trong lượt chạy này.

Có tổng cộng **100 đơn chưa có mapping**. Khi đồng bộ, mặc định dừng trước khi ghi nếu còn đơn chưa xác định khu vực. Có thể sửa Coverage SA rồi chạy lại, hoặc chủ động chọn **Bỏ qua đơn chưa xác định được khu vực** trong launcher. Các báo cáo được tạo sau phiên bản này liệt kê thêm `stats.unmapped_zones` để đối chiếu.

JSON và log chi tiết nằm trong `data/`, `logs/` (đã được đưa vào `.gitignore`).
## Zone ID cũ/mới — Delivery 22/10/2026, kiểm tra 24/10/2026

- Lưu nguyên bản CSV người dùng gửi tại `config/mappings/coverage-old-new.csv` (732 dòng; SHA-256 `7dc484b7e559390600898c1cbc9e83e5ee17c713ebccf4a98fc12ba30edd144a`). Đọc được 1.174 alias, bao gồm mã cũ, mã v2, Hub và mã bỏ ký hiệu `.#`; 11 alias có nhiều địa bàn được giữ để đối chiếu người nhận thay vì ghi đè theo dòng cuối.
- `Z5.01.NL.01` và `Z4.01.NL.01` cùng về KV4 / Quận 3 / Phường 9; `Z6.02.TTH.01` lấy từ cột Hub, về KV5 / Quận 12 / Phường Hiệp Thành. Mã Z6 không bị loại khỏi phạm vi KV1–KV5 chỉ vì mang tiền tố cũ.
- Đã sao lưu 723 dòng / 61.040 đơn trước sửa tại `data/before-old-new-zone-2026-10-22.json`, đọc lại đủ 61.333 đơn từ SPX rồi đồng bộ 725 dòng / 61.316 đơn. Trong 293 đơn trước đây chưa mapping, 292 đơn đã khớp; đồng thời phát hiện 16 đơn khác dùng mã trùng/địa bàn người nhận mâu thuẫn nên không tiếp tục gán theo dòng cuối. Tăng ròng 276 đơn trên web.
- Còn 17 đơn chưa xác định: `Z4.02.TH.01` (8), `Z4.02.TH.02` (2), `Z4.02.TH.03` (5), `Z3.03.PVH.01` (1), mã trống (1). Đối chiếu SPX cho một mã TH xác nhận API trả `buyer_addr_district`, tên phường và địa chỉ rỗng; phường tra qua ID lại không khớp các địa bàn trong CSV. Không giả định KV theo tiền tố khi hai phiên bản dùng lại cùng mã.
- Đọc lại database và so sánh từng trường của toàn bộ 725 dòng với báo cáo: khớp. KV1: 10.696; KV2: 13.726; KV3: 10.850; KV4: 10.770; KV5: 15.274. Tổng 61.316 + 17 chưa mapping = 61.333 đơn nguồn.
- 94 kiểm thử Python đạt, bao gồm alias cũ/mới, Hub khác cột Zone, ký hiệu `.#`, mã trùng cần phường/quận người nhận và áp dụng alias cho tồn Delivery. Chứng cứ: `data/old-new-zone-verification-20261022.json`; nguồn tổng hợp: `data/delivery-2026-10-22-20261024-104047-115745.json`.


## Tồn Delivery — tối ưu và sửa timeout 26/10/2026

Lỗi 57014 khi publish đã được sửa và kiểm chứng bằng job thật. Với 27.238 đơn hợp lệ, job 38cd87e1-a730-4dc9-b524-ddd823aff2b1 hoàn tất thành công trong 47.924 ms, gồm 12.436 ms gửi/ghi và xác nhận, so với lượt lỗi cũ 101.604 ms (19.973 đơn). Payload 27.180 đơn giảm từ 9.764.213 xuống 2.263.313 byte. Cache chỉ giữ partition plan; trạng thái đơn luôn đọc lại. Pool riêng DELIVERY_WORKERS mặc định 24, có thể hạ nếu cần. Benchmark count 1.000/2.000 chậm hơn nên giữ count 500. Cache địa bàn theo lượt và chỉ mục phường giữ nguyên quy tắc đối chiếu.

Triển khai SQL trước worker: trong repo website chạy scripts/apply-delivery-performance.cjs với --env-file=.env --env-file=.env.local, rồi thêm --test để kiểm tra rollback. Hướng dẫn và số đo: C:/code/sdd-performance-project/docs/delivery-performance.md. Python suite: 178 tests, 1 skipped. benchmark_delivery.py chỉ đọc SPX, không publish, không tự mở trình duyệt.

## Performance COT 25–26/10/2026

Đã sửa thiếu cache COT khi Performance chạy độc lập. Volume lưu aggregate theo nguồn/ngày; Performance tự lấy phần thiếu. Có guard phạm vi và không nội suy completion; UI hiển thị phần chưa xác định thay vì gán tổng theo COT hồ sơ. Đã đối chiếu 2.095 dòng/ngày trên DB với artifact cuối, 0 sai khác, 0 dòng có tổng tách COT vượt tổng ngày. Ngày 25 có 906 rider KV1–KV5 có COT hợp lệ; ngày 26 có 307. Ngày 23–24 giữ nguyên dữ liệu. Xem docs-performance-cot-fix.md để phân biệt số ban đầu với kết quả sau loại sai phạm vi. 185 tests, 1 skipped; website build/lint đạt.
