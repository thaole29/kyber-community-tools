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
   - Reports: `daily_report.py`, `three_day_report.py`, `weekly_report.py`, `fourteen_day_report.py`, `three_week_report.py` — đọc DB, build báo cáo, post lên Telegram (`TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID`). `range_report.py` là CLI legacy đọc `ticket_analytics.csv` và in stdout — chưa migrate sang DB.

2. **Crypto News**
   - `crypto_news_crawler.py` — Dùng Playwright (cookie từ `x_user_data/`) crawl các tài khoản trong `config.json` (WatcherGuru, unusual_whales, ...), lọc theo `keywords` và `min_views_for_news`, ghi `crypto_news_report.md`.
   - `telegram_notifier.py` — Gửi `crypto_news_report.md` lên Telegram group.

3. **Community Digest** (Section 2 spec v2)
   - `community_digest.py` — Daily script chạy bằng cron. Connect Discord read-only, fetch 24h messages từ `config.COMMUNITY_CHANNELS` (tên có emoji prefix, vd `🌎general-english`), anonymize tác giả (user_N), gọi **Google Gemini 2.5 Flash** (`config.GEMINI_MODEL`, default `gemini-2.5-flash`, free tier) để summarize từng channel ra JSON, format Telegram digest HTML và post lên staff group. Lưu raw JSON vào table `community_digests` cho weekly rollup.
   - `community_weekly.py` — Weekly rollup chạy Monday. Đọc 7 ngày từ `community_digests`, gọi Gemini lần thứ hai để aggregate trending topics / recurring complaints / sentiment trend / contributor volume, post Telegram.
   - Gemini config: `thinking_budget=0` (model 2.5 mặc định budget 1k+ thinking tokens và sẽ ăn vào `max_output_tokens` → JSON bị truncate). `response_mime_type="application/json"` đảm bảo output JSON không có code-fence wrap. `LLM_MAX_TOKENS=4000` cho daily, `WEEKLY_MAX_TOKENS=3000` cho weekly.
   - Privacy: prompt cấm đưa username; tác giả map sang `user_1, user_2…` trước khi gửi LLM; output dùng counts thay vì tên.

`daily_update.sh` là cron entrypoint chạy crawler → telegram → daily_report theo thứ tự.

### Cron schedule (spec v2 §3, all UTC)

Khuyến nghị các entry tách biệt cho timing chính xác. macOS cron chạy theo local TZ, nên đặt `CRON_TZ=UTC` ở đầu crontab hoặc dùng `launchd` plist.

```cron
CRON_TZ=UTC
# 00:00 daily — support team report
0  0 * * * cd "/Volumes/Macintosh HD - Data/Project" && venv/bin/python daily_report.py     >> logs/daily.log 2>&1
# 00:05 daily — community digest (LLM)
5  0 * * * cd "/Volumes/Macintosh HD - Data/Project" && venv/bin/python community_digest.py >> logs/community.log 2>&1
# 00:00 Mon — weekly support summary
0  0 * * 1 cd "/Volumes/Macintosh HD - Data/Project" && venv/bin/python weekly_report.py    >> logs/weekly.log 2>&1
# 00:10 Mon — weekly community rollup
10 0 * * 1 cd "/Volumes/Macintosh HD - Data/Project" && venv/bin/python community_weekly.py >> logs/community_weekly.log 2>&1
```

`bot.py` là long-running (Discord listener + SLA loop 5 phút) — chạy riêng qua `launchd`/`supervisor`/`tmux`, không qua cron.

## Configuration

`config.py` là single source of truth — load `.env` và export mọi hằng số. KHÔNG đọc env trực tiếp ở module khác.

Quan trọng:
- `SHIFTS` — A/B/C, full 24h coverage. Shift C (TerrorMichael 17:00–02:00 UTC) **cross midnight**; logic on-duty trong `get_on_duty_agent` xử lý case này — đừng đơn giản hoá thành `start <= h < end`.
- `AGENT_MAPPING` & `AGENT_DISCORD_IDS` — chuẩn hoá tên agent. Luôn gọi `config.normalize_agent()` trước khi so sánh hoặc lưu DB; tên hiển thị Discord không ổn định.
- `TICKET_TOOL_BOT_ID = 557628352828014614` — bot này được xử lý đặc biệt (không bị filter như bot khác) để parse owner và closer từ message của Ticket Tool.
- `TZ_OFFSET = +7h` (LOCAL_TZ). DB lưu ISO format **UTC**; chỉ convert sang local khi format report.
- `SLA_FRT_THRESHOLD_MINS = 30`, `SLA_RESOLUTION_THRESHOLD_MINS = 1440`.

`.env` chứa: `DISCORD_TOKEN`, `TELEGRAM_BOT_TOKEN` (legacy alias `TELEGRAM_TOKEN` vẫn nhận), `TELEGRAM_CHAT_ID` (format `id` hoặc `id/threadId`), `GEMINI_API_KEY` (cho community digest Section 2 — lấy free tại https://aistudio.google.com), optional `GEMINI_MODEL` override, `ANTHROPIC_API_KEY` legacy unused. **Không commit.**

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

- Production reports (`bot.py` SLA alert, `daily_report.py`, `weekly_report.py`) gửi Telegram **HTML** mode + dùng `config.html_escape()` cho nội dung động. Ad-hoc reports (`three_day_report.py`, `fourteen_day_report.py`, `three_week_report.py`) còn Markdown — chạy CLI thủ công, chưa convert vì không nằm trên cron.
- Có nhiều report scripts trùng cấu trúc (`three_day_report.py`, `weekly_report.py`, ...) — sửa logic chung thì nhớ áp cho cả nhóm.
- File CSV cũ (`ticket_analytics*.csv`, `*.xlsx`) là dữ liệu legacy trước khi migrate sang SQLite; giữ làm backup, không phải input runtime.
- `debug_transcripts.py`, `find_data_everywhere.py`, `find_transcripts.py`, `inspect_one.py`, `save_sample.py`, `verify.py` là các script khám phá / debug một lần — không phải production.
