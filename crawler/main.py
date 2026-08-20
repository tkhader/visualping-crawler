import json
import os
import re
from datetime import timedelta
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext

PATTERN = re.compile(r"VISUALPING\{[^{}]+\}")
START_URL = os.getenv("CRAWLER_START_URL", "http://54.214.7.161/")
ALLOWED_HOST = os.getenv("CRAWLER_ALLOWED_HOST", urlparse(START_URL).hostname or "")
MAX_REQUESTS = int(os.getenv("CRAWLER_MAX_REQUESTS", "100"))
LOGIN_URL = os.getenv("CRAWLER_LOGIN_URL")
USERNAME = "tanzil.khader"
PASSWORD = "05b00ab58de4873c754c"
USERNAME_SELECTOR = os.getenv("CRAWLER_USERNAME_SELECTOR", 'input[name="username"]')
PASSWORD_SELECTOR = os.getenv("CRAWLER_PASSWORD_SELECTOR", 'input[type="password"]')
SUBMIT_SELECTOR = os.getenv("CRAWLER_SUBMIT_SELECTOR", 'button[type="submit"]')


def allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname == ALLOWED_HOST


def matches(value: str, source: str, found: set[str]) -> None:
    for match in PATTERN.findall(value or ""):
        found.add(match)


async def main() -> None:
    found: set[str] = set()
    visited: set[str] = set()
    results: list[dict] = []

    crawler = PlaywrightCrawler(
        max_requests_per_crawl=MAX_REQUESTS,
        request_handler_timeout=timedelta(seconds=30),
        max_request_retries=2,
    )

    @crawler.router.default_handler
    async def handle(context: PlaywrightCrawlingContext) -> None:
        url = context.request.url
        if not allowed(url) or url in visited:
            return
        visited.add(url)
        page = context.page

        if LOGIN_URL and USERNAME is not None and PASSWORD is not None and url == START_URL:
            if not allowed(LOGIN_URL):
                raise ValueError("CRAWLER_LOGIN_URL must use the allowed host")
            await page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await page.locator(USERNAME_SELECTOR).fill(USERNAME)
            await page.locator(PASSWORD_SELECTOR).fill(PASSWORD)
            await page.locator(SUBMIT_SELECTOR).click()
            await page.wait_for_load_state("domcontentloaded")
            url = page.url

        html = await page.content()
        text = await page.locator("body").inner_text(timeout=10000)
        title = await page.title()
        page_found: set[str] = set()
        matches(url, "url", page_found)
        matches(html, "html", page_found)
        matches(text, "text", page_found)

        soup = BeautifulSoup(html, "html.parser")
        links = []
        for tag in soup.find_all(["a", "img", "script", "link"]):
            for attr in ("href", "src", "data-src", "content"):
                value = tag.get(attr)
                if value:
                    absolute = urljoin(url, value)
                    matches(absolute, "attribute", page_found)
                    if tag.name == "a" and allowed(absolute):
                        links.append(absolute)

        found.update(page_found)
        results.append({"url": url, "title": title, "matches": sorted(page_found)})
        await context.add_requests([Request.from_url(link) for link in set(links) if allowed(link)])

    await crawler.run([START_URL])
    with open("results.json", "w", encoding="utf-8") as output:
        json.dump({"matches": sorted(found), "pages": results}, output, indent=2)
    print(json.dumps({"matches": sorted(found), "pages_crawled": len(results)}, indent=2))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
