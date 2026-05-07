# Support Analytics & Crypto News Project

Tổng hợp công cụ Python cho team support: Discord bot theo dõi ticket lifecycle, báo cáo định kỳ ra Discord/Telegram, và crawler tin tức crypto từ X (Twitter).

## Architecture

Single-flat-package project (không có `src/` hay sub-package). Mọi module nằm cùng thư mục root và import lẫn nhau qua `import config`, `import database`.

Hai trục chính:

1. **Ticket Analytics**
   - `bot.py` — Discord bot live; lắng nghe `on_guild_channel_create/update/delete` và `on_message` để ghi nhận ticket lifecycle. Có `sla_check_loop` (chu kỳ 5 phút) gửi cảnh báo Telegram khi FRT vượt ngưỡng.
   - `database.py` — Lớp truy cập SQLite (`tickets.db`); schema gồm 1 bảng `tickets` với các cột về thời gian tạo, FRT, agent, on-duty, SLA, cross-shift, deletion.
   - `migrate_csv_to_sqlite.py` — Migration một lần từ `ticket_analytics.csv` sang SQLite.
   - `backfill.py`, `backfill_closed.py` — Quét lịch sử Discord để bù dữ liệu các ticket bot bỏ lỡ.
   - Reports: `daily_report.py`, `three_day_report.py`, `weekly_report.py`, `fourteen_day_report.py`, `three_week_report.py`, `range_report.py` — đọc DB, build báo cáo, post lên Discord webhook (`DAILY_REPORT_WEBHOOK_URL`).

2. **Crypto News**
   - `crypto_news_crawler.py` — Dùng Playwright (cookie từ `x_user_data/`) crawl các tài khoản trong `config.json` (WatcherGuru, unusual_whales, ...), lọc theo `keywords` và `min_views_for_news`, ghi `crypto_news_report.md`.
   - `telegram_notifier.py` — Gửi `crypto_news_report.md` lên Telegram group.

`daily_update.sh` là cron entrypoint chạy crawler → telegram → daily_report theo thứ tự.

## Configuration

`config.py` là single source of truth — load `.env` và export mọi hằng số. KHÔNG đọc env trực tiếp ở module khác.

Quan trọng:
- `SHIFTS` — A/B/C, full 24h coverage. Shift C (TerrorMichael 17:00–02:00 UTC) **cross midnight**; logic on-duty trong `get_on_duty_agent` xử lý case này — đừng đơn giản hoá thành `start <= h < end`.
- `AGENT_MAPPING` & `AGENT_DISCORD_IDS` — chuẩn hoá tên agent. Luôn gọi `config.normalize_agent()` trước khi so sánh hoặc lưu DB; tên hiển thị Discord không ổn định.
- `TICKET_TOOL_BOT_ID = 557628352828014614` — bot này được xử lý đặc biệt (không bị filter như bot khác) để parse owner và closer từ message của Ticket Tool.
- `TZ_OFFSET = +7h` (LOCAL_TZ). DB lưu ISO format **UTC**; chỉ convert sang local khi format report.
- `SLA_FRT_THRESHOLD_MINS = 30`, `SLA_RESOLUTION_THRESHOLD_MINS = 1440`.

`.env` chứa: `DISCORD_TOKEN`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` (format `id` hoặc `id/threadId`), `DAILY_REPORT_WEBHOOK_URL`. **Không commit.**

## Database conventions

- File: `tickets.db` (SQLite, WAL mode).
- Mọi datetime lưu dưới dạng ISO string UTC.
- `record_response` chỉ update khi `first_responded_at IS NULL` — first-response-only, idempotent.
- `close_ticket` không-op nếu đã có `closed_at`.
- Nếu nhận event cho ticket chưa có row (close/delete trước khi bot thấy create), hàm sẽ tạo stub row.
- Thêm cột mới: nhớ update cả `init_db()` schema và `upsert_ticket()` để migration script không bỏ sót.

## Workflow để chạy / phát triển

```bash
source venv/bin/activate
pip install -r requirements.txt        # nếu venv mới
python3 bot.py                          # chạy Discord bot (long-running)
python3 daily_report.py                 # chạy report một lần
bash daily_update.sh                    # full daily cycle
```

`venv/` đã có sẵn trong repo cũ — nếu setup máy mới thì tạo lại.

`x_user_data/` là Playwright user-data dir đã login X — cần cho crypto crawler. Khôi phục bằng `hello_playwright.py` (login thủ công 1 lần).

## Notes / gotchas

- `requirements.txt` liệt kê `asyncio` — đây là stdlib, dòng đó thừa nhưng không gây hại.
- Có nhiều report scripts trùng cấu trúc (`three_day_report.py`, `weekly_report.py`, ...) — sửa logic chung thì nhớ áp cho cả nhóm.
- File CSV cũ (`ticket_analytics*.csv`, `*.xlsx`) là dữ liệu legacy trước khi migrate sang SQLite; giữ làm backup, không phải input runtime.
- `debug_transcripts.py`, `find_data_everywhere.py`, `find_transcripts.py`, `inspect_one.py`, `save_sample.py`, `verify.py` là các script khám phá / debug một lần — không phải production.
