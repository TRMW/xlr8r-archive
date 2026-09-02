"""
Crawl media.hyperreal.org's XLR8R zine pages and attach each one to the
matching issue as a content link -- creating the issue row too if it
doesn't exist yet (hyperreal covers the earliest zine-era issues, several
of which archive.org doesn't have as separate items).

This site predates any clean URL scheme (it's 1994-era static HTML), so
unlike the Wayback scraper we can't infer the issue number from the URL.
Instead we crawl same-domain links from the seed page and pattern-match
each page's own text, which reliably states e.g. "This is Issue 9 of the
magazine, published February 1994." Coverage here will be sparse -- this
corner of the site was never large -- so a handful of matches is expected,
not a bug.

Usage:
    python fetch_hyperreal_links.py --dsn postgresql://user:pass@host/db
    python fetch_hyperreal_links.py --dry-run
"""
import argparse
import re
import sys
import time
from urllib.parse import urljoin, urlparse

import psycopg2
import requests
from bs4 import BeautifulSoup

SEED_URL = "http://media.hyperreal.org/zines/xlr8r/"
ALLOWED_PREFIX = "media.hyperreal.org/zines/xlr8r"

ISSUE_STATEMENT_RE = re.compile(
    r"Issue\s+(\d+)\s+of the magazine,\s+published\s+([A-Za-z]+\s+\d{4})",
    re.IGNORECASE,
)

HEADERS = {"User-Agent": "xlr8r-archive-indexer/0.1 (metadata + linking only, no rehosting)"}


def same_site(url: str) -> bool:
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}".startswith(ALLOWED_PREFIX) or ALLOWED_PREFIX in url


def crawl(seed: str, max_pages: int = 200):
    """Breadth-first crawl restricted to the zine's own subtree."""
    seen = {seed}
    queue = [seed]
    pages = []

    while queue and len(pages) < max_pages:
        url = queue.pop(0)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"skip {url}: {e}", file=sys.stderr)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        pages.append((url, soup))

        for a in soup.find_all("a", href=True):
            next_url = urljoin(url, a["href"])
            next_url = next_url.split("#")[0]
            if next_url not in seen and same_site(next_url):
                seen.add(next_url)
                queue.append(next_url)

        time.sleep(0.3)  # be polite -- this is a small, old, personally-run server

    return pages


def extract_issue_info(url: str, soup: BeautifulSoup):
    text = soup.get_text(" ", strip=True)
    m = ISSUE_STATEMENT_RE.search(text)
    if not m:
        return None
    issue_number = int(m.group(1))
    published = m.group(2)  # e.g. "February 1994"
    title = soup.title.string.strip() if soup.title and soup.title.string else f"XLR8R Issue {issue_number}"
    return {
        "issue_number": issue_number,
        "published_text": published,
        "title": title,
        "url": url,
    }


FIND_ISSUE_SQL = "SELECT id FROM issues WHERE issue_number = %(issue_number)s LIMIT 1;"

INSERT_ISSUE_SQL = """
INSERT INTO issues (identifier, issue_number, title, source, source_url)
VALUES (%(identifier)s, %(issue_number)s, %(title)s, 'hyperreal', %(url)s)
ON CONFLICT (identifier) DO UPDATE SET source_url = EXCLUDED.source_url
RETURNING id;
"""

INSERT_LINK_SQL = """
INSERT INTO content_links (issue_id, source, link_type, url, title)
VALUES (%(issue_id)s, 'hyperreal', 'article_page', %(url)s, %(title)s)
ON CONFLICT (issue_id, url) DO NOTHING;
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--seed", default=SEED_URL)
    args = parser.parse_args()

    if not args.dry_run and not args.dsn:
        sys.exit("Provide --dsn or run with --dry-run")

    conn = psycopg2.connect(args.dsn) if not args.dry_run else None
    if conn:
        conn.autocommit = True

    pages = crawl(args.seed)
    print(f"Crawled {len(pages)} pages under {ALLOWED_PREFIX}", file=sys.stderr)

    found_count = 0
    for url, soup in pages:
        info = extract_issue_info(url, soup)
        if not info:
            continue
        found_count += 1

        if args.dry_run:
            print(info)
            continue

        with conn.cursor() as cur:
            cur.execute(FIND_ISSUE_SQL, {"issue_number": info["issue_number"]})
            row = cur.fetchone()
            if row:
                issue_id = row[0]
            else:
                # Not in the DB yet (archive.org doesn't have this one) -- add it.
                cur.execute(
                    INSERT_ISSUE_SQL,
                    {
                        "identifier": f"hyperreal-issue-{info['issue_number']}",
                        "issue_number": info["issue_number"],
                        "title": f"XLR8R Issue {info['issue_number']}",
                        "url": args.seed,
                    },
                )
                issue_id = cur.fetchone()[0]

            cur.execute(
                INSERT_LINK_SQL,
                {"issue_id": issue_id, "url": info["url"], "title": info["title"]},
            )

    if conn:
        conn.close()

    print(f"pages_with_recognizable_issue_number={found_count}", file=sys.stderr)


if __name__ == "__main__":
    main()
