import asyncio
import json
import re

import httpx

PATTERN = re.compile(r"VISUALPING\{[0-9a-f]{16}\}")
EXAMPLE_MARKER = "VISUALPING{0000deadbeef0000}"
AUTH = ("tanzil.khader", "05b00ab58de4873c754c")
BASE = "http://54.214.7.161"


def find_matches(text: str) -> set[str]:
    return {m for m in PATTERN.findall(text or "") if m != EXAMPLE_MARKER}


def load_real_urls(limit: int = 40) -> list[str]:
    """Pull actual crawled page URLs from results.json instead of guessing paths."""
    try:
        with open("results.json") as f:
            data = json.load(f)
    except FileNotFoundError:
        print("results.json not found in this directory -- run the crawler first, or cd to where it wrote the file.")
        return []
    urls = [p["url"] for p in data.get("pages", [])]
    # Prioritize the pages we already know are "special" plus a spread of sections
    priority = [u for u in urls if "filter-gateway" in u or "status" in u]
    seen_prefixes = set()
    spread = []
    for u in urls:
        prefix = u.split("54.214.7.161")[-1].split("/")[1] if "54.214.7.161" in u else u
        if prefix not in seen_prefixes:
            seen_prefixes.add(prefix)
            spread.append(u)
    combined = list(dict.fromkeys(priority + spread))[:limit]
    return combined


QUERY_PARAMS = [
    "?debug=1", "?admin=1", "?internal=1", "?preview=1",
    "?format=json", "?role=admin", "?mode=dev", "?show_all=1",
]
ACCEPT_HEADERS = ["application/json", "text/plain", "application/xml"]
USER_AGENTS = ["Googlebot/2.1 (+http://www.google.com/bot.html)", "curl/8.0", "InternalMonitor/1.0"]


async def probe():
    urls = load_real_urls()
    if not urls:
        return
    print(f"Testing {len(urls)} real crawled URLs...\n")

    hits = []
    async with httpx.AsyncClient(auth=AUTH, timeout=20, follow_redirects=True) as client:
        for url in urls:
            try:
                base_resp = await client.get(url)
                base_len = len(base_resp.text)
                base_matches = find_matches(base_resp.text)
            except httpx.HTTPError as e:
                print(f"{url} baseline ERROR: {e}")
                continue

            variants = (
                [(url + qp, {}, f"query:{qp}") for qp in QUERY_PARAMS]
                + [(url, {"Accept": a}, f"accept:{a}") for a in ACCEPT_HEADERS]
                + [(url, {"User-Agent": ua}, f"ua:{ua}") for ua in USER_AGENTS]
            )

            for target_url, headers, label in variants:
                try:
                    resp = await client.get(target_url, headers=headers)
                    matches = find_matches(resp.text) - base_matches
                    if matches:
                        print(f"NEW FLAG: {target_url} [{label}] -> {sorted(matches)}")
                        hits.append({"url": target_url, "variant": label, "matches": sorted(matches)})
                    elif resp.status_code == 200 and len(resp.text) != base_len:
                        print(f"DIFF (no flag yet): {target_url} [{label}] len={len(resp.text)} vs baseline {base_len}")
                except httpx.HTTPError:
                    continue

    print("\n=== CONFIRMED NEW HITS ===")
    print(json.dumps(hits, indent=2))


asyncio.run(probe())
