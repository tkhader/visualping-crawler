import asyncio
import json
import os
import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

PATTERN = re.compile(r"VISUALPING\{[0-9a-f]{16}\}")
URL_PATTERN = re.compile(r"(?:https?://|/)[^\"'\s<>`]+")
CSS_URL_PATTERN = re.compile(r"url\(\s*[\"']?([^\"')]+)")
EXAMPLE_MARKER = "VISUALPING{0000deadbeef0000}"
START_URL = os.getenv("CRAWLER_START_URL", "http://54.214.7.161/")
ALLOWED_HOST = os.getenv("CRAWLER_ALLOWED_HOST", urlparse(START_URL).hostname or "")
MAX_REQUESTS = int(os.getenv("CRAWLER_MAX_REQUESTS", "500"))
MAX_DEPTH = int(os.getenv("CRAWLER_MAX_DEPTH", "5"))
USERNAME = os.getenv("CRAWLER_USERNAME")
PASSWORD = os.getenv("CRAWLER_PASSWORD")
TEXT_CONTENT_TYPES = ("text/", "application/json", "application/javascript", "application/xml", "application/xhtml+xml")


def allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname == ALLOWED_HOST


def extract_matches(value: str) -> set[str]:
    return {match for match in PATTERN.findall(value or "") if match != EXAMPLE_MARKER}


def absolute_candidate(value: str, base_url: str) -> str | None:
    if not value or value.startswith(("#", "mailto:", "javascript:", "data:", "tel:")):
        return None
    candidate = urljoin(base_url, value.strip())
    return candidate if allowed(candidate) else None


async def main() -> None:
    found: set[str] = set()
    visited: set[str] = set()
    results: list[dict] = []
    response_matches: list[dict] = []

    context_options = {}
    if USERNAME is not None and PASSWORD is not None:
        context_options["http_credentials"] = {
            "username": USERNAME,
            "password": PASSWORD,
            "send": "always",
        }

    crawler = PlaywrightCrawler(
        max_requests_per_crawl=MAX_REQUESTS,
        request_handler_timeout=timedelta(seconds=30),
        max_request_retries=1,
        browser_new_context_options=context_options,
    )

    @crawler.router.default_handler
    async def handle(context: PlaywrightCrawlingContext) -> None:
        url = context.request.url
        depth = int(context.request.user_data.get("depth", 0))
        if not allowed(url) or url in visited or depth > MAX_DEPTH:
            return
        visited.add(url)
        page = context.page

        async def scan_response(response) -> None:
            if not allowed(response.url):
                return
            content_type = response.headers.get("content-type", "").lower()
            if not any(content_type.startswith(prefix) for prefix in TEXT_CONTENT_TYPES):
                return
            try:
                body = await response.text()
            except Exception:
                return
            matches = extract_matches(body)
            if matches:
                found.update(matches)
                response_matches.append({"url": response.url, "content_type": content_type, "matches": sorted(matches)})

        page.on("response", lambda response: asyncio.create_task(scan_response(response)))
        await page.wait_for_load_state("networkidle", timeout=15000)

        html = await page.content()
        text = await page.locator("body").inner_text(timeout=10000)
        title = await page.title()
        page_found = extract_matches(url) | extract_matches(html) | extract_matches(text)
        candidates: set[str] = set()

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all(True):
            for attr, value in tag.attrs.items():
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if not isinstance(item, str):
                        continue
                    page_found.update(extract_matches(item))
                    for raw in URL_PATTERN.findall(item):
                        candidate = absolute_candidate(raw, url)
                        if candidate:
                            candidates.add(candidate)
                    for raw in CSS_URL_PATTERN.findall(item):
                        candidate = absolute_candidate(raw, url)
                        if candidate:
                            candidates.add(candidate)

        for script in soup.find_all("script"):
            script_text = script.string or script.get_text()
            page_found.update(extract_matches(script_text))
            for raw in URL_PATTERN.findall(script_text):
                candidate = absolute_candidate(raw, url)
                if candidate:
                    candidates.add(candidate)

        try:
            resource_urls = await page.evaluate("performance.getEntriesByType('resource').map(e => e.name)")
            for resource_url in resource_urls:
                candidate = absolute_candidate(resource_url, url)
                if candidate:
                    candidates.add(candidate)
        except Exception:
            pass

        found.update(page_found)
        results.append({"url": url, "title": title, "depth": depth, "matches": sorted(page_found), "links_found": len(candidates)})
        if depth < MAX_DEPTH:
            requests = [Request.from_url(link, user_data={"depth": depth + 1}) for link in candidates]
            await context.add_requests(requests)

    await crawler.run([Request.from_url(START_URL, user_data={"depth": 0})])
    await asyncio.sleep(1)
    with open("results.json", "w", encoding="utf-8") as output:
        json.dump({"matches": sorted(found), "pages": results, "response_matches": response_matches}, output, indent=2)
    print(json.dumps({"matches": sorted(found), "pages_crawled": len(results), "response_matches": len(response_matches)}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
