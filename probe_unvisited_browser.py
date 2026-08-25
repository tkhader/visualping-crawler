import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

BASE_HOST = "54.214.7.161"
AUTH = {"username": "tanzil.khader", "password": "05b00ab58de4873c754c"}
PATTERN = re.compile(r"VISUALPING\{[0-9a-f]{16}\}")
EXAMPLE_MARKER = "VISUALPING{0000deadbeef0000}"


def extract_matches(value: object) -> set[str]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str):
        value = str(value)
    return {match for match in PATTERN.findall(value) if match != EXAMPLE_MARKER}


async def main() -> None:
    with open("results.json", encoding="utf-8") as source:
        urls = json.load(source)["crawl_audit"]["discovered_but_unvisited"]

    hits: dict[str, set[str]] = {}
    response_count = 0
    rendered_count = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            http_credentials=AUTH,
            locale="de-DE",
            extra_http_headers={"Accept-Language": "de-DE,de;q=0.9,en;q=0.5"},
        )

        for url in urls:
            page = await context.new_page()
            response_tasks: set[asyncio.Task] = set()

            async def inspect_response(response) -> None:
                nonlocal response_count
                if BASE_HOST not in response.url:
                    return
                response_count += 1
                try:
                    headers = await response.all_headers()
                    text = " ".join(f"{key}: {value}" for key, value in headers.items())
                    if "text" in headers.get("content-type", "").lower() or "json" in headers.get("content-type", "").lower():
                        text += " " + (await response.body()).decode("utf-8", errors="ignore")
                    matches = extract_matches(text)
                    if matches:
                        hits.setdefault(f"network:{response.request.method}:{response.url}", set()).update(matches)
                except Exception:
                    pass

            def schedule_response(response) -> None:
                task = asyncio.create_task(inspect_response(response))
                response_tasks.add(task)
                task.add_done_callback(response_tasks.discard)

            page.on("response", schedule_response)
            try:
                await page.goto(url, wait_until="networkidle", timeout=30000)
                rendered_count += 1
                await page.wait_for_timeout(250)
                state = await page.evaluate("""async () => {
                    const values = [location.href, location.hash, document.cookie, document.documentElement.outerHTML];
                    for (const element of document.querySelectorAll('*')) {
                        values.push(getComputedStyle(element, '::before').content);
                        values.push(getComputedStyle(element, '::after').content);
                        if (element.shadowRoot) values.push(element.shadowRoot.textContent || '');
                    }
                    values.push(JSON.stringify({...localStorage}), JSON.stringify({...sessionStorage}));
                    if (indexedDB.databases) values.push(JSON.stringify(await indexedDB.databases()));
                    if (window.caches) {
                        for (const name of await caches.keys()) {
                            const cache = await caches.open(name);
                            for (const request of await cache.keys()) {
                                const response = await cache.match(request);
                                values.push(request.url, response ? await response.text() : '');
                            }
                        }
                    }
                    return values.join(' ');
                }""")
                matches = extract_matches(state)
                if matches:
                    hits.setdefault(f"browser_state:{url}", set()).update(matches)
            except Exception as error:
                hits.setdefault(f"error:{url}", set()).add(str(error))
            finally:
                if response_tasks:
                    await asyncio.gather(*response_tasks, return_exceptions=True)
                await page.close()

        await browser.close()

    serializable_hits = {source: sorted(matches) for source, matches in hits.items()}
    output = {
        "urls_probed": len(urls),
        "pages_rendered": rendered_count,
        "same_host_responses": response_count,
        "hits": serializable_hits,
        "new_markers": sorted(
            {
                marker
                for matches in serializable_hits.values()
                for marker in matches
                if marker.startswith("VISUALPING{")
            }
        ),
    }
    Path("unvisited_browser_audit.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
