import asyncio
import httpx

SERVICES = [
    "https://ipapi.co/json/",
    "https://ipinfo.io/json",
    "https://api.myip.com",
    "https://check.torproject.org/api/ip",
]

async def check():
    async with httpx.AsyncClient(proxy="socks5://127.0.0.1:9050", timeout=30) as client:
        for url in SERVICES:
            try:
                resp = await client.get(url)
                print(url, "->", resp.status_code)
                print(resp.text[:500])
                print("-" * 60)
            except Exception as e:
                print(url, "-> ERROR:", e)
                print("-" * 60)

asyncio.run(check())
