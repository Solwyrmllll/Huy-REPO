# Huy-REPO
Prompt mẫu cho Phase 1
[CONTEXT]
Tôi đang xây dựng NIMO AI — một Desktop App dùng Tauri v2 + Python backend + React UI.
Python backend chạy như một subprocess của Tauri, lưu dữ liệu local vào SQLite.
File này là module database.py — khởi tạo tất cả storage khi app khởi động.

[TASK]
Viết file backend/database.py với hàm init_db() thực hiện đúng theo spec sau:
1. Tạo thư mục ~/.nimo_kb/ và ~/.nimo_kb/chroma/ nếu chưa có (dùng os.makedirs exist_ok=True)
2. Connect SQLite tại ~/.nimo_kb/nimo.db
3. Bật WAL mode: PRAGMA journal_mode=WAL và PRAGMA synchronous=NORMAL — phải là dòng đầu tiên
4. Tạo 3 bảng: app_config, usage_stats, chat_history với đúng schema sau: [dán schema vào đây]
5. Tạo index idx_usage_synced trên usage_stats(synced)
6. Tạo file backend/__init__.py rỗng

[CONSTRAINTS]
- KHÔNG tạo bảng api_key_enc hoặc bất kỳ field nào liên quan API Key
- KHÔNG dùng ORM (không SQLAlchemy) — chỉ sqlite3 thuần
- Hàm init_db() phải idempotent — gọi nhiều lần không lỗi (dùng CREATE TABLE IF NOT EXISTS)
- DB path phải dùng os.path.expanduser("~/.nimo_kb/nimo.db")

Chỉ viết code, không giải thích dài dòng.

PHASE 2 — Security Module
Mục tiêu
Module duy nhất xử lý credential trên Client. Chỉ đọc JWT_TOKEN từ SQLite. Không có bất kỳ logic nào liên quan API Key.
Files cần tạo
backend/modules/
  __init__.py
  security.py
Spec chi tiết
# Hàm cần có:
def get_jwt_token() -> str:
    # Đọc jwt_token từ app_config trong SQLite
    # Raise Exception nếu không tìm thấy với message: "JWT Token chưa được cấu hình. Vui lòng liên hệ Admin."
    # DB path: os.path.expanduser("~/.nimo_kb/nimo.db")

def save_jwt_token(token: str) -> None:
    # INSERT OR REPLACE vào app_config key='jwt_token'
    # Dùng khi Admin onboard user lần đầu

def get_user_config() -> dict:
    # Trả về dict: {"jwt_token": str, "plan": str, "user_id": str}
    # Lấy tất cả từ app_config một lần
    # Trả về None cho key nào không có

# KHÔNG CÒN: get_decrypted_api_key(), encrypt_api_key(), FERNET_KEY
Prompt mẫu cho Phase 2
[CONTEXT]
NIMO AI — Desktop App Tauri v2 + Python backend.
Kiến trúc bảo mật: Client KHÔNG giữ OpenRouter API Key. Client chỉ giữ JWT_TOKEN do Admin cấp.
Mọi call đến LLM đều đi qua Railway proxy — Client chỉ gửi JWT_TOKEN để xác thực với Railway.
Phase 1 đã hoàn thành: database.py với init_db() và SQLite schema đã tồn tại.

[TASK]
Viết file backend/modules/security.py với đúng 3 hàm sau:
1. get_jwt_token() -> str: đọc jwt_token từ bảng app_config trong ~/.nimo_kb/nimo.db. Raise Exception với message "JWT Token chưa được cấu hình. Vui lòng liên hệ Admin." nếu không có.
2. save_jwt_token(token: str) -> None: INSERT OR REPLACE vào app_config với key='jwt_token'.
3. get_user_config() -> dict: đọc tất cả config một lần, trả về dict với keys: jwt_token, plan, user_id. Giá trị None nếu key không tồn tại.
Tạo thêm backend/modules/__init__.py rỗng.

[CONSTRAINTS]
- KHÔNG viết bất kỳ hàm nào liên quan đến API Key, Fernet, encryption của API Key
- KHÔNG import cryptography hoặc bất kỳ encryption library nào
- Chỉ dùng sqlite3 thuần, không ORM
- Mỗi hàm tự mở và đóng connection — không dùng global connection

Chỉ viết code, không giải thích dài dòng.

PHASE 3 — Python FastAPI Core
Mục tiêu
Khung FastAPI chạy local trên máy user. Nhận port động từ Rust qua --port. In NIMO_PORT=<port> ra stdout để Rust xác nhận. Có các endpoint cơ bản.
Files cần tạo
backend/
  main.py
  routes/
    __init__.py
    health.py
    system.py
    chat.py      (stub — sẽ implement ở Phase 4)
Spec chi tiết
# Endpoints cần có:
GET  /health          → {"status": "ok", "version": "7.3"}
POST /system/shutdown → flush ChromaDB → stop event loop → {"status": "shutting_down"}
POST /chat/stream     → stub, trả về {"error": "not implemented"} tạm thời

# main.py phải:
# 1. Parse --hwid (required) và --port (required, type=int) từ argparse
# 2. Gọi init_db() từ database.py
# 3. Print f"NIMO_PORT={args.port}" với flush=True — TRƯỚC khi uvicorn.run()
# 4. uvicorn.run(app, host="127.0.0.1", port=args.port)
# 5. Không có default port — required=True
Prompt mẫu cho Phase 3
[CONTEXT]
NIMO AI — Desktop App. Python backend chạy như subprocess của Tauri/Rust.
Rust spawn Python với: nimo-backend --hwid <string> --port <number>
Rust đọc stdout để tìm dòng "NIMO_PORT=<number>" để xác nhận backend đã ready (timeout 10s).
Sau khi ready, Rust gọi http://127.0.0.1:<port>/health để poll trạng thái.
Khi user đóng app, Rust gọi POST http://127.0.0.1:<port>/system/shutdown trước khi kill process.
Phase 1 (database.py) và Phase 2 (security.py) đã hoàn thành.

[TASK]
Viết các files sau:
1. backend/main.py:
   - argparse: --hwid required=True, --port required=True type=int (KHÔNG có default)
   - Gọi init_db()
   - Print f"NIMO_PORT={args.port}" flush=True — dòng này PHẢI xuất hiện trên stdout trước khi uvicorn bắt đầu listen
   - uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="error")
2. backend/routes/health.py: GET /health trả {"status": "ok", "version": "7.3"}
3. backend/routes/system.py: POST /system/shutdown — try ChromaDB PersistentClient flush, sau đó asyncio.get_event_loop().stop(), trả {"status": "shutting_down"}
4. backend/routes/chat.py: POST /chat/stream stub trả {"error": "not implemented yet"}
5. Các file __init__.py cần thiết

[CONSTRAINTS]
- --port KHÔNG có default value, nếu thiếu Python phải exit với error rõ ràng
- Print NIMO_PORT PHẢI có flush=True
- ChromaDB path trong shutdown: ~/.nimo_kb/chroma — dùng try/except nếu chưa khởi tạo
- Không dùng uvicorn reload mode
- CORS: chỉ allow origin http://localhost:1420 (Tauri dev) và tauri://localhost (Tauri prod)

Chỉ viết code, không giải thích dài dòng.