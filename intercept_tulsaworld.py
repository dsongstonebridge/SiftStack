import asyncio
import json
from playwright.async_api import async_playwright

async def main():
    captured = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Capture all network requests
        async def on_request(req):
            url = req.url
            if "cloudfunctions" in url or "firestore" in url or "firebase" in url:
                captured.append({"method": req.method, "url": url, "body": req.post_data})

        async def on_response(resp):
            url = resp.url
            if "cloudfunctions" in url and resp.status == 200:
                try:
                    body = await resp.text()
                    captured.append({"type": "response", "url": url, "status": resp.status, "preview": body[:500]})
                except:
                    pass

        page.on("request", on_request)
        page.on("response", on_response)

        print("Loading search page...")
        await page.goto("https://tulsaworld.column.us/search", wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(5000)

        # Try clicking Foreclosure Sale filter if visible
        try:
            btn = page.locator("text=Foreclosure Sale").first
            if await btn.count() > 0:
                await btn.click()
                await page.wait_for_timeout(3000)
                print("Clicked Foreclosure Sale filter")
        except:
            pass

        await browser.close()

    print(f"\nCaptured {len(captured)} requests:")
    for c in captured:
        print(json.dumps(c, indent=2)[:600])
        print("---")

asyncio.run(main())
