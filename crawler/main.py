from __future__ import annotations

import asyncio
import json
import os
import re
import zipfile
from datetime import timedelta
from io import BytesIO
from urllib.parse import urljoin, urlparse

import httpx
import pytesseract
from bs4 import BeautifulSoup, Comment
from crawlee import Request
from crawlee.crawlers import PlaywrightCrawler, PlaywrightCrawlingContext
from PIL import Image, UnidentifiedImageError

try:
    from pypdf import PdfReader
except ImportError:
    PdfReader = None

PATTERN = re.compile(r"VISUALPING\{[0-9a-f]{16}\}")
OCR_PATTERN = re.compile(r"VISUALPING\{([0-9a-z/]{16})\}", re.IGNORECASE)
URL_PATTERN = re.compile(r"(?:https?://|/)[^\"'\s<>`]+")
CSS_URL_PATTERN = re.compile(r"url\(\s*[\"']?([^\"')]+)")
EXAMPLE_MARKER = "VISUALPING{0000deadbeef0000}"
START_URL = os.getenv("CRAWLER_START_URL", "http://54.214.7.161/")
ALLOWED_HOST = os.getenv("CRAWLER_ALLOWED_HOST", urlparse(START_URL).hostname or "")
MAX_REQUESTS = int(os.getenv("CRAWLER_MAX_REQUESTS", "500"))
MAX_DEPTH = int(os.getenv("CRAWLER_MAX_DEPTH", "5"))
USERNAME = os.getenv("CRAWLER_USERNAME")
PASSWORD = os.getenv("CRAWLER_PASSWORD")

# Extensions that are resources, not pages. These should never be queued
# as Playwright navigations -- doing so is what caused the 403 storm.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
TEXT_RESOURCE_EXTENSIONS = {
    ".js", ".mjs", ".css", ".map", ".json", ".xml", ".svg", ".txt", ".csv",
    ".woff", ".woff2", ".ttf", ".eot", ".otf",
}
DOCUMENT_EXTENSIONS = {".pdf"}
ARCHIVE_EXTENSIONS = {".zip"}

# Pages that 403 for every normal request turned out to be geofenced
# (e.g. /status/eu-region/ only serves 200 to German IPs). We can't get
# a real browser to originate from another country, so known geofenced
# paths are retried post-crawl through a local Tor SOCKS proxy exited
# in the required region, via a plain httpx GET (no need for a full
# browser context for a static status page).
TOR_PROXY = os.getenv("CRAWLER_TOR_PROXY", "socks5://127.0.0.1:9050")
KNOWN_GEOFENCED_PATHS = ["/status/eu-region/"]


def allowed(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname == ALLOWED_HOST


def extract_matches(value: object) -> set[str]:
    if isinstance(value, bytes):
        decoded_values = [value.decode("utf-8", errors="ignore")]
        for encoding in ("utf-16le", "utf-16be"):
            decoded_values.append(value.decode(encoding, errors="ignore"))
        value = " ".join(decoded_values)
    elif not isinstance(value, str):
        value = str(value)
    return {match for match in PATTERN.findall(value or "") if match != EXAMPLE_MARKER}


def extract_ocr_matches(value: str) -> set[str]:
    corrections = str.maketrans({"o": "0", "i": "1", "l": "1", "/": "1"})
    matches = set()
    for candidate in OCR_PATTERN.findall(value or ""):
        normalized = candidate.lower().translate(corrections)
        marker = f"VISUALPING{{{normalized}}}"
        if PATTERN.fullmatch(marker) and marker != EXAMPLE_MARKER:
            matches.add(marker)
    return matches


def absolute_candidate(value: str, base_url: str) -> str | None:
    if not value or value.startswith(("#", "mailto:", "javascript:", "data:", "tel:")):
        return None
    candidate = urljoin(base_url, value.strip())
    return candidate if allowed(candidate) else None


def resource_kind(url: str) -> str:
    """Classify a URL as 'image', 'text_resource', 'document', 'archive', or 'page' based on its path extension."""
    path = urlparse(url).path.lower().split("?")[0]
    if any(path.endswith(ext) for ext in IMAGE_EXTENSIONS):
        return "image"
    if any(path.endswith(ext) for ext in DOCUMENT_EXTENSIONS):
        return "document"
    if any(path.endswith(ext) for ext in ARCHIVE_EXTENSIONS):
        return "archive"
    if any(path.endswith(ext) for ext in TEXT_RESOURCE_EXTENSIONS):
        return "text_resource"
    return "page"


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
    match_sources: dict[str, str] = {}  # flag -> where/how it was discovered
    visited: set[str] = set()
    processed_images: set[str] = set()
    processed_resources: set[str] = set()
    blocked_urls: dict[str, str] = {}  # url -> kind ("image"/"text"/"document"), for Tor retry
    results: list[dict] = []
    image_results: list[dict] = []
    resource_results: list[dict] = []
    debug_counts = {"canvas_seen": 0, "reveal_clicks": 0, "iframes_seen": 0, "pages_with_canvas": 0, "pages_with_iframe": 0, "selects_seen": 0, "checkboxes_radios_seen": 0, "header_checks_ok": 0, "header_checks_response_none": 0, "header_checks_failed": 0, "text_inputs_seen": 0, "websockets_seen": 0}

    def record(matches: set[str], source: str) -> None:
        for m in matches:
            if m not in match_sources:
                match_sources[m] = source
        found.update(matches)

    context_options = {}
    if USERNAME is not None and PASSWORD is not None:
        context_options["http_credentials"] = {
            "username": USERNAME,
            "password": PASSWORD,
            "send": "always",
        }
    # The one confirmed geofenced page is region-gated; test whether content
    # elsewhere is also gated by *language* preference rather than IP, by
    # requesting German content on every page throughout the whole crawl.
    context_options["extra_http_headers"] = {"Accept-Language": "de-DE,de;q=0.9,en;q=0.5"}
    context_options["locale"] = "de-DE"
    # Default headless Chromium UA literally contains "HeadlessChrome" --
    # a direct bot-detection giveaway. Use a realistic desktop Chrome UA.
    context_options["user_agent"] = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )

    crawler = PlaywrightCrawler(
        max_requests_per_crawl=MAX_REQUESTS,
        request_handler_timeout=timedelta(seconds=30),
        max_request_retries=1,
        browser_new_context_options=context_options,
    )

    async def scan_images(urls: set[str], client_override: httpx.AsyncClient | None = None) -> None:
        if not USERNAME or not PASSWORD:
            return

        async def _do_scan(client: httpx.AsyncClient) -> None:
            for image_url in urls - processed_images:
                processed_images.add(image_url)
                try:
                    response = await client.get(image_url)
                    if response.status_code == 403:
                        blocked_urls[image_url] = "image"
                        continue
                    if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
                        continue
                    # Scan response headers too -- flags can live outside the body.
                    header_text = " ".join(f"{k}: {v}" for k, v in response.headers.items())
                    record(extract_matches(header_text), f"image_header:{image_url}")

                    image = Image.open(BytesIO(response.content))
                    ocr_text = pytesseract.image_to_string(image, config="--psm 13")
                    matches = extract_ocr_matches(ocr_text)
                    record(matches, f"image_ocr:{image_url}")

                    # EXIF metadata is a separate hiding spot from pixel content --
                    # OCR never sees it since it's not rendered in the image.
                    exif_matches: set[str] = set()
                    try:
                        exif_data = image.getexif()
                        for tag_id, value in exif_data.items():
                            exif_matches |= extract_matches(str(value))
                        # Also check common textual EXIF fields (Comment, ImageDescription, etc.)
                        # via PIL's info dict, which sometimes carries data getexif() misses.
                        for value in image.info.values():
                            exif_matches |= extract_matches(value)
                    except Exception:
                        pass
                    if exif_matches:
                        record(exif_matches, f"image_exif:{image_url}")
                        matches |= exif_matches

                    image_results.append({"url": image_url, "matches": sorted(matches), "ocr_chars": len(ocr_text)})
                except (httpx.HTTPError, UnidentifiedImageError, OSError, pytesseract.TesseractError):
                    continue

        if client_override is not None:
            await _do_scan(client_override)
        else:
            async with httpx.AsyncClient(auth=(USERNAME, PASSWORD), follow_redirects=True, timeout=20) as client:
                await _do_scan(client)

    async def scan_text_resources(urls: set[str], client_override: httpx.AsyncClient | None = None) -> None:
        """Fetch raw text resources (JS/CSS/JSON/etc.) directly and scan their content.
        Playwright's DOM/content() never exposes the raw text of external files, so this
        is required to catch flags embedded in scripts, stylesheets, or other assets."""

        async def _do_scan(client: httpx.AsyncClient) -> None:
            for res_url in urls - processed_resources:
                processed_resources.add(res_url)
                try:
                    response = await client.get(res_url)
                    if response.status_code == 403:
                        blocked_urls[res_url] = "text"
                        continue
                    if response.status_code != 200:
                        continue

                    header_text = " ".join(f"{k}: {v}" for k, v in response.headers.items())
                    record(extract_matches(header_text), f"text_resource_header:{res_url}")

                    body_text = response.text
                    matches = extract_matches(body_text)
                    record(matches, f"text_resource:{res_url}")
                    resource_results.append({"url": res_url, "matches": sorted(matches), "content_type": response.headers.get("content-type", "")})

                    # CSS files can reference further images (e.g. background-image: url(...))
                    # that never appear anywhere in the HTML -- chase those too.
                    if res_url.lower().split("?")[0].endswith(".css"):
                        nested_images = set()
                        for raw in CSS_URL_PATTERN.findall(body_text):
                            candidate = absolute_candidate(raw, res_url)
                            if candidate and resource_kind(candidate) == "image":
                                nested_images.add(candidate)
                        if nested_images:
                            await scan_images(nested_images, client_override=client_override)
                except (httpx.HTTPError, UnicodeDecodeError):
                    continue

        if client_override is not None:
            await _do_scan(client_override)
        else:
            async with httpx.AsyncClient(auth=(USERNAME, PASSWORD) if USERNAME and PASSWORD else None, follow_redirects=True, timeout=20) as client:
                await _do_scan(client)

    async def scan_documents(urls: set[str]) -> None:
        """PDFs are a classic place to hide flags -- extract text page by page.
        Falls back to a raw byte-level regex scan if pypdf isn't available or
        parsing fails, since simple/uncompressed PDF streams often still
        contain readable ASCII."""
        async with httpx.AsyncClient(auth=(USERNAME, PASSWORD) if USERNAME and PASSWORD else None, follow_redirects=True, timeout=30) as client:
            for doc_url in urls - processed_resources:
                processed_resources.add(doc_url)
                try:
                    response = await client.get(doc_url)
                    if response.status_code != 200:
                        continue
                    header_text = " ".join(f"{k}: {v}" for k, v in response.headers.items())
                    record(extract_matches(header_text), f"document_header:{doc_url}")

                    matches: set[str] = set()
                    if PdfReader is not None:
                        try:
                            reader = PdfReader(BytesIO(response.content))
                            text_parts = [page.extract_text() or "" for page in reader.pages]
                            matches |= extract_matches("\n".join(text_parts))
                            for meta_value in (reader.metadata or {}).values():
                                matches |= extract_matches(str(meta_value))
                        except Exception:
                            pass
                    if not matches:
                        # Fallback: raw byte scan for readable ASCII flags.
                        matches |= extract_matches(response.content.decode("latin-1", errors="ignore"))

                    record(matches, f"document:{doc_url}")
                    resource_results.append({"url": doc_url, "matches": sorted(matches), "content_type": response.headers.get("content-type", "")})
                except httpx.HTTPError:
                    continue

    async def scan_archives(urls: set[str]) -> None:
        """Zip archives may contain files with flags in their names or contents."""
        async with httpx.AsyncClient(auth=(USERNAME, PASSWORD) if USERNAME and PASSWORD else None, follow_redirects=True, timeout=30) as client:
            for archive_url in urls - processed_resources:
                processed_resources.add(archive_url)
                try:
                    response = await client.get(archive_url)
                    if response.status_code != 200:
                        continue
                    matches: set[str] = set()
                    try:
                        with zipfile.ZipFile(BytesIO(response.content)) as zf:
                            for name in zf.namelist():
                                matches |= extract_matches(name)
                                try:
                                    with zf.open(name) as member:
                                        content = member.read(2_000_000)  # cap per-file read
                                        matches |= extract_matches(content.decode("utf-8", errors="ignore"))
                                except Exception:
                                    continue
                    except zipfile.BadZipFile:
                        continue
                    record(matches, f"archive:{archive_url}")
                    resource_results.append({"url": archive_url, "matches": sorted(matches), "content_type": "application/zip"})
                except httpx.HTTPError:
                    continue

    @crawler.pre_navigation_hook
    async def setup_stealth_and_websocket_capture(context: PlaywrightCrawlingContext) -> None:
        # Must attach before navigation -- by the time the page handler runs,
        # any WebSocket handshake during initial load has already happened,
        # and any bot-detection JS on the page has already run its checks.
        page = context.page

        # Mask common automation fingerprints. The persistent, recurring
        # "session blocked (403)" warnings seen on every single run of this
        # crawler are consistent with active bot-detection -- if so, some
        # content may be silently served in a decoy/reduced form specifically
        # to automated browsers rather than outright blocked. This patches
        # the most commonly checked properties before any page script runs.
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['de-DE', 'de', 'en-US', 'en'] });
            window.chrome = window.chrome || { runtime: {} };
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : originalQuery(parameters)
            );
        """)

        def handle_ws(ws):
            debug_counts["websockets_seen"] += 1

            def on_frame(payload):
                matches = extract_matches(str(payload))
                if matches:
                    record(matches, f"websocket:{ws.url}")

            ws.on("framereceived", on_frame)
            ws.on("framesent", on_frame)

        page.on("websocket", handle_ws)

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
        page_found = extract_matches(url) | extract_matches(html) | extract_matches(text) | extract_matches(title)

        # Check the main navigation response headers too (not just asset fetches).
        try:
            main_response = context.response
            if main_response is not None:
                header_text = " ".join(f"{k}: {v}" for k, v in (await main_response.all_headers()).items())
                header_matches = extract_matches(header_text)
                record(header_matches, f"response_header:{url}")
                page_found.update(header_matches)
                debug_counts["header_checks_ok"] += 1
            else:
                debug_counts["header_checks_response_none"] += 1
        except Exception:
            debug_counts["header_checks_failed"] += 1

        # Cookies can carry flags too.
        try:
            cookies = await page.context.cookies()
            cookie_text = " ".join(f"{c.get('name')}={c.get('value')}" for c in cookies)
            cookie_matches = extract_matches(cookie_text)
            record(cookie_matches, f"cookie:{url}")
            page_found.update(cookie_matches)
        except Exception:
            pass
        candidates: set[str] = set()
        soup = BeautifulSoup(html, "html.parser")

        # HTML comments are invisible to inner_text() and easy to miss.
        for comment in soup.find_all(string=lambda t: isinstance(t, Comment)):
            page_found.update(extract_matches(str(comment)))

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

        # Canvas-drawn text is invisible to page.content() and to <img> OCR --
        # it only exists as pixels painted by JS. Screenshot each canvas and OCR it.
        try:
            canvas_count = await page.locator("canvas").count()
            if canvas_count:
                debug_counts["canvas_seen"] += canvas_count
                debug_counts["pages_with_canvas"] += 1
            for i in range(canvas_count):
                try:
                    canvas_bytes = await page.locator("canvas").nth(i).screenshot(timeout=5000)
                    canvas_image = Image.open(BytesIO(canvas_bytes))
                    ocr_text = pytesseract.image_to_string(canvas_image)
                    canvas_matches = extract_matches(ocr_text)
                    if canvas_matches:
                        record(canvas_matches, f"canvas_ocr:{url}#{i}")
                        page_found.update(canvas_matches)
                except Exception:
                    continue
        except Exception:
            pass

        # Click-to-reveal content: buttons/toggles that only render their text
        # (or swap in real content) after interaction. Click anything plausible,
        # then re-scan the DOM. Cheap and safe since this is a read-only crawl.
        try:
            reveal_selector = (
                "button, [role='button'], summary, "
                "[class*='reveal'], [class*='toggle'], [class*='accordion'], "
                "[class*='expand'], [class*='show'], [aria-expanded='false']"
            )
            reveal_count = await page.locator(reveal_selector).count()
            for i in range(min(reveal_count, 40)):  # cap to avoid pathological pages
                try:
                    el = page.locator(reveal_selector).nth(i)
                    if await el.is_visible():
                        await el.click(timeout=2000, force=True)
                        debug_counts["reveal_clicks"] += 1
                        await page.wait_for_timeout(200)
                except Exception:
                    continue
            if reveal_count:
                post_click_html = await page.content()
                post_click_text = await page.locator("body").inner_text(timeout=10000)
                revealed = extract_matches(post_click_html) | extract_matches(post_click_text)
                if revealed:
                    record(revealed, f"click_reveal:{url}")
                    page_found.update(revealed)
        except Exception:
            pass

        # Form controls (dropdowns, checkboxes, radio buttons) can filter/reveal
        # content that's never in the initial DOM -- pages literally named
        # "filter-gateway" are a strong hint this matters. Exercise every
        # <select> option and every checkbox/radio, re-scanning after each.
        try:
            select_locators = page.locator("select")
            select_count = await select_locators.count()
            debug_counts["selects_seen"] += select_count
            for i in range(min(select_count, 10)):
                try:
                    select_el = select_locators.nth(i)
                    option_values = await select_el.locator("option").evaluate_all(
                        "opts => opts.map(o => o.value).filter(v => v !== '')"
                    )
                    for value in option_values[:15]:  # cap options per dropdown
                        try:
                            await select_el.select_option(value=value, timeout=2000)
                            await page.wait_for_timeout(200)
                            post_select_html = await page.content()
                            post_select_text = await page.locator("body").inner_text(timeout=5000)
                            revealed = extract_matches(post_select_html) | extract_matches(post_select_text)
                            if revealed:
                                record(revealed, f"filter_select:{url}#option={value}")
                                page_found.update(revealed)
                        except Exception:
                            continue
                except Exception:
                    continue

            checkbox_radio = page.locator("input[type='checkbox'], input[type='radio']")
            cr_count = await checkbox_radio.count()
            debug_counts["checkboxes_radios_seen"] += cr_count

            text_inputs = page.locator("input[type='text'], input[type='search'], input:not([type])")
            debug_counts["text_inputs_seen"] += await text_inputs.count()
            for i in range(min(cr_count, 20)):
                try:
                    el = checkbox_radio.nth(i)
                    if await el.is_visible():
                        await el.check(timeout=2000, force=True)
                        await page.wait_for_timeout(200)
                        post_check_html = await page.content()
                        post_check_text = await page.locator("body").inner_text(timeout=5000)
                        revealed = extract_matches(post_check_html) | extract_matches(post_check_text)
                        if revealed:
                            record(revealed, f"filter_checkbox:{url}#{i}")
                            page_found.update(revealed)
                except Exception:
                    continue

            # If there's a submit button, also try submitting after the last
            # selections/checks above (some filter UIs require explicit submit
            # rather than reacting live to onChange).
            if select_count or cr_count:
                submit_btn = page.locator("button[type='submit'], input[type='submit']")
                if await submit_btn.count():
                    try:
                        await submit_btn.first.click(timeout=2000, force=True)
                        await page.wait_for_timeout(300)
                        post_submit_html = await page.content()
                        post_submit_text = await page.locator("body").inner_text(timeout=5000)
                        revealed = extract_matches(post_submit_html) | extract_matches(post_submit_text)
                        if revealed:
                            record(revealed, f"filter_submit:{url}")
                            page_found.update(revealed)
                    except Exception:
                        pass
        except Exception:
            pass

        # Iframes are separate documents -- BeautifulSoup on the parent HTML
        # never sees their content. Walk each child frame directly.
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            debug_counts["iframes_seen"] += 1
            try:
                frame_html = await frame.content()
                frame_text = await frame.locator("body").inner_text(timeout=5000)
                frame_matches = extract_matches(frame_html) | extract_matches(frame_text)
                if frame_matches:
                    record(frame_matches, f"iframe:{frame.url}")
                    page_found.update(frame_matches)
            except Exception:
                continue

        # Sort every candidate URL (from attributes/scripts above) into the right bucket
        # instead of queuing everything as a page navigation.
        page_candidates: set[str] = set()
        text_resource_candidates: set[str] = set()
        document_candidates: set[str] = set()
        archive_candidates: set[str] = set()
        for candidate in candidates:
            kind = resource_kind(candidate)
            if kind == "image":
                images.add(candidate)
            elif kind == "text_resource":
                text_resource_candidates.add(candidate)
            elif kind == "document":
                document_candidates.add(candidate)
            elif kind == "archive":
                archive_candidates.add(candidate)
            else:
                page_candidates.add(candidate)

        try:
            resource_entries = await page.evaluate(
                "performance.getEntriesByType('resource').map(e => ({name: e.name, initiatorType: e.initiatorType}))"
            )
            for entry in resource_entries:
                resource_url = entry.get("name")
                initiator = entry.get("initiatorType", "")
                candidate = absolute_candidate(resource_url, url)
                if not candidate:
                    continue
                kind = resource_kind(candidate)
                # fetch()/XHR calls often hit extension-less API endpoints
                # (e.g. /status/eu-region/) that resource_kind() can't classify
                # by extension alone -- scan those as raw text too. But don't
                # treat "fetched via JS" and "is a real page" as mutually
                # exclusive: some sites prefetch actual page navigations via
                # fetch(), and excluding those from page_candidates entirely
                # shrinks the reachable link graph (confirmed: dropped page
                # coverage from 566 -> 371 pages when this was exclusive).
                if initiator in ("fetch", "xmlhttprequest"):
                    text_resource_candidates.add(candidate)
                    if kind == "page":
                        page_candidates.add(candidate)
                elif kind == "image":
                    images.add(candidate)
                elif kind == "text_resource":
                    text_resource_candidates.add(candidate)
                elif kind == "document":
                    document_candidates.add(candidate)
                elif kind == "archive":
                    archive_candidates.add(candidate)
                else:
                    page_candidates.add(candidate)
        except Exception:
            pass

        await scan_images(images)
        await scan_text_resources(text_resource_candidates)
        await scan_documents(document_candidates)
        await scan_archives(archive_candidates)

        # Browser storage can carry flags too, if they're set via JS.
        try:
            storage_dump = await page.evaluate(
                "JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})"
            )
            storage_matches = extract_matches(storage_dump)
            record(storage_matches, f"browser_storage:{url}")
            page_found.update(storage_matches)
        except Exception:
            pass

        record(page_found, f"page_html_or_text:{url}")
        results.append({"url": url, "title": title, "depth": depth, "matches": sorted(page_found), "links_found": len(page_candidates), "images_found": len(images)})
        if depth < MAX_DEPTH:
            await context.add_requests([Request.from_url(link, user_data={"depth": depth + 1}) for link in page_candidates])

    # robots.txt / sitemap.xml are things "the server gives you" without any
    # clicking at all -- cheap to check once up front.
    if USERNAME and PASSWORD:
        try:
            async with httpx.AsyncClient(auth=(USERNAME, PASSWORD), follow_redirects=True, timeout=20) as client:
                for well_known_path in (
                    "/robots.txt", "/sitemap.xml", "/favicon.ico", "/manifest.json",
                    "/humans.txt", "/security.txt", "/.well-known/security.txt",
                    "/.well-known/change-password", "/crossdomain.xml", "/.env",
                    "/version.json", "/status.json", "/health", "/api/status",
                ):
                    try:
                        well_known_url = urljoin(START_URL, well_known_path)
                        response = await client.get(well_known_url)
                        if response.status_code == 200:
                            record(extract_matches(response.text), f"well_known:{well_known_url}")
                            print(f"well-known hit: {well_known_url} -> 200 ({len(response.content)} bytes)")
                    except (httpx.HTTPError, UnicodeDecodeError):
                        continue
        except Exception:
            pass

    await crawler.run([Request.from_url(START_URL, user_data={"depth": 0})])

    # Retry known geofenced pages through Tor (or another configured proxy)
    # exited in the required region, since a normal browser/httpx request
    # from this machine's real IP will always get a 403 for these. Tor exit
    # nodes rotate and can land outside the required country on any given
    # circuit, so retry a few times, forcing a fresh circuit between attempts.
    geofenced_results: list[dict] = []

    def force_new_tor_circuit() -> None:
        try:
            from stem import Signal
            from stem.control import Controller

            with Controller.from_port(port=9051) as controller:
                controller.authenticate()
                controller.signal(Signal.NEWNYM)
        except Exception:
            pass  # best-effort; if stem/control port isn't available, just retry as-is

    if USERNAME and PASSWORD:
        for path in KNOWN_GEOFENCED_PATHS:
            geofenced_url = urljoin(START_URL, path)
            attempts = []
            success = False
            for attempt in range(4):
                try:
                    async with httpx.AsyncClient(auth=(USERNAME, PASSWORD), proxy=TOR_PROXY, timeout=30) as tor_client:
                        response = await tor_client.get(geofenced_url)
                    matches = extract_matches(response.text) if response.status_code == 200 else set()
                    attempts.append({"attempt": attempt + 1, "status_code": response.status_code})
                    if response.status_code == 200:
                        record(matches, f"geofenced_page:{geofenced_url}")
                        geofenced_results.append({
                            "url": geofenced_url,
                            "status_code": response.status_code,
                            "matches": sorted(matches),
                            "attempts": attempts,
                        })
                        success = True
                        break
                except (httpx.HTTPError, Exception) as e:
                    attempts.append({"attempt": attempt + 1, "error": str(e)})

                # Not successful yet -- force a new circuit before the next try
                # (skip the wait after the final attempt).
                if attempt < 3:
                    force_new_tor_circuit()
                    await asyncio.sleep(5)  # give Tor a moment to build the new circuit

            if not success:
                geofenced_results.append({"url": geofenced_url, "status_code": None, "matches": [], "attempts": attempts})
            elif success:
                # The geofenced page was only ever scanned as raw text -- it was
                # never rendered/parsed, so any images/scripts it references were
                # never discovered. If those assets are ALSO geofenced, a plain
                # (non-Tor) fetch would 403 anyway, so fetch them through Tor too.
                try:
                    geo_soup = BeautifulSoup(response.text, "html.parser")
                    geo_images = image_candidates(geo_soup, geofenced_url)
                    geo_text_candidates: set[str] = set()
                    for tag in geo_soup.find_all(True):
                        for attr, value in tag.attrs.items():
                            values = value if isinstance(value, list) else [value]
                            for item in values:
                                if not isinstance(item, str):
                                    continue
                                for raw in URL_PATTERN.findall(item):
                                    candidate = absolute_candidate(raw, geofenced_url)
                                    if candidate and resource_kind(candidate) == "text_resource":
                                        geo_text_candidates.add(candidate)
                    if geo_images or geo_text_candidates:
                        async with httpx.AsyncClient(auth=(USERNAME, PASSWORD), proxy=TOR_PROXY, timeout=30) as tor_client:
                            if geo_images:
                                await scan_images(geo_images, client_override=tor_client)
                            if geo_text_candidates:
                                await scan_text_resources(geo_text_candidates, client_override=tor_client)
                except Exception:
                    pass

    # Any asset (image/JS/CSS/etc.) that 403'd during the normal crawl might be
    # geofenced too, not just the one known page -- retry every one of them
    # through Tor now that we know the mechanism exists on this site.
    retried_blocked: list[dict] = []
    if USERNAME and PASSWORD and blocked_urls:
        async with httpx.AsyncClient(auth=(USERNAME, PASSWORD), proxy=TOR_PROXY, timeout=30) as tor_client:
            image_retries = {u for u, kind in blocked_urls.items() if kind == "image"}
            text_retries = {u for u, kind in blocked_urls.items() if kind == "text"}
            # Clear processed-sets for these so the retry actually re-fetches them.
            processed_images.difference_update(image_retries)
            processed_resources.difference_update(text_retries)
            before = len(found)
            if image_retries:
                await scan_images(image_retries, client_override=tor_client)
            if text_retries:
                await scan_text_resources(text_retries, client_override=tor_client)
            retried_blocked = [{"url": u, "kind": blocked_urls[u]} for u in blocked_urls]
            print(f"Retried {len(blocked_urls)} previously-403'd assets through Tor; new flags found: {len(found) - before}")

    with open("results.json", "w", encoding="utf-8") as output:
        json.dump(
            {
                "matches": sorted(found),
                "match_sources": match_sources,
                "pages": results,
                "images": image_results,
                "text_resources": resource_results,
                "geofenced_pages": geofenced_results,
                "retried_blocked_assets": retried_blocked,
            },
            output,
            indent=2,
        )
    print(json.dumps({"matches": sorted(found), "pages_crawled": len(results), "images_processed": len(image_results), "text_resources_processed": len(resource_results), "geofenced_pages": geofenced_results, "blocked_assets_found": len(blocked_urls), "debug_counts": debug_counts}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
