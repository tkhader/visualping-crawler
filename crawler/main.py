import asyncio
import json
import os
import re
from datetime import timedelta
from io import BytesIO
from urllib.parse import urljoin, urlparse

import httpx
import pytesseract
from bs4 import BeautifulSoup
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from PIL import Image, UnidentifiedImageError

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


def image_candidates(soup: BeautifulSoup, base_url: str) -> set[str]:
    candidates: set[str] = set()
    for tag in soup.find_all(["img", "source", "meta"]):
        for attr in ("src", "srcset", "data-src", "data-lazy-src", "content"):
            value = tag.get(attr)
            if not value:
                continue
            values = value.split(",") if attr == "srcset" else [value]
            for item in values:
                raw = item.strip().split(" ")[0]
                candidate = absolute_candidate(raw, base_url)
                if candidate:
                    candidates.add(candidate)
    return candidates


async def main() -> None:
    found: set[str] = set()
    visited: set[str] = set()
    processed_images: set[str] = set()
    results: list[dict] = []
    image_results: list[dict] = []

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

    async def scan_images(urls: set[str]) -> None:
        if not USERNAME or not PASSWORD:
            return
        async with httpx.AsyncClient(auth=(USERNAME, PASSWORD), follow_redirects=True, timeout=20) as client:
            for image_url in urls - processed_images:
                processed_images.add(image_url)
                try:
                    response = await client.get(image_url)
                    if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
                        continue
                    image = Image.open(BytesIO(response.content))
                    ocr_text = pytesseract.image_to_string(image)
                    matches = extract_matches(ocr_text)
                    found.update(matches)
                    image_results.append({"url": image_url, "matches": sorted(matches), "ocr_chars": len(ocr_text)})
                except (httpx.HTTPError, UnidentifiedImageError, OSError, pytesseract.TesseractError):
                    continue

    @crawler.router.default_handler
    async def handle(context: PlaywrightCrawlingContext) -> None:
        url = context.request.url
        depth = int(context.request.user_data.get("depth", 0))
        if not allowed(url) or url in visited or depth > MAX_DEPTH:
            return
        visited.add(url)
        page = context.page
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

        for link in soup.find_all("link", rel=lambda value: value and "next" in value):
            candidate = absolute_candidate(link.get("href", ""), url)
            if candidate:
                candidates.add(candidate)

        for tag in soup.find_all(["a", "button", "input"]):
            label = " ".join(tag.get_text(" ", strip=True).split()).lower()
            metadata = " ".join(str(tag.get(attr, "")) for attr in ("aria-label", "title", "value", "data-page", "data-page-number", "data-next", "data-url"))
            if any(word in f"{label} {metadata}".lower() for word in ("next", "older", "more", "page")):
                for attr in ("href", "data-url", "data-next", "formaction"):
                    candidate = absolute_candidate(tag.get(attr, ""), url)
                    if candidate:
                        candidates.add(candidate)

        images = image_candidates(soup, url)
        await scan_images(images)
        found.update(extract_matches(" ".join(images)))

        try:
            resource_urls = await page.evaluate("performance.getEntriesByType('resource').map(e => e.name)")
            for resource_url in resource_urls:
                candidate = absolute_candidate(resource_url, url)
                if candidate:
                    candidates.add(candidate)
                if candidate and any(resource_url.lower().split("?")[0].endswith(ext) for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp")):
                    await scan_images({candidate})
        except Exception:
            pass

        found.update(page_found)
        results.append({"url": url, "title": title, "depth": depth, "matches": sorted(page_found), "links_found": len(candidates), "images_found": len(images)})
        if depth < MAX_DEPTH:
            await context.add_requests([Request.from_url(link, user_data={"depth": depth + 1}) for link in candidates])

    await crawler.run([Request.from_url(START_URL, user_data={"depth": 0})])
    with open("results.json", "w", encoding="utf-8") as output:
        json.dump({"matches": sorted(found), "pages": results, "images": image_results}, output, indent=2)
    print(json.dumps({"matches": sorted(found), "pages_crawled": len(results), "images_processed": len(image_results)}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
