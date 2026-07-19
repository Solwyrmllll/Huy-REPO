---
tags:
  - #OS/Scheduling
  - #Algorithm
  - #TimeSharing
aliases:
  - Round Robin
  - Thuật toán RR
---

# 🔄 Lập lịch Xoay vòng (Round Robin - RR)

**Round Robin (RR)** là một trong những thuật toán [[Lập lịch CPU]] kinh điển nhất, được thiết kế đặc biệt cho các hệ thống chia sẻ thời gian (Time-sharing) [1]. Nó hoạt động dựa trên cơ chế tước quyền (Preemptive) và chia đều CPU cho tất cả mọi người.

## 1. Khái niệm Cốt lõi
- **[[Time Quantum]] (Định mức thời gian)**: Là một lát cắt thời gian nhỏ cố định (thường từ 10-100 mili-giây) được Hệ điều hành quy định để cấp cho mỗi [[Tiến trình (Process)]] trong một lượt chạy [1].
- **Tính công bằng (Fairness)**: Nếu có $n$ tiến trình đang chờ và định mức thời gian là $q$, thì mỗi tiến trình sẽ nhận được chính xác $1/n$ thời lượng CPU [1].
- **Tính sống còn (Liveness)**: Không có tiến trình nào phải mòn mỏi chờ quá $(n-1)q$ đơn vị thời gian trước khi được CPU phục vụ [1].

## 2. Quy trình Vận hành (Vòng lặp 5 bước)
1. **Xếp hàng**: Các tiến trình sẵn sàng sẽ nằm chờ ở đầu [[Hàng đợi Ready (Ready Queue)]] [1].
2. **Cấp phát**: Bộ điều phối lấy tiến trình ở đầu hàng đợi ra và cấp phát CPU cho nó [1].
3. **Thực thi & Giám sát**: Bộ đếm thời gian phần cứng ([[Timer Interrupt]]) bắt đầu đếm ngược thời gian $q$ [2].
4. **Ngắt tước quyền**: Khi thời gian $q$ đã hết (Elapsed), tiến trình lập tức bị Hệ điều hành tước quyền (Preempted) [1].
5. **Luân chuyển**: Hệ điều hành thực hiện [[Chuyển đổi ngữ cảnh (Context Switch)]]:
   - Lưu trạng thái (Save state) đang dở dang của tiến trình này vào thẻ căn cước [[PCB (Process Control Block)]] [3, 4].
   - Ném tiến trình này về **cuối (tail)** của [[Hàng đợi Ready (Ready Queue)]] để xếp hàng lại [1].
   - Nạp tiến trình tiếp theo lên chạy [4].

## 3. Vấn đề "Goldilocks" (Kích thước Quantum)
Bài toán đau đầu nhất là chọn kích thước chuẩn xác cho $q$ [5]:
- Nếu $q$ **quá lớn**: Thuật toán RR bị thoái hóa, biến thành thảm họa [[FCFS (First-Come First-Served)]] [5].
- Nếu $q$ **quá nhỏ**: Tần suất ngắt diễn ra quá rày đặc, dẫn đến chi phí hao tổn (Overhead) của việc [[Chuyển đổi ngữ cảnh (Context Switch)]] quá cao, làm CPU chậm chạp và lãng phí [5].

## 4. Đánh đổi Hiệu năng
Nhờ việc chia nhỏ thời gian, giao diện người dùng sẽ mượt mà hơn (thời gian phản hồi - **Response time** tốt hơn) so với thuật toán chạy một mạch như [[SRTF (Shortest Remaining-Time First)]] [5, 6]. Tuy nhiên, tổng thời gian để hoàn thành trọn vẹn một công việc (**Turnaround time**) trung bình sẽ bị kéo dài hơn [5].

---
