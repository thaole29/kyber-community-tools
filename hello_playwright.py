import asyncio
from playwright.async_api import async_playwright

async def main():
    print("HELLO PLAYWRIGHT")
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        print(f"BROWSER LAUNCHED: {browser.version}")
        await browser.close()
    print("DONE")

if __name__ == "__main__":
    asyncio.run(main())
