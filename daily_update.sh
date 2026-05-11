#!/bin/bash

# Navigate to the project directory
cd "/Volumes/Macintosh HD - Data/Project"

# Activate the virtual environment
source venv/bin/activate

echo "[DAILY UPDATE] Starting Crypto News Crawl..."
# Run the crawler (unbuffered)
python3 -u crypto_news_crawler.py

echo "[DAILY UPDATE] Sending Telegram Notification..."
# Run the Telegram notifier
python3 -u telegram_notifier.py

echo "[DAILY UPDATE] Sending Support Team Daily Report to Telegram..."
# Run the daily support team report
python3 -u daily_report.py

echo "[DAILY UPDATE] Done. Check your Telegram group and Discord!"
