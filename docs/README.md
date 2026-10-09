# Architecture Diagram

![Architecture Diagram](architecture-diagram.png)

# Mô tả chi tiết

---

## 1. Repo Retriever

- **Trách nhiệm:** Khảo sát toàn diện kho mã nguồn thông qua công cụ phân tích đồ thị để xây dựng call graph, tự động định vị các sources và sinks, từ đó trích xuất taint flow graph kết nối từ source đến sink để chuyển tiếp cho bộ điều phối.

- **Input:** Mã nguồn.

- **Output:** Call graph và taint flow graph.

- **Model:** Model nhanh.

- **Tools:** Graphify, regex/grep search.

---

## 2. Repo Builder

- **Trách nhiệm:** Phân tích cấu hình dự án để thiết lập môi trường Docker khép kín, cài đặt các phụ thuộc theo từng ngôn ngữ, kích hoạt các chế độ giám sát/debug runtime như Sanitizer cho C/C++, Race detector cho Go, hoặc chế độ Debug/Trace cho Python/Node.js và tự động xử lý lỗi build để ứng dụng thực thi được.

- **Input:** Mã nguồn dự án, tập tin hướng dẫn build.

- **Output:** Ứng dụng/nhị phân sẵn sàng chạy kèm công cụ giám sát runtime. Kịch bản build tối ưu (`build.sh`) và danh sách gói phụ thuộc (`packages.txt`).

- **Model:** Model trung bình

- **Tools:** Shell code.

---

## 3. Coordinator

- **Trách nhiệm:** Quản lý vòng đời kiểm thử, phân bổ nhiệm vụ tuần tự đến các tác tử chuyên trách, lưu trữ ngữ cảnh giữa các pha, điều khiển vòng lặp phản hồi và kích hoạt cơ chế replan khi gặp bế tắc. Đảm nhiệm vai trò đánh giá threat model: xếp hạng ứng viên dựa trên bề mặt tấn công ngoài và thẩm định kết quả từ PoV Verifier dựa trên ranh giới tin cậy nhằm loại bỏ các lỗi vô hại trước khi xuất báo cáo.

- **Input:** Báo cáo trạng thái và dữ liệu từ các tác tử con.

- **Output:** Chỉ thị thực thi nhiệm vụ, quyết định chuyển pha và báo cáo lỗ hổng đã thẩm định.

- **Model:** Model mạnh.

- **Tools:** Quản lý trạng thái, context summarizer.

---

## 4. Vuln Analysis

- **Trách nhiệm:** Đọc sâu mã nguồn các hàm trên đường dẫn nghi vấn, phân tích ngữ nghĩa để xác định nguyên nhân gốc rễ, đối chiếu phân loại CWE và trích xuất các điều kiện rẽ nhánh. Khi gặp các liên kết động hoặc interface bị đứt đoạn, agent chủ động tự tra cứu mã nguồn để nối tiếp và hoàn thiện chuỗi vết.

- **Input:** Đoạn mã nguồn trích xuất từ Repo Retriever và quyền truy cập tra cứu mã nguồn mở rộng khi cần.

- **Output:** Bản đặc tả lỗi gồm vị trí, loại CWE, chuỗi vết hoàn chỉnh và các điều kiện kích hoạt.

- **Model:** Model mạnh.

- **Tools:** Trích xuất mã nguồn, code viewer, grep/symbol lookup.

---

## 5. PoV Generator

- **Trách nhiệm:** Thiết kế dữ liệu đầu vào và tạo kịch bản kiểm thử tự động thỏa mãn các điều kiện từ Vuln Analysis nhằm kích hoạt trạng thái lỗi, đồng thời tinh chỉnh kịch bản dựa trên phản hồi của PoV Verifier.

- **Input:** Bản đặc tả lỗi và các điều kiện kích hoạt từ Vuln Analysis.

- **Output:** Kịch bản kiểm thử tự động và tập tin dữ liệu đầu vào.

- **Model:** Model mạnh.

- **Tools:** Trình sinh mã, shell/python execution.

---

## 6. PoV Verifier

- **Trách nhiệm:** Thực thi kịch bản kiểm thử trong môi trường Docker do Repo Builder thiết lập, giám sát mã thoát, bắt log crash hoặc lỗi runtime và phản hồi lại cho PoV Generator nếu kịch bản chưa kích hoạt thành công.

- **Input:** Kịch bản kiểm thử từ PoV Generator và môi trường Docker từ Repo Builder.

- **Output:** Kết quả thẩm định kèm log lỗi runtime.

- **Model:** Model nhanh / trung bình.

- **Tools:** Docker execution, log parser.

---

# Phân tích

---

## 1. Điểm mạnh

- **No endpoint file & trajectory drift:**
  - *Lỗi được chỉ ra trong VulnGym:* Khi đánh giá các coding agent ở cấp độ repository, có tới **26.8% ca thất bại ở tác vụ End-to-End là do không tìm được bất kỳ file nào chứa entry point hoặc critical operation**. Agent duyệt mã tự do bằng các lệnh rời rạc thường bị trajectory drift qua hàng chục turn và nhanh chóng tràn cửa sổ ngữ cảnh.
  
  - *Khắc phục:* Tác tử `Repo Retriever` sử dụng `Graphify` để tự động bóc tách toàn diện call graph và taint flow graph thô. Việc này cô lập không gian tìm kiếm rộng lớn thành các Candidate Slices, chuyển giao trực tiếp cho `Vuln Analysis` mà không để agent tự mò mẫm thủ công.

- **Over-classification & over-analyzing patched guards:**
  - *Lỗi được chỉ ra trong Yildiz et al.:* LLM và agent có xu hướng đoán bừa mã lành thành có lỗ hổng, khiến pairwise accuracy thấp hơn cả đoán ngẫu nhiên. Khi gặp mã đã vá, LLM thường suy đoán vô căn cứ rằng rào chắn bảo vệ bị vượt qua, gây ra lượng lớn cảnh báo giả.
  
  - *Khắc phục:* Tách biệt phân tích giả thuyết khỏi kiểm chứng thực thi. Mọi nghi vấn buộc phải chạy thực tế trong Docker của `Repo Builder`; nếu không kích hoạt được crash hay vi phạm runtime, cảnh báo giả sẽ bị loại bỏ hoàn toàn.

- **Trace incompleteness & static blindness:**
  - *Lỗi được chỉ ra trong VulnGym & IRIS:* Phân tích tĩnh truyền thống thường bị mù trước các liên kết động, làm đứt gãy đường truyền và khiến tỷ lệ khớp chuỗi vết trung gian chỉ đạt 14–26%.
  
  - *Khắc phục:* `Vuln Analysis` được cấp công cụ tự tra cứu để chủ động tìm kiếm mã nguồn cục bộ, nối lại các liên kết động bị đứt đoạn mà không cần qua các khâu điều phối trung gian.

- **Prohibitive token cost & budget exhaustion:**
  - *Lỗi được chỉ ra trong Revelio & Wen et al.:* Dùng một model mạnh duy nhất chạy toàn bộ vòng lặp ReAct tốn tới $10–$20/repo và dễ cạn quota ngữ cảnh trước khi tìm thấy mã lỗi.
  
  - *Khắc phục:* Phân cấp mô hình theo chi phí: dùng model nhẹ cho quét đồ thị, model vừa cho dựng môi trường, và chỉ dùng model mạnh cho phân tích logic và sinh kịch bản kiểm thử.

- **C/C++ scope lock:**
  - *Hạn chế của Revelio:* Revelio chứng minh PoV hiệu quả nhưng bị giới hạn trong phạm vi Memory Safety trên C/C++ với AddressSanitizer/UBSan.
  
  - *Khắc phục:* Thiết kế `Repo Builder` đa ngôn ngữ, tự động nhận diện hệ sinh thái dự án, cấu hình môi trường và chèn công cụ giám sát runtime tương ứng.

- **AI-powered triage & threat model:**
  - *Hạn chế của Revelio:* Crash tái hiện được chưa chắc đã là lỗ hổng bảo mật nếu nằm ngoài ranh giới tin cậy, Revelio phải lọc thủ công bằng con người.
  
  - *Khắc phục:* `Coordinator` tự động đối chiếu kết quả crash với mô hình hiểm họa của dự án và ranh giới tin cậy, loại bỏ các lỗi DoS nội bộ hoặc assertion vô hại trước khi xuất báo cáo.

---

## 2. Điểm yếu

- **Dynamic tracing & search budget:** Khi gặp framework phân tán qua quá nhiều tầng trừu tượng, việc `Vuln Analysis` mở rộng tra cứu liên tục có thể làm tăng số lượt gọi tool và tốn ngữ cảnh nếu không giới hạn độ sâu hợp lý.

- **Path constraint solving bottleneck:** Việc kích hoạt lỗi đòi hỏi input vượt qua các điều kiện rẽ nhánh phức tạp. Dù ReAct agent có thể lặp để thử sai, việc giải các ràng buộc cứng mà thiếu công cụ chuyên dụng vẫn là một rào cản.

- **Feedback loop latency:** Vòng lặp giữa `Coordinator` <-> `Vuln Analysis` và `PoV Generator` <-> `PoV Verifier` có thể kéo dài thời gian xử lý nếu test liên tục thất bại, đòi hỏi phải có ngưỡng dừng (turn limit) chặt chẽ.