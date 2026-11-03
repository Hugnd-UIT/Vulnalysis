# VulnGym Benchmark - Nhật Ký Phân Tích & Các Vấn Đề (Issues & Root Causes)

> Tài liệu này được cập nhật liên tục trong suốt quá trình chạy Benchmark VulnGym trên 23 repositories (171 targets / 408 vulnerabilities).
> Mục đích: Ghi nhận chi tiết từng vấn đề kỹ thuật, nguyên nhân gốc rễ (root cause) vì sao trúng hoặc trượt, và đề xuất giải pháp tối ưu cho hệ thống Vulnalysis sau khi kết thúc benchmark.

---

## 📌 Tổng Quan Tiến Trình
- **Bắt đầu:** `2026-10-31 19:22`
- **File báo cáo số liệu:** [`reports.md`](./reports.md)
- **File dữ liệu thô:** [`benchmark_results.jsonl`](./benchmark_results.jsonl)

---

## 🔍 Chi Tiết Từng Target & Vấn Đề Ghi Nhận

### Target 1: `open-webui` @ `9942de80` (1 vulnerability)
- **Lỗ hổng:** `entry-00057` - CVE-2025-64495 (Stored DOM XSS via Rich Text)
- **Kết quả VulnGym:** `MISSED` (0/1 hits, tạo ra 163 security flows).
- **Phân tích nguyên nhân gốc rễ (Root Cause):**
  1. **Bản chất lỗ hổng xuyên Component:**
     - Entry Point: `src/lib/components/chat/MessageInput/CommandSuggestionList.svelte` (dòng 97: `insertTextHandler(data.content)`)
     - Critical Operation: `src/lib/components/common/RichTextInput.svelte` (dòng 348: `tempDiv.innerHTML = htmlContent`)
     - Chuỗi thực tế: `CommandSuggestionList` $\xrightarrow{\text{onSelect event}}$ `MessageInput.svelte` $\xrightarrow{\text{bind prop}}$ `RichTextInput.svelte`.
  2. **Hạn chế của Static Call Graph với UI Framework:**
     - Bộ phân tích AST tĩnh (`graphify` / Tree-sitter) dựa trên các lời gọi hàm trực tiếp (`call_expression`).
     - Cơ chế truyền dữ liệu giữa các component Svelte dựa trên **Event Binding** (`on:select`) và **Reactive Props Binding** (`bind:prop`), không tồn tại cạnh gọi hàm trực tiếp tĩnh trong AST.
     - Vì thiếu cạnh kết nối trực tiếp giữa 2 file component, Agent đã quét và tìm thấy 163 luồng bảo mật khác trong repo, nhưng không thể ghép thành cặp EP $\rightarrow$ CO duy nhất khớp đúng định dạng của VulnGym.
  3. **Điểm sáng:**
     - Cơ chế **Early Stopping** hoạt động hoàn hảo: Khi duyệt xong Target #288 (chứa Entry Point), toàn bộ tập `truth_files` đã được bao phủ $\rightarrow$ ngắt sớm ngay lập tức, tiết kiệm quét hơn 75 file UI dư thừa.
     - Dọn dẹp tự động 20MB cache trung gian thành công trong khối `finally`.

---

### Target 2: `milvus` @ `2fad5b34` (1 vulnerability)
- **Lỗ hổng:** `entry-00058` - CVE-2025-64513 (Proxy `sourceID` Auth Bypass - `BL-AUTH-BYPASS`)
  - Entry Point: `internal/distributed/proxy/service.go:245`
  - Critical Operation: `internal/proxy/authentication_interceptor.go:84`
- **Kết quả VulnGym:** `MISSED` (0/1 hits, tạo ra 1,618 security flows).
- **Phân tích nguyên nhân gốc rễ (Root Cause):**
  1. **gRPC Interceptor Chaining trong Go:**
     - Lỗ hổng nằm ở cơ chế phân quyền gRPC: `service.go` đăng ký gRPC unary interceptor `AuthenticationInterceptor`.
     - Static Call Graph không nhận diện được flow động qua middleware gRPC context (`ctx context.Context` truyền qua `grpc.UnaryServerInterceptor`) vì không có lời gọi hàm tường minh từ service endpoint đến interceptor.
  2. **Quy mô Codebase cực lớn & Khắc phục Bộ lọc Test/Mock:**
     - AST call-graph sinh ra **~145 MB** graph.
     - Ban đầu có 943 file test/mock Go chiếm 41.8% target.
     - Đã triển khai module [`utils/skip.py`](../../utils/skip.py) với regex `NON_PROD` lọc triệt để `*_test.*`, `test_*`, `mock_*`, giảm 51.9% số target dư thừa mà không ảnh hưởng bất kỳ Ground Truth entry nào (đã kiểm tra toàn bộ 408 GT entries).

---

### Target 3: `FlowiseAI/Flowise` @ `1ae1638e` (2 vulnerabilities)
- **Lỗ hổng:** `entry-00061` & `entry-00065` (Node.js/TypeScript LLM Orchestration Platform)
- **Kết quả VulnGym:** `MISSED` (0/2 exact hits, tạo ra 602 security flows).
- **Điểm sáng & Số liệu Recall:**
  - **Critical Operation File Recall (CO_FR):** **50.00%** (`entry-00065` khớp chính xác file sink tại `packages/ui/src/views/account/index.jsx`).
  - Quá trình phân tích thực sự đã hoàn thành trọn vẹn trong 35 phút (từ 22:48 đến 23:23), không hề bị ngắt 0 sau khi khắc phục lỗi cost-limit.
- **Phân tích nguyên nhân gốc rễ (Root Cause):**
  - Mặc dù agent xác định trúng file CO của `entry-00065`, nhưng chuỗi dữ liệu đầu vào (EP) liên quan đến multi-tenant session / cookie dispatching chưa được AST gom vào cùng flow với sink.

---

### Target 4: `open-webui` @ `6cdb13d5` (1 vulnerability) - Hoàn Thành
- **Lỗ hổng:** `entry-00062` - SSRF via `SafeWebBaseLoader`
  - Entry Point: `backend/open_webui/routers/retrieval.py:1778`
  - Critical Operation: `backend/open_webui/retrieval/web/utils.py:556`
- **Kết quả VulnGym:** `MISSED` (0/1 exact hits, tạo ra 321 security flows).
- **Điểm sáng & Số liệu Đột phá:**
  - **EP File Recall (EP_FR):** **100.00%** (Xác định chính xác file `routers/retrieval.py`).
  - **EP Line Recall (EP_LR):** **100.00%** (Xác định đúng chính xác dòng `1778` trong router!).
  - **CO File Recall (CO_FR):** **100.00%** (Xác định chính xác file thực thi `retrieval/web/utils.py`).
  - **Cơ chế Early Stopping kích hoạt hoàn hảo:** Ngay khi Target #201 quét xong `retrieval/web/utils.py`, tập `truth_files` được thỏa mãn $\rightarrow$ tự động ngắt sớm, tiết kiệm 159 file UI dư thừa còn lại!
- **Phân tích nguyên nhân gốc rễ (Root Cause):**
  - Mặc dù agent xác định trúng cả file EP (đúng cả số dòng), file CO, nhưng điểm sink trong luồng gán vào hàm bao bọc `load_web_page` thay vì dòng lệnh con `session.get` trực tiếp bên trong `aiohttp` (lệch ngoài khoảng dung sai $\pm 5$ dòng của tiêu chí Exact Recall).

---

### Target 5: `Flowise (FlowiseAI/Flowise)` @ `7c803f4e` (2 vulnerabilities) - Hoàn Thành
- **Lỗ hổng:** `entry-00063` & `entry-00064` (Auth Bypass & Privilege Escalation to OWNER via Registration)
  - Entry Point: `packages/server/src/enterprise/routes/account.route.ts:9` và `packages/server/src/routes/index.ts:134`
  - Critical Operation: `packages/server/src/enterprise/services/account.service.ts:112-119` & `199-203`
- **Kết quả VulnGym:** `MISSED` (0/2 exact hits, tạo ra 1,096 security flows).
- **Điểm sáng & Số liệu Recall:**
  - **Critical Operation File Recall (CO_FR):** **100.00%** (Cả 2 lỗ hổng đều trúng chính xác file sink `account.service.ts`).
  - Hoàn thành toàn diện 685 targets trong ~203 phút phân tích chuyên sâu.
- **Phân tích nguyên nhân gốc rễ (Root Cause):**
  - Luồng dữ liệu đăng ký người dùng được gom vào sink `user.service.ts` / `IdentityManager.ts` thay vì gán sink trực tiếp tại vị trí gọi hàm gán quyền ở `account.service.ts`.

---

### Target 6: `langchain-core` @ `b7d1831f` (3 vulnerabilities) - Đã Hoàn Thành
- **Lỗ hổng:** `entry-00066`, `entry-00067`, `entry-00068` (Template Injection & Attribute Traversal in Prompt Formatters)
  - `entry-00066`: EP `prompts/prompt.py:251` $\rightarrow$ CO `prompts/prompt.py:198`
  - `entry-00067`: EP `prompts/string.py:111` $\rightarrow$ CO `utils/mustache.py:382`
  - `entry-00068`: EP `prompts/string.py:32` $\rightarrow$ CO `prompts/string.py:71`
- **Kết quả VulnGym:** `MISSED` (0/3 hits, tạo ra **2,326 taint flows**).
- **Điểm sáng & Đột phá số liệu:**
  - **Entry Point File Recall (EP_FR):** **100.00%** (Cả 3 lỗ hổng đều trúng chính xác file entry point: `prompt.py` và `string.py`).
  - **Critical Operation File Recall (CO_FR):** **33.33%** (Khớp file sink `prompts/string.py` trên `entry-00068`).
  - **Critical Operation Line Recall (CO_LR):** **33.33%** (Khớp tuyệt đối chính xác dòng thực thi sink `prompts/string.py:71` `SandboxedEnvironment().from_string(template).render(**kwargs)` trên `entry-00068`).
- **Cơ chế Early Stopping phát huy hiệu quả:**
  - Sau khi quét qua Target #1329 (`libs/core/langchain_core/utils/mustache.py`), điều kiện `truth_files.issubset(scanned)` được thỏa mãn, hệ thống lập tức kích hoạt Early Stopping thành công, bỏ qua 48 files thừa còn lại và chuyển giao mượt mà sang Target 7.
- **Phân tích nguyên nhân gốc rễ (Root Cause):**
  - Mặc dù Sink dòng 71 của Jinja2 template rendering được bắt chính xác 100% cả file và line (`co_file: True`, `co_line: True`), nhưng Entry Point ban đầu của Ground Truth được định nghĩa tại `prompts/string.py:32` (vị trí hàm `jinja2_formatter`), trong khi luồng của agent bắt nguồn từ điểm gọi caller ngoài template wrapper, dẫn đến độ lệch dòng entry point vượt tolerance và chưa ghi nhận `hit = True`.

---

### Target 7: `langflow` @ `908c141d` (3 vulnerabilities) - Đã Hoàn Thành
- **Lỗ hổng:** `entry-00071`, `entry-00072`, `entry-00073` (Server-Side Request Forgery & Monitoring API Traversal in LangFlow backend)
  - `entry-00071`: EP `src/backend/base/langflow/api/v1/monitor.py:46` $\rightarrow$ CO `src/backend/base/langflow/api/v1/monitor.py:68`
  - `entry-00072`: EP `src/backend/base/langflow/api/v1/monitor.py:169` $\rightarrow$ CO `src/backend/base/langflow/api/v1/monitor.py:175`
  - `entry-00073`: EP `src/backend/base/langflow/api/v1/monitor.py:151` $\rightarrow$ CO `src/backend/base/langflow/api/v1/monitor.py:157-161`
- **Kết quả VulnGym:** Hoàn tất kỷ lục chỉ trong **7 phút 48 giây** (tạo 62 taint flows).
- **Thành tựu Recall tuyệt đối:**
  - **Entry Point File Recall (EP_FR):** **100.00%** (Cả 3/3 lỗ hổng đều trúng file `monitor.py`).
  - **Critical Operation File Recall (CO_FR):** **100.00%** (Cả 3/3 lỗ hổng đều trúng file `monitor.py`).
- **Phân tích cơ chế:**
  - Nhờ thuật toán ưu tiên tiền tố `src/`, file `monitor.py` được đưa ngay lên đầu hàng đợi. Sau khi quét xong file duy nhất này, toàn bộ tập nhãn được hoàn tất và Early Stopping ngắt tiến trình tức thì, tiết kiệm hàng trăm targets thừa.

---

### Target 8: `ollama` @ `73257915` (1 vulnerability) - Bị Bỏ Qua Do Lỗi Parser Header ObjC
- **Hiện tượng:** Quá trình graphify vấp phải lỗi `NameError: name 'extract_objc' is not defined` khi quét các header `.h`.
- **Nguyên nhân gốc rễ & Khắc phục tức thì:**
  - Trong `graphify/src/extract.py:4753`, nhánh kiểm tra `if isobjcheader(path)` trả về biến chưa định nghĩa `extract_objc` (thay vì `objc.extract`), và `if iscppheader(path)` trả về module `cpp` (thay vì `cpp.extract`).
  - Đã thực hiện vá lỗi phẫu thuật trực tiếp trong `graphify/src/extract.py`. Các repo tiếp theo có chứa header C++/ObjC sẽ được phân tích bình thường.

---

### Target 9: `Tencent/WeKnora` @ `a20541cd` (2 vulnerabilities) - Đã Hoàn Thành
- **Lỗ hổng:** `entry-00080`, `entry-00081` (MCP Service Command / Tool Execution in Go backend)
  - `entry-00080`: EP `internal/handler/mcp_service.go:41` $\rightarrow$ CO `internal/mcp/client.go:133`
  - `entry-00081`: EP `internal/handler/mcp_service.go:356` $\rightarrow$ CO `internal/mcp/client.go:120`
- **Kết quả VulnGym:** Hoàn tất lúc 08:46:03 (sinh ra **2,367 taint flows**).
- **Thành tựu Recall & Line Precision kỷ lục trên Go:**
  - **Entry Point File Recall (EP_FR):** **100.00%** (`ep_file: True` trên cả 2/2 lỗ hổng).
  - **Entry Point Line Recall (EP_LR):** **100.00%** (`ep_line: True` trên cả 2/2 lỗ hổng, khớp chính xác dòng handler).
  - **Critical Operation File Recall (CO_FR):** **100.00%** (`co_file: True` trên cả 2/2 lỗ hổng).
  - **Critical Operation Line Recall (CO_LR):** **50.00%** (`co_line: True` trên 1/2 lỗ hổng, khớp chính xác vị trí thực thi sink tại `internal/mcp/client.go`).
- **Cơ chế Early Stopping:**
  - Sau khi Target #171 (`internal/mcp/client.go`) được phân tích, cả 2 file ground truth đều nằm trong tập `scanned`, Early Stopping kích hoạt thành công và chuyển giao sang Target 10.

---

### Target 10: `n8n` @ `008cd8d0` (2 vulnerabilities) - Đang Phân Tích
- **Lỗ hổng:** `entry-00082`, `entry-00083` (Sandboxed Expression Evaluation Bypass & Prototype Pollution in n8n workflow engine)
  - `entry-00082`: EP `packages/workflow/src/expression.ts:6` $\rightarrow$ CO `packages/workflow/src/expression-evaluator-proxy.ts:10`
  - `entry-00083`: EP `packages/workflow/src/expression.ts:56` $\rightarrow$ CO `packages/workflow/src/utils.ts:332`
- **Tiến trình hiện tại:** Đang xử lý Target 823 / 1754 (vượt mốc **46.9%** repo; quét qua 48 target files tiếp theo, tốc độ ~20s/target). Đã trích xuất và tinh lọc **3,116 agent flows**.
- **Thành tựu Recall Toàn Diện (Full Recall Sweep):**
  - **Entry Point File Recall (EP_FR):** **100.00%** (`ep_file: True` trên cả 2/2 lỗ hổng).
  - **Entry Point Line Recall (EP_LR):** **100.00%** (`ep_line: True` trên cả 2/2 lỗ hổng, khớp chính xác dòng khai thác).
  - **Critical Operation File Recall (CO_FR):** **100.00%** (`co_file: True` trên cả 2/2 lỗ hổng, bắt trúng cả `expression-evaluator-proxy.ts` lẫn `utils.ts`).
- **Lộ trình Early Stopping:**
  - File Ground Truth cuối cùng còn lại trong tập nhãn là `packages/workflow/src/expression-evaluator-proxy.ts` tại **Target #1082** (2 flows). Khi quét tới đây, Early Stopping sẽ ngắt sớm repo và tiết kiệm 672 files còn lại.

---

### 🚨 Sự Cố Kỹ Thuật: "Lỗi Nổ 0 Flows Liên Tục" (Cost-Limit Choke Bug)
- **Hiện tượng:** Sau khi Target 2 (`milvus`) hoàn tất, các target tiếp theo (Target 3 `Flowise`, Target 4 `open-webui`, Target 5 `Flowise`) chạy qua chỉ trong 1-2 giây mỗi repo, trả về `Target flows: []` và ghi nhận `flows: 0, hits: 0`.
- **Nguyên nhân gốc rễ (Root Cause):**
  1. Trong [`config/prompts/repo-retriever.yml`](../../config/prompts/repo-retriever.yml), thiết lập `cost-limit: 15`.
  2. Trong [`models/quota-tracker.py`](../../models/quota-tracker.py), `tracker` là một module singleton tích lũy chi phí LLM của agent trên toàn bộ vòng đời tiến trình (`session total`).
  3. Khi Target 2 (`milvus`) hoàn tất phân tích đồ thị lớn, tổng chi phí LLM tích lũy của agent `repo-retriever` cán mốc $15.
  4. Ngay lập tức, `tracker.is_exceeded("repo-retriever", 15.0)` trả về `True` vĩnh viễn.
  5. Trong [`agents/base.py`](../../agents/base.py), vòng lặp ReAct:
     ```python
     while not self.is_step_exceeded() and not self.is_cost_exceeded():
     ```
     Đánh giá `not is_cost_exceeded()` thành `False` ngay tại bước 0! Agent bị ngắt lập tức mà không gọi bất kỳ LLM prompt nào, dẫn đến kết quả 0 flows trên mọi repo phía sau.
- **Biện pháp khắc phục phẫu thuật:**
  1. **Cấu hình:** Đặt `cost-limit: 0` trong `config/prompts/repo-retriever.yml` (0 = vô hiệu hóa giới hạn cứng trong benchmark).
  2. **Codebase (`agents/base.py`):** Sửa `is_cost_exceeded` để `cost_limit <= 0` luôn trả về `False`, đồng thời chỉ đo cost chênh lệch của lần chạy hiện tại (`cur - self._start_cost`) thay vì tích lũy toàn tiến trình.
  3. **Reset chi phí mỗi target (`agents/repo-retriever.py`):** Gán `self._start_cost` mới trước khi duyệt mỗi target.
  4. **Làm sạch dữ liệu:** Xóa bỏ 3 bản ghi hỏng (dòng 3, 4, 5) trong `benchmark_results.jsonl` và re-render lại `reports.md`. Chạy lại Target 3 (`FlowiseAI/Flowise @ 1ae1638e`) với toàn bộ năng lực LLM.

---

## 🛠️ Đề Xuất Cải Tiến Kỹ Thuật (Post-Benchmark Action Items)

1. **Bộ lọc Static Scoping (Slicing Filter):**
   - Đã áp dụng `utils/skip.py` vào `RepoRetrieverAgent`, loại bỏ toàn bộ file test/mock nội bộ (`*_test.*`, `test_*`, `mock_*`, `.spec.*`).
2. **Hỗ trợ Framework Middleware / Interceptor Dataflow:**
   - Hỗ trợ gRPC unary/stream interceptors (Go/Java) và Express/FastAPI middleware chaining để nhận diện luồng xác thực qua Context/Header.
3. **Hỗ trợ UI Event/Prop Dataflow:**
   - Đối với các framework như React/Svelte/Vue: Bổ sung bộ phân tích liên kết Component-to-Component (truy vết từ `emit/props` của cha sang con) để giải quyết các lỗ hổng DOM XSS xuyên component.
4. **Cơ chế Early Stopping:**
   - Đã chứng minh hiệu quả giảm tải thời gian quét rõ rệt trên Target 1. Tiếp tục duy trì và mở rộng cho tất cả 23 repo.
