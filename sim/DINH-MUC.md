# Định mức nhân sự: làm tay so với có agent, và nguồn của từng con số

Bảng nhân sự ở màn cuối được tính bằng `sim/staffing.py` (bộ máy trung tâm 50 trạm) và `sim/workload.py` (người trực
mỗi trạm). Trang này giải thích cách tính và ghi nguồn của mọi con số. Ký hiệu: **[Nguồn]** là số tra được (luật, tin
tuyển dụng, số liệu ngành); **[Giả định]** là số Chợ Z cần xác nhận.

## 0. Bản 04/11 trưa: theo thiết kế Chợ Z (chủ dự án chốt)

| Quyết định | Áp dụng cho cả hai cột |
|---|---|
| Luồng hàng | Kho chia **theo trạm, theo mặt hàng** (mỗi trạm một sọt theo phiếu gom); không đóng túi theo từng đơn. Người trực trạm giao từng hộ theo đơn trên app (đã trả bằng 5 % của trạm) |
| Trả lương | Văn phòng, điều phối, quản lý: toàn thời gian có bảo hiểm (× 1,318). Chia hàng, kiểm hàng nhập kho, giám sát trạm: **theo giờ 30.000 đ** (Định hướng Chợ Z: sinh viên part-time; mức tối thiểu vùng I là 25.500 đ/giờ). Xe: **thuê ngoài theo chuyến**, 60–70 triệu/tháng, chờ báo giá đối tác |
| Quản lý | 4 người khi làm tay (kho, trạm, nhà cung cấp, tiền); 2 người khi có agent (agent theo dõi, báo cáo) |
| Phí nuôi bộ máy | 4 % (kho và giao hàng 2 % + bộ máy 2 %) của 4,5 tỷ tiền hàng/tháng = **180 triệu/tháng** (slide tài chính Chợ Z: 1.500 đơn × 100.000 đ × 30 ngày) |

Kết quả 50 trạm, 1.500 đơn/ngày, 30 ngày vận hành:

| | Chỉ con người | Con người + 6 agent |
|---|---|---|
| Văn phòng, toàn thời gian | 19 người (CSKH 3, thu mua 5, điều phối xe 1, đăng bán 1, kế toán 9): 268 triệu | 2 người điều phối, chịu trách nhiệm số liệu: 34 triệu |
| Quản lý, toàn thời gian | 4 người: 84 triệu | 2 người: 42 triệu |
| Chia hàng theo trạm, theo giờ | 550 dòng × 2,25 phút ≈ 20,6 giờ/ngày: 18,6 triệu | giống bên trái |
| Kiểm hàng nhập kho, theo giờ | 2 giờ/ngày: 1,8 triệu | giống bên trái |
| Giám sát trạm, theo giờ | 2 người × 4 giờ: 7,2 triệu | giống bên trái |
| Vận chuyển thuê ngoài | 65 triệu (60–70) | giống bên trái |
| 6 agent | không có | 8,5 triệu (đo từ lượt chạy thật 30, 150, 300 đơn; 189 đ/đơn) |
| **Tổng** | **445 triệu/tháng** | **178 triệu/tháng, trong 180 triệu phí** |

Các mục dưới đây (1–6) là bản trước (soạn từng đơn, mọi vai toàn thời gian); giữ lại để thấy cách tính đã đổi gì.

## 1. Ba nhóm người, ba cách tính

| Nhóm | Ai | Cách tính |
|---|---|---|
| Người trực mỗi trạm | Chủ tiệm hoặc bạn trẻ đã có ở tiệm | Phút mỗi ngày làm việc của Chợ Z; không tính lương ở bên nào vì trạm được trả bằng 5 % phí. Cùng một người ở cả hai cách vận hành |
| Bộ máy trung tâm: việc tại trạm, tại kho | Soạn hàng và đóng túi, kiểm hàng nhập kho, tài xế, giám sát trạm | Người nguyên; **giống hệt nhau** ở hai bên (agent không thay việc tại trạm, tại kho) |
| Bộ máy trung tâm: việc giấy tờ, tính toán | Chăm sóc khách hàng, thu mua và đặt hàng, điều phối xe, đăng bán, kế toán và đối soát | Làm tay: người nguyên theo từng vai. Có agent: agent làm, người duyệt việc có tiền hoặc có khách; giữ ít nhất 1 người chịu trách nhiệm |

Số người = phút việc mỗi ngày ÷ (480 phút × 85 % làm hiệu quả) × 1,2, làm tròn lên theo từng vai. Hệ số 1,2: mở 7
ngày/tuần trong khi một người làm 26 công/tháng (30/26 = 1,15) cộng 12 ngày phép/năm (Bộ luật Lao động 2019).
Trưởng nhóm: 1 người cho khoảng 10 người.

## 2. Chi phí một người

| Khoản | Mức | Nguồn |
|---|---|---|
| Lương tối thiểu vùng I (nội thành Hải Phòng, Cát Hải) | 5.310.000 đ/tháng từ 01/01/2026 | [Nguồn] Nghị định 293/2025/NĐ-CP |
| Doanh nghiệp đóng thêm | 23,5 % lương: BHXH 17,5 %, BHYT 3 %, BHTN 1 %, công đoàn 2 % | [Nguồn] Luật BHXH 41/2024/QH15, Luật Công đoàn 2024 |
| Lương tháng 13 | + 1/12 | [Nguồn] thông lệ trong các tin tuyển dụng dưới đây |
| Hệ số chi phí trên lương | × 1,318 | tính từ hai dòng trên |

Lương theo vai (giữa khoảng lương tin tuyển dụng Hải Phòng 2026):

| Vai | Lương | Khoảng tìm được |
|---|---|---|
| Soạn hàng, đóng túi | 10 tr | [Nguồn] công nhân kho Hải Phòng 8–13 tr, 10–12 tr (123job, Joboko) |
| Kiểm hàng nhập kho | 10 tr | [Nguồn] phụ kho, QC 10–15 tr |
| Tài xế | 12,5 tr | [Nguồn] tài xế C1/C Hải Phòng 10–14 tr, PDC 12,5–13 tr (JobsGO) |
| Chăm sóc khách hàng | 9 tr | [Nguồn] CSKH online Hải Phòng 7–12 tr, 5,5–10 tr (Timviec365, CareerViet) |
| Điều phối xe | 13 tr | [Nguồn] điều phối giao vận WinCommerce Hải Phòng 12–14 tr |
| Kế toán, đối soát | 11 tr | [Nguồn] kế toán Hải Phòng 9–15 tr, công nợ 8–13 tr (Joboko, CareerViet) |
| Trưởng nhóm, quản lý kho | 16 tr | [Nguồn] quản lý kho Hải Phòng 10–18 tr, 14 tr trở lên |
| Thu mua, đăng bán, giám sát trạm | 11 / 9 / 12 tr | [Giả định] theo vai gần nhất |

## 3. Khối lượng việc của 50 trạm (1.500 đơn/ngày)

| Việc | Định mức | Nguồn |
|---|---|---|
| Soạn hàng, đóng túi | 100 món/giờ; Giỏ trung bình 4,7 món → 2,8 phút/đơn | [Nguồn] soạn thủ công ngành tạp hóa 50–60 món/giờ (Wynshop), 100–200 món/giờ ở nhiều nhà bán lẻ (StrongPoint 2023) |
| Chăm sóc khách | 10 lượt hỏi / 100 đơn × 5 phút | [Nguồn] 3,2 / 100 đơn trung vị bán lẻ (Gartner 2024) đến 20 / 100 đơn ngành thực phẩm (Gorgias 2026); 3–5 phút mỗi lượt (HiveDesk) |
| Lượt hỏi cần người khi có agent | 35 % × 2 phút | [Nguồn] đổi trả, hoàn tiền, khiếu nại chiếm khoảng 30–45 % lượt hỏi (HiveDesk) |
| Xe | 20 trạm một tuyến, 240 phút một tuyến (đi giao, về gom giặt và bưu phẩm) | [Nguồn] mua chung Trung Quốc 15–30 điểm/xe; [Giả định] thời gian tuyến |
| Kiểm hàng nhập kho | 15 phút mỗi chuyến hàng nhà cung cấp, 15 nhà cung cấp | [Giả định] |
| Giám sát trạm | 1 lần thăm/trạm/tuần × 60 phút | [Giả định] |
| Thu mua, gom nhu cầu | 30 phút/trạm/ngày | yêu cầu demo mục 6b, [Giả định] |
| Kế toán | 1,5 phút/đơn + 10 phút đối chiếu tiền mỗi trạm + 10 phút báo cáo mỗi nhà cung cấp | mục 6b + [Giả định] |
| Đăng bán | 17 phút/tin, 15 tin/ngày | mục 6b + [Giả định] |
| Điều phối xe | 80 phút/tuyến làm tay, 15 phút/tuyến khi có agent | [Giả định] |

## 4. Chi phí agent: đo, không nhân

Ba lượt chạy thật trên cùng một trạm với 30, 150 và 300 đơn (`runs/live_final`, `runs/scale_x5`, `runs/scale_x10`):

| Khâu | Đi theo | Đo được |
|---|---|---|
| Gom đơn, đối soát | số đơn | khoảng 4.100 đ/ngày cố định + 53 đ mỗi đơn |
| Đăng bán, kiểm chứng | số tin | khoảng 1.700 đ cho 4 tin |
| Kiểm lô ở kho | số nhà cung cấp | khoảng 2.500 đ cho 6 lô |
| Lộ trình xe | số tuyến | khoảng 700 đ một tuyến |
| Chăm sóc khách | số lượt hỏi | khoảng 1.200 đ một lượt (có cả xin duyệt và trả lời) |

Cộng lại cho 50 trạm: khoảng 280.000 đ/ngày, **8,5 triệu đ/tháng (30 ngày), 189 đ mỗi đơn**. Nhân thẳng theo số đơn từ
một trạm (cách cũ) cho 15,6 triệu, gần gấp đôi. Lượt 300 đơn có một lỗi: agent chia đơn không tự nhắn đủ 20 hộ bị thay
hàng, bộ điều phối phát hiện và báo lỗi; ở vận hành thật, tin báo hàng loạt nên do phần mềm gửi theo mẫu.

## 5. Kết quả hiện tại (live_final)

| | Chỉ con người | Con người + 6 agent |
|---|---|---|
| Người trực mỗi trạm | 119 phút/ngày | 43 phút/ngày |
| Bộ máy 50 trạm: việc giấy tờ | 19 người | 2 người |
| Bộ máy 50 trạm: việc tại trạm, tại kho | 20 người | 20 người |
| Bộ máy 50 trạm: quản lý | 4 người | 3 người |
| Tổng | 43 người, 634 triệu đ/tháng | 25 người + 6 agent, 388 triệu đ/tháng (agent 8,5 triệu) |

Chênh lệch khoảng 246 triệu đ/tháng, gần như toàn bộ đến từ khối giấy tờ. Không tính ở bên nào: người trực 50 trạm,
xe và xăng dầu, mặt bằng kho.

## 6. Chợ Z cần chốt

Số nhà cung cấp và số tin đăng mỗi ngày khi có 50 trạm; thời gian một tuyến xe; định mức thu mua, kế toán, đăng bán;
lương ba vai đang giả định; tỷ lệ khách hỏi thực tế của Chợ Z (nếu đã có số từ Zalo). Đổi một số trong
`sim/staffing.py` rồi chạy `python -m sim.workload runs/live_final/events.jsonl`, không cần gọi lại model.
