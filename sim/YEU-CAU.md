# Làm rõ yêu cầu: demo "Một ngày của Trạm Z"

Bản yêu cầu gốc: tài liệu "Yêu cầu kỹ thuật: Bộ não agent Phố Trạm Z" (03/11/2026, anh Mạnh Hùng). Dưới đây là cách
hiểu đang dùng trong code, và những chỗ **cần anh Hùng xác nhận** trước khi số liệu được dùng ngày thi. Không chỗ nào
được tự đoán rồi giấu trong số: mỗi giả định có một dòng ở đây và một hằng số trong code.

## A. Đã chốt theo yêu cầu (không cần hỏi)

| Mục | Cách làm |
|---|---|
| Mã tác nhân, địa điểm, tên trường | Đúng mục 3 và 4; `sim/schema.py` từ chối mã lạ |
| Bảy cảnh | Đúng thứ tự và giờ mục 5 |
| Agent Meta | Gọi `app/pipeline.py` qua wrapper mỏng `sim/agents/dang_ban.py`, không viết pipeline mới |
| Agent Claude | AgentScope `ReActAgent` + `MsgHub` (bản 1.0.21; bản 2.x đã đổi tên hai lớp này), model `claude-sonnet-5-5` |
| Người duyệt | Tự duyệt sau 2 giây mô phỏng ở cả mock và live; luôn đủ cặp `approve_request` + `approve` |
| Phút người | Bảng mục 6: duyệt listing 0,5; nhận hàng kho 10; nhận hàng trạm 10; mỗi khách 1; đổi trả 1; đối soát 5 |
| Chi phí | Lấy từ usage trả về, nhân giá niêm yết (nguồn và ngày trong `summary.json`); mock ghi 0 |
| Trần chi phí | `MAX_RUN_USD=40` một lần chạy; dừng có trật tự, sự kiện đã ghi vẫn hợp lệ |

## B. Cách hiểu cần xác nhận (đang dùng giá trị trong cột "Đang dùng")

| # | Câu hỏi | Đang dùng | Ảnh hưởng |
|---|---|---|---|
| B1 | `t` của buổi tối hôm trước | số âm (20:00 = −14400) để giữ đúng ví dụ 05:12 = 18720 | sân khấu sắp theo `t`, không đổi gì khác |
| B2 | Bộ đếm "đơn" tăng lúc nào | lúc khách nhận và trả tiền (mục 5, cảnh 5); số Giỏ đặt hiện trong bong bóng cảnh 1 | 20 giây đầu bộ đếm đơn bằng 0 |
| B3 | Giá 12 mặt hàng và cơ cấu Giỏ | tự đặt, chẵn nghìn; 10 Giỏ 70k, 12 Giỏ 100k, 8 Giỏ 150k, 6 hộ mua thêm hộp cháo hàu | tiền hàng 3.228.000 đ (ví dụ trong yêu cầu là 3,1 triệu) |
| B4 | Phí 10 % tính trên gì | trên giá trị từng dòng hàng của mỗi nhà cung cấp trong đơn hoàn thành | bảng đối soát theo đơn × nhà cung cấp |
| B5 | 1 % người giới thiệu khi hộ không có ai giới thiệu | mọi hộ trong mô phỏng đều có người giới thiệu (phần lớn là người trực trạm) | nếu thực tế khác, cần quy tắc cho phần 1 % này |
| B6 | Duyệt thay hàng tốn bao nhiêu phút | 1 phút (bằng duyệt đổi trả; bảng mục 6 không có dòng này) | +1 phút người |
| B7 | 20 suất ăn sáng, giặt là 3 hộ | chỉ là một dòng dịch vụ trong `summary.json`; không tính vào đơn, tiền hàng, phí | nếu suất ăn có phí 10 % thì phải thêm vào đối soát |
| B8 | Kiểm lô tại kho theo nhãn và phiếu đặt (đúng hàng, hạn dùng) | agent kiểm; lô tôm có nhãn quá hạn dùng bị loại, nhà cung cấp đổi lô còn hạn | bảng định mức không có dòng "kiểm lô thủ công" nên không tính bên nào |
| B9 | Mở rộng lên 50 trạm | đơn ×50, trạm ×50; 1 kho; vẫn 5 nhà cung cấp; số tin đăng mỗi ngày không tăng | đổi giả định → số người 50 trạm đổi theo (`workload.SCALE`) |
| B10 | Việc tại trạm, tại kho trong bảng nhân sự | thêm 3 dòng từ bảng mục 6 (nhận hàng kho, nhận hàng trạm, giao khách) vào **cả hai bên** cùng số phút, cạnh dòng "chia hàng" | hai cột so sánh cùng một khối việc; agent không "tiết kiệm" việc tại trạm, tại kho |
| B11 | Câu chốt "Bộ máy 6 người lo được 50 trạm" | **chưa có nguồn trong dữ liệu**: số người tính được từ ngày mô phỏng xem `headcount.json` (khối văn phòng và khối việc tại trạm, tại kho tách riêng) | cần anh Hùng định nghĩa "bộ máy" gồm ai, hoặc sân khấu chỉ hiện số tính được |
| B12 | Ảnh và giọng của nhà cung cấp | cháo hàu: 1 ảnh mẫu (thư mục mẫu chỉ có 1 ảnh) + giọng đọc tổng hợp VieNeu "Đoan Trang" (giọng Bắc, không phải người thật) | yêu cầu ví dụ "3 ảnh"; có ảnh thật thêm thì thay vào `samples/products/05-chao-hau-hop` |
| B13 | Mặt hàng bị từ chối | Theo góp ý 04/11 bỏ "rượu ngâm mật gấu"; thay bằng **thuốc lá** (hàng tiệm tạp hóa vẫn bán, Chợ Z không đăng; luật P3 trong `app/guard.py`), ảnh vẽ bằng code `sim/samples/thuoc-la` | từ chối hẳn, không hỏi duyệt; vẫn đủ yêu cầu "một mặt hàng bị chặn" |
| B15 | Cảnh đăng bán: nước mắm Cát Hải, sản phẩm OCOP Hải Phòng | Nước mắm Cát Hải: ảnh thật Quang Hải (cá thu, 28 độ đạm, OCOP 4 sao, 650 ml) chủ dự án gửi qua Drive 04/11; giá 65.000 đ và tồn 60 là số mô phỏng. Bánh đa cua, cháo hàu Khánh Thọ là OCOP 4 sao (Hải Dương đã sáp nhập vào Hải Phòng 2025) | cần ảnh thật và tên đúng của nhà sản xuất nước mắm nếu muốn ghi thương hiệu |
| B16 | Lương tháng để quy người ra tiền ở màn cuối | giả định Hải Phòng 2026: nhân viên trạm 7 tr, nhân viên kho 7 tr, điều phối 10 tr, nội dung 9 tr, kế toán 10 tr, chủ kho 10 tr (`sim/workload.py` WAGES); phần lẻ người tính theo giờ công | đổi một con số, cả hai cột tiền đổi theo; chưa tính bảo hiểm, thưởng |
| B14 | Tỷ giá | 26.300 đ/USD theo yêu cầu, ghi ngày 03/11/2026 | đổi bằng `USD_VND` |

## C. Ràng buộc kỹ thuật phát hiện khi làm

| # | Việc | Xử lý |
|---|---|---|
| C1 | `pnpm build` của landing page chạy `payload migrate`, cần Postgres; laptop ngày thi không có DB | `/demo` nằm trong route group riêng (không Payload); thêm bản tĩnh offline `public/demo/offline/` + server Node nội bộ |
| C2 | Claude 5.5 không nhận `tool_choice` ép công cụ, AgentScope ép khi cần kết quả có cấu trúc | gửi `auto`, prompt yêu cầu kết thúc bằng `generate_response`; ReActAgent tự nhắc nếu thiếu |
| C3 | AgentScope 1.0.21 import hàm mcp đã đổi tên ở mcp 2.x (repo ghim mcp 2.3.0) | alias một dòng trong `sim/agents/claude.py`; venv riêng `.venv-sim` để không đụng môi trường app |
| C4 | Đưa `/demo` lên choz.vn | làm trên nhánh `demo`; **chờ chủ dự án đồng ý** mới đưa lên `main` (Vercel tự deploy `main`) |
