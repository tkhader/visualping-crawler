import asyncio
import httpx

async def probe():
    async with httpx.AsyncClient(
        auth=("tanzil.khader", "05b00ab58de4873c754c"),
        proxy="socks5://127.0.0.1:9050",
        timeout=30,
    ) as client:
        resp = await client.get("http://54.214.7.161/status/eu-region/")
        print(resp.status_code)
        print(resp.text[:2000])

asyncio.run(probe())
