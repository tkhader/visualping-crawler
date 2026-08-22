import asyncio
import httpx

URL = "http://54.214.7.161/status/eu-region/"
AUTH = ("tanzil.khader", "05b00ab58de4873c754c")

HEADER_VARIANTS = [
    {"X-Forwarded-For": "85.214.132.117"},
    {"X-Real-IP": "85.214.132.117"},
    {"CF-Connecting-IP": "85.214.132.117"},
    {"CF-IPCountry": "DE"},
    {"X-Country-Code": "DE"},
    {"X-Geo-Country": "DE"},
    {"X-AppEngine-Country": "DE"},
    {"X-Forwarded-For": "85.214.132.117", "CF-IPCountry": "DE"},
]

async def probe():
    async with httpx.AsyncClient(auth=AUTH, follow_redirects=True, timeout=20) as client:
        base = await client.get(URL)
        print("BASELINE:", base.status_code, base.text[:200].replace("\n", " "))
        print("-" * 60)

        for headers in HEADER_VARIANTS:
            resp = await client.get(URL, headers=headers)
            print(headers, "->", resp.status_code)
            if resp.status_code == 200:
                print(resp.text[:2000])
            print("-" * 60)

asyncio.run(probe())
