import os
import requests
import pandas as pd
import config

TOKEN = config.TELEGRAM_TOKEN
CHAT_ID = config.TELEGRAM_CHAT_ID
REPORT_FILE = 'crypto_news_report.md'

def send_telegram_update():
    if not TOKEN or not CHAT_ID:
        print("[ERROR] TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not found in .env")
        return

    if not os.path.exists(REPORT_FILE):
        print(f"[ERROR] Report file {REPORT_FILE} not found.")
        return

    # Handle the "ID/Thread" format (e.g. 3383119533/10)
    chat_id_val = CHAT_ID
    thread_id = None
    if '/' in CHAT_ID:
        parts = CHAT_ID.split('/')
        chat_id_val = parts[0]
        thread_id = parts[1]
    
    if not chat_id_val.startswith('-'):
        chat_id_val = f"-100{chat_id_val}"

    # 1. Build a rich message from the report
    # We'll use the .md file as a source or re-process the data if needed.
    # For now, let's parse the already generated report logic for simplicity.
    with open(REPORT_FILE, 'r') as f:
        content = f.read()

    # Create sections
    sections = content.split('---')
    intro = sections[0].strip() if sections else "📈 **Latest Crypto News (Last 24h)**"
    
    # We will build the message block by block to handle character limits
    current_message = f"{intro}\n\n"
    messages_to_send = []

    for s in sections[1:]:
        s = s.strip()
        if not s: continue
        
        # Format the section for Telegram (Cleaner version of the MD)
        # s looks like: ### User\n> Text...\n- Stats\n- Source
        formatted_section = s.replace('###', '👤 **').replace('\n>', '**\n\n💬 "').replace('...\n', '"\n\n')
        
        # Make the [Quoted] block look better
        formatted_section = formatted_section.replace('[Quoted]:', '\n\n🔁 **Quoted Content:**\n> "')
        if '🔁' in formatted_section:
             formatted_section += '"'
        
        if len(current_message) + len(formatted_section) > 4000:
            messages_to_send.append(current_message)
            current_message = f"🔄 *Continuing report...*\n\n{formatted_section}\n\n"
        else:
            current_message += f"{formatted_section}\n\n"
    
    messages_to_send.append(current_message)

    # 2. Send messages to Telegram
    for msg in messages_to_send:
        print(f"[TELEGRAM] Sending message part (len {len(msg)})...")
        url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id_val,
            "text": msg,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }
        if thread_id:
            payload["message_thread_id"] = thread_id
            
        try:
            r = requests.post(url, json=payload)
            r.raise_for_status()
        except Exception as e:
            print(f"[ERROR] Failed to send message: {e}")

    # 3. Send the file as a backup
    print(f"[TELEGRAM] Sending file {REPORT_FILE}...")
    file_url = f"https://api.telegram.org/bot{TOKEN}/sendDocument"
    files = {"document": open(REPORT_FILE, 'rb')}
    payload = {"chat_id": chat_id_val}
    if thread_id:
        payload["message_thread_id"] = thread_id
    
    try:
        r = requests.post(file_url, data=payload, files=files)
        r.raise_for_status()
        print("[SUCCESS] All updates sent.")
    except Exception as e:
        print(f"[ERROR] Failed to send file: {e}")

if __name__ == "__main__":
    send_telegram_update()
