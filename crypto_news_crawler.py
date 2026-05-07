import asyncio
import json
import os
import pandas as pd
from datetime import datetime, timezone, timedelta
from playwright.async_api import async_playwright
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
CONFIG_FILE = 'config.json'
USER_DATA_DIR = './x_user_data'
REPORT_FILE = 'crypto_news_report.md'
DEBUG_SCREENSHOT = 'debug_last_error.png'

with open(CONFIG_FILE, 'r') as f:
    config = json.load(f)

ACCOUNTS = config.get('accounts', [])
KEYWORDS = config.get('keywords', [])
MAX_TWEETS = config.get('max_tweets_per_search', 20)

async def login(page):
    """Logs into X.com if not already logged in."""
    username = os.getenv('X_USERNAME')
    password = os.getenv('X_PASSWORD')
    
    if not username or not password:
        print("[ERROR] X_USERNAME or X_PASSWORD not found in .env")
        return False

    print(f"[INFO] Attempting login for {username}...")
    await page.goto("https://x.com/login")
    
    try:
        # Check if already logged in
        if "login" not in page.url and "x.com/home" in page.url:
            print("[INFO] Already logged in.")
            return True

        # Enter username/email/phone
        # Try multiple selectors for the username field
        username_selectors = ['input[name="text"]', 'input[autocomplete="username"]', 'input[type="text"]']
        username_found = False
        for selector in username_selectors:
            try:
                await page.wait_for_selector(selector, timeout=10000)
                await page.fill(selector, username)
                username_found = True
                break
            except:
                continue
        
        if not username_found:
            print("[ERROR] Could not find username field.")
            return False

        await page.keyboard.press("Enter")
        await page.wait_for_timeout(3000)

        # Sometimes it asks for phone or email if the account is flagged
        if await page.query_selector('text="Verify your identity"'):
            print("[WARN] X is asking for identity verification. Cannot proceed automatically.")
            return False

        # Enter password
        password_selectors = ['input[name="password"]', 'input[type="password"]']
        password_found = False
        for selector in password_selectors:
            try:
                await page.wait_for_selector(selector, timeout=10000)
                await page.fill(selector, password)
                password_found = True
                break
            except:
                continue

        if not password_found:
            print("[ERROR] Could not find password field.")
            return False

        await page.keyboard.press("Enter")
        await page.wait_for_timeout(5000)

        if "login" in page.url:
            print("[ERROR] Login failed. Check credentials or CAPTCHA.")
            await page.screenshot(path=DEBUG_SCREENSHOT)
            return False
            
        print("[SUCCESS] Logged in.")
        return True
    except Exception as e:
        print(f"[ERROR] Login process failed: {e}")
        await page.screenshot(path=DEBUG_SCREENSHOT)
        return False

def parse_metric(metric_str):
    """Converts metric strings like '1.2K', '5M' to integers."""
    if not metric_str: return 0
    metric_str = metric_str.lower().replace(',', '')
    if 'k' in metric_str:
        return int(float(metric_str.replace('k', '')) * 1000)
    if 'm' in metric_str:
        return int(float(metric_str.replace('m', '')) * 1000000)
    try:
        return int(metric_str)
    except:
        return 0

async def scrape_tweets(page, query, is_account=False):
    """Scrapes tweets from a search query or an account profile."""
    url = f"https://x.com/{query}" if is_account else f"https://x.com/search?q={query}"
    print(f"[SCRAPE] Navigating to: {url}...")
    await page.goto(url)
    await page.wait_for_timeout(5000)

    tweets_data = []
    
    # Scroll to load more tweets
    for _ in range(2):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(2000)

    tweet_elements = await page.query_selector_all('article[data-testid="tweet"]')
    print(f"[INFO] Found {len(tweet_elements)} tweets for {query}")

    for tweet in tweet_elements[:MAX_TWEETS]:
        try:
            # Extract basic info
            text_el = await tweet.query_selector('div[data-testid="tweetText"]')
            text = await text_el.inner_text() if text_el else ""
            
            # Check for Quoted Tweet
            quote_el = await tweet.query_selector('div[data-testid="quotedTweet"]')
            if quote_el:
                quote_text_el = await quote_el.query_selector('div[data-testid="tweetText"]')
                if quote_text_el:
                    quote_text = await quote_text_el.inner_text()
                    text += f"\n\n[Quoted]: {quote_text}"
                else:
                    # Sometimes the quote is just an image or has a different structure
                    quote_inner = await quote_el.inner_text()
                    text += f"\n\n[Quoted]: {quote_inner[:200]}..."

            user_el = await tweet.query_selector('div[data-testid="User-Name"]')
            user = await user_el.inner_text() if user_el else "Unknown"
            
            # Final Cleaning & Filtering (Skip if only "Source")
            clean_text = text.replace('Source:', '').replace('Source：', '').strip()
            if not clean_text or len(clean_text) < 10:
                # print(f"[DEBUG] Skipping low-info tweet: {text}")
                continue

            data = {"text": text, "user": user, "query": query, "url": url}

            # Extract metrics using the improved selectors
            # Reply
            reply_el = await tweet.query_selector('[data-testid="reply"]')
            if reply_el:
                label = await reply_el.get_attribute("aria-label")
                if label: data["replies"] = parse_metric(label.split(' ')[0])

            # Retweet
            rt_el = await tweet.query_selector('[data-testid="retweet"]')
            if rt_el:
                label = await rt_el.get_attribute("aria-label")
                if label: data["retweets"] = parse_metric(label.split(' ')[0])

            # Like
            like_el = await tweet.query_selector('[data-testid="like"]')
            if like_el:
                label = await like_el.get_attribute("aria-label")
                if label: data["likes"] = parse_metric(label.split(' ')[0])
            
            # Views (Analytics)
            views_el = await tweet.query_selector('a[href*="/analytics"]')
            if views_el:
                views_text = await views_el.get_attribute("aria-label")
                if views_text:
                    data["views"] = parse_metric(views_text.split(' ')[0])

            # Extract Timestamp
            time_el = await tweet.query_selector('time')
            if time_el:
                dt_str = await time_el.get_attribute('datetime')
                if dt_str:
                    dt_str = dt_str.replace('Z', '+00:00')
                    tweet_dt = datetime.fromisoformat(dt_str)
                    data["timestamp"] = tweet_dt.isoformat()
                    
                    # 24h Filter
                    now = datetime.now(timezone.utc)
                    if now - tweet_dt > timedelta(hours=24):
                        continue

            tweets_data.append(data)
        except Exception as e:
            continue

    return tweets_data

async def run_crawl():
    """Main entry point for the crawler, returns the report content."""
    async with async_playwright() as p:
        print("[DEBUG] Launching browser...")
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1280, "height": 720})
        page = await context.new_page()

        print("[DEBUG] Starting login flow...")
        if not await login(page):
            print("[INFO] Proceeding without login (limited results)...")

        all_results = []
        for acc in ACCOUNTS:
            results = await scrape_tweets(page, acc, is_account=True)
            all_results.extend(results)

        for kw in KEYWORDS:
            results = await scrape_tweets(page, kw)
            all_results.extend(results)

        df = pd.DataFrame(all_results)
        if df.empty:
            return "No news found today."

        df['score'] = df.get('likes', 0) + df.get('retweets', 0) * 2 + df.get('views', 0) / 100
        df = df.sort_values(by='score', ascending=False)

        report_content = f"# Crypto News Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        report_content += "## Top Mentioned / Viewed News\n\n"
        
        for _, row in df.head(10).iterrows():
            user_name = row['user'].split('\n')[0]
            report_content += f"### {user_name}\n"
            report_content += f"> {row['text']}\n\n"
            report_content += f"- **Engagement**: {int(row.get('likes',0))} Likes, {int(row.get('retweets',0))} RTs, {int(row.get('views',0))} Views\n"
            report_content += f"- **Source**: [View Tweet]({row['url']})\n\n"
            report_content += "---\n\n"

        with open(REPORT_FILE, 'w') as f:
            f.write(report_content)

        await browser.close()
        return report_content

async def main():
    await run_crawl()
    print("[SUCCESS] Crawl complete.")

if __name__ == "__main__":
    asyncio.run(main())
