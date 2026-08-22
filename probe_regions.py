import asyncio
import httpx

AUTH = ("tanzil.khader", "05b00ab58de4873c754c")
CANDIDATES = [
    "us-region", "na-region", "apac-region", "asia-region",
    "uk-region", "emea-region", "latam-region", "au-region",
    "jp-region", "in-region", "ca-region", "br-region",
]

async def probe():
    async with httpx.AsyncClient(auth=AUTH, follow_redirects=True, timeout=20) as client:
        for region in CANDIDATES:
            url = f"http://54.214.7.161/status/{region}/"
            try:
                resp = await client.get(url)
                print(url, "->", resp.status_code)
                if resp.status_code == 200:
                    print(resp.text[:1500])
                    print("=" * 60)
            except httpx.HTTPError as e:
                print(url, "-> ERROR", e)

asyncio.run(probe())
