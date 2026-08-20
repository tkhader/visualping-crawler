# Visualping Marker Crawler

Authorized crawler for finding markers matching `VISUALPING{...}` across web pages and linked resources.

## Safety and authorization

Use this only against targets you are authorized to crawl. The default target is the supplied interview endpoint. Keep the host allowlist enabled, respect robots.txt and rate limits, and do not crawl private or local-network targets.

Credentials are read from environment variables and are never committed.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
```

Optional OCR support requires the Tesseract executable installed separately.

## Configuration

```bash
export CRAWLER_START_URL='http://54.214.7.161/'
export CRAWLER_USERNAME='your-username'
export CRAWLER_PASSWORD='your-rotated-password'
export CRAWLER_ALLOWED_HOST='54.214.7.161'
```

Run:

```bash
python -m crawler.main
```

Results are written to `results.json`.
