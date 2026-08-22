import asyncio
import json
import re

import httpx

PATTERN = re.compile(r"VISUALPING\{[0-9a-f]{16}\}")
AUTH = ("tanzil.khader", "05b00ab58de4873c754c")
BASE = "http://54.214.7.161"

# A representative sample: homepage, the known geofenced page, and one page
# from each top-level section (adjust these to real URLs from your results.json
# if you want broader coverage -- these are just plausible/likely paths).
SAMPLE_PATHS = [
    "/",
    "/status/eu-region/",
    "/wiki/",
    "/products/",
    "/help/",
    "/docs/",
    "/notes/",
    "/blog/",
    "/report/",
]

QUERY_PARAMS = [
    "?debug=1", "?debug=true", "?admin=1", "?internal=1",
    "?preview=1", "?verbose=1", "?format=json", "?raw=1",
    "?role=admin", "?mode=dev", "?test=1", "?show_all=1",
]

ACCEPT_HEADERS = [
    "application/json",
    "text/plain",
    "application/xml",
]

USER_AGENTS = [
    "Googlebot/2.1 (+http://www.google.com/bot.html)",
    "curl/8.0",
    "InternalMonitor/1.0",
]


def find_matches(text: str) -> set[str]:
    return {m for m in PATTERN.findall(text or "") if m != "0000deadbeef0000"}


async def probe():
    hits = []
    async with httpx.AsyncClient(auth=AUTH, timeout=20, follow_redirects=True) as client:
        for path in SAMPLE_PATHS:
            url = BASE + path

            # Baseline
            try:
                base_resp = await client.get(url)
                base_len = len(base_resp.text)
            except httpx.HTTPError as e:
                print(f"{url} baseline ERROR: {e}")
                continue

            # Query params
            for qp in QUERY_PARAMS:
                try:
                    resp = await client.get(url + qp)
                    matches = find_matches(resp.text)
                    if matches or (resp.status_code == 200 and len(resp.text) != base_len):
                        print(f"{url}{qp} -> {resp.status_code}, len={len(resp.text)} (baseline {base_len}), matches={matches}")
                        if matches:
                            hits.append({"url": url + qp, "matches": sorted(matches)})
                except httpx.HTTPError:
                    continue

            # Accept header negotiation
            for accept in ACCEPT_HEADERS:
                try:
                    resp = await client.get(url, headers={"Accept": accept})
                    matches = find_matches(resp.text)
                    if matches or (resp.status_code == 200 and len(resp.text) != base_len):
                        print(f"{url} Accept:{accept} -> {resp.status_code}, len={len(resp.text)}, matches={matches}")
                        if matches:
                            hits.append({"url": url, "header": f"Accept: {accept}", "matches": sorted(matches)})
                except httpx.HTTPError:
                    continue

            # User-Agent variations
            for ua in USER_AGENTS:
                try:
                    resp = await client.get(url, headers={"User-Agent": ua})
                    matches = find_matches(resp.text)
                    if matches or (resp.status_code == 200 and len(resp.text) != base_len):
                        print(f"{url} UA:{ua} -> {resp.status_code}, len={len(resp.text)}, matches={matches}")
                        if matches:
                            hits.append({"url": url, "header": f"User-Agent: {ua}", "matches": sorted(matches)})
                except httpx.HTTPError:
                    continue

            # OPTIONS method
            try:
                resp = await client.options(url)
                matches = find_matches(resp.text) | find_matches(str(resp.headers))
                if matches or resp.status_code not in (404, 405):
                    print(f"{url} OPTIONS -> {resp.status_code}, allow={resp.headers.get('allow')}, matches={matches}")
                    if matches:
                        hits.append({"url": url, "method": "OPTIONS", "matches": sorted(matches)})
            except httpx.HTTPError:
                continue

    print("\n=== HITS ===")
    print(json.dumps(hits, indent=2))


asyncio.run(probe())
