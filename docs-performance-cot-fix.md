# Performance thiếu COT từ 25/10/2026 — kết quả cuối

## Nguyên nhân

Tổng assigned/completed lấy từ báo cáo rider SPX, còn số tách COT lấy từ đơn Volume. Luồng cũ chỉ sinh rider-cots-YYYY-MM-DD.json khi chạy Volume và Performance chung một lượt. Worker trung tâm chạy nguồn độc lập, nên ngày 23–24 có cache cũ, ngày 25–26 thiếu cache. Performance ghi mặc định toàn bộ COT_COUNTERS bằng 0 và cot_breakdown null. UI có thể gán toàn bộ theo COT hồ sơ rider thay vì một phân loại thực.

## Sửa

- Volume Pickup/Delivery lưu aggregate COT riêng theo nguồn/ngày sau khi kiểm tra đủ nguồn và ID duy nhất. Cache atomic, tách project/trạm/ngày/nguồn; TTL 10 phút cho ngày hiện tại, 1 giờ cho ngày cũ. Cache không lưu raw shipment hay địa chỉ khách hàng.
- Performance chạy riêng tự đọc nguồn thiếu/hỏng/quá hạn, không còn cần chạy chung all. Lỗi đọc COT dừng trước khi ghi Performance.
- Giữ mốc thời gian classifier, bỏ nội suy số hoàn thành theo tỷ lệ. Nếu COT của một nguồn/rider vượt assigned/completed của báo cáo ngày hoặc hoàn thành vượt phân công, giữ tổng chính thức, đặt phần tách không khớp thành chưa xác định và lưu quality.source_scope_mismatch trong cot_breakdown.
- UI không gán toàn bộ sản lượng vào COT hồ sơ khi thiếu chi tiết. Hiển thị Chưa xác định COT; nếu chỉ có một phần được phân loại thì hiện phần phân công còn thiếu.

## Dữ liệu đã xử lý

Đã sao lưu trước sửa tại data/performance-cot-backup/. Đọc thực SPX, đồng bộ lại ngày 25 và 26; sau đó sửa riêng COT dựa trên các snapshot đã xác minh. Bước sửa riêng kiểm tra tổng official chưa đổi trước khi update trong transaction, giữ tổng và fetched_at. Không ghi ngày 23–24.

Toàn bộ 2.095 dòng mỗi ngày, 8 cột COT, chi tiết và số tổng đều khớp artifact cuối: 0 sai khác. Trong 1.151 rider KV1–KV5, ngày 25 có 906 rider có số COT hợp lệ, ngày 26 có 307. 0 dòng có tổng tách assigned/completed vượt tổng ngày. Các phần không khớp giữ chưa xác định; không gán hoặc nội suy để ép đủ tổng. Số 1.070 rider ở bước kiểm tra ban đầu là trước khi loại các phân loại khác phạm vi, không phải kết quả cuối.

Artifacts cuối: data/performance-cot-corrected-2026-10-25.json và data/performance-cot-corrected-2026-10-26.json. Bản đọc thực gốc được giữ riêng để đối chiếu. Trong lượt đọc lại bổ sung, SPX từng trả lỗi ứng dụng ở trang 6; các lượt đó dừng trước khi ghi. Phần hiệu chỉnh cuối dùng snapshot đọc đủ đã xác minh, không dùng trang đọc dở hay dữ liệu mẫu.

## Kiểm tra

Python: 185 tests, 1 skipped. Các case mới bao gồm nguồn chạy độc lập, cache thiếu/hỏng/quá hạn/sai phạm vi, ID trùng, lỗi nguồn không ghi, không nội suy completion và loại COT vượt tổng. Frontend build production và lint đạt. Localhost đã khởi động lại.

repair_performance_cot.py YYYY-MM-DD mặc định dry-run; thêm --publish để đồng bộ thực. Script chặn tự mở trình duyệt khi hết phiên. Trong repo website: scripts/verify-performance-cot.cjs YYYY-MM-DD artifact.json đối chiếu DB; scripts/apply-performance-cot-repair.cjs chỉ nhận artifact cot_only, có backup và guard tổng official. Chạy với --env-file=.env --env-file=.env.local.

## Pick Performance theo giờ picked — 26/10/2026

COT Pick chỉ xét pickup_time hợp lệ, theo Asia/Ho_Chi_Minh: trước 12:00:00 là COT 1, từ 12:00:00 là COT 2. Bỏ fallback create_time/ctime/createTime, delivering_time và delivered_time. Không suy ra picked từ order_status khi thiếu pickup_time. Đơn thiếu giờ picked không sinh phân loại theo giờ; phần còn thiếu trên bảng tiếp tục được gộp vào COT chính rider theo quy tắc người dùng đã chốt.

Đổi context riêng cache Pickup sang pickup-picked-at-noon-v3 để cache cũ bị đọc lại; cache Delivery giữ nguyên. Không đổi COT của Volume/Tồn Pickup, chỉ đổi classifier phục vụ Performance. Đã đọc SPX thực để xác nhận pickup_time: có đơn tạo 08:58 nhưng picked 13:27, đúng COT 2.

Đã fetch lại nguồn Pickup ngày 25 và 26, tạo artifact performance-picked-time-corrected-YYYY-MM-DD.json. Snapshot trước sửa: data/performance-cot-backup/picked-time-YYYY-MM-DD-before.json. Bước backfill dùng guard tổng official và chỉ thay phân loại; Delivery, tổng Performance và fetched_at giữ nguyên. Source artifacts và số đếm picked là theo tập đơn nguồn Volume được đọc, không đại diện toàn bộ SPX ngoài phạm vi nguồn.

Test: trước 12h, đúng 12h, sau 12h, timestamp milliseconds, thiếu/0/hỏng pickup_time không lấy giờ tạo/giao thay thế, cache phiên bản cũ bị vô hiệu. Full suite: 188 tests, 1 skipped.
