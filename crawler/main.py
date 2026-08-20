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
MAX_REQUESTS = int(os.getenv("CRAWLER_MAX_REQUESTS", "500"))
MAX_DEPTH = int(os.getenv("CRAWLER_MAX_DEPTH", "5"))
LOGIN_URL = os.getenv("CRAWLER_LOGIN_URL")
USERNAME = os.getenv("CRAWLER_USERNAME")
PASSWORD = os.getenv("CRAWLER_PASSWORD")
USERNAME_SELECTOR = os.getenv("CRAWLER_USERNAME_SELECTOR", 'input[name="username"]')
PASSWORD_SELECTOR = os.getenv("CRAWLER_PASSWORD_SELECTOR", 'input[type="password"]')
SUBMIT_SELECTOR = os.getenv("CRAWLER_SUBMIT_SELECTOR", 'button[type="submit"]')


def allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname == ALLOWED_HOST


def extract_matches(value: str) -> set[str]:
    return set(PATTERN.findall(value or ""))


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
        depth = int(context.request.user_data.get("depth", 0))
        if not allowed(url) or url in visited or depth > MAX_DEPTH:
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
        page_found = extract_matches(url) | extract_matches(html) | extract_matches(text)

        soup = BeautifulSoup(html, "html.parser")
        next_requests: list[Request] = []
        link_count = 0
        for tag in soup.find_all(True):
            for attr, value in tag.attrs.items():
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if not isinstance(item, str):
                        continue
                    absolute = urljoin(url, item) if attr in {"href", "src", "action", "poster", "content"} else item
                    page_found.update(extract_matches(item))
                    page_found.update(extract_matches(absolute))
                    if attr in {"href", "src", "action"} and allowed(absolute):
                        link_count += 1
                        if tag.name == "a" and depth < MAX_DEPTH:
                            next_requests.append(Request.from_url(absolute, user_data={"depth": depth + 1}))

        for script in soup.find_all("script"):
            page_found.update(extract_matches(script.string or script.get_text()))

        found.update(page_found)
        results.append({"url": url, "title": title, "depth": depth, "matches": sorted(page_found), "links_found": link_count})
        unique = {request.url: request for request in next_requests}
        await context.add_requests(list(unique.values()))

    await crawler.run([Request.from_url(START_URL, user_data={"depth": 0})])
    with open("results.json", "w", encoding="utf-8") as output:
        json.dump({"matches": sorted(found), "pages": results}, output, indent=2)
    print(json.dumps({"matches": sorted(found), "pages_crawled": len(results)}, indent=2))


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
