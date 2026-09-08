"""
Recover XLR8R's zine-era issues (the 1993-94 Seattle newsprint period)
from media.hyperreal.org, via the Wayback Machine.

Why via Wayback rather than direct: media.hyperreal.org does not resolve
from inside Railway's network, though it's reachable elsewhere -- the
earlier direct-crawl version of this script failed with a DNS error on
every run. Going through web.archive.org fixes that (Railway reaches it
fine, since fetch_wayback_links.py already does), and has two other
advantages: the CDX API enumerates the whole subtree for us rather than
needing a crawler, and Wayback replay URLs are stable even if that
1990s-era server goes away.

This is the only lead we have for issues below #67. The archive.org
bundle starts at #67, and Wayback captures of xlr8r.com only begin once
that site existed, which post-dates the zine era entirely. Coverage here
will still be sparse -- this corner of hyperreal was never large -- so a
handful of issues is the expected outcome, not a bug.

Usage:
    python fetch_hyperreal_links.py --dsn postgresql://user:pass@host/db
    python fetch_hyperreal_links.py --dsn ... --dry-run
"""
import argparse
import re
import sys
import time

import psycopg2
import requests
from bs4 import BeautifulSoup

CDX_URL = "https://web.archive.org/cdx/search/cdx"
SUBTREE = "media.hyperreal.org/zines/xlr8r"

HEADERS = {"User-Agent": "xlr8r-archive-indexer/0.1 (metadata + linking only, no rehosting)"}

# The site states its own issue number in prose, e.g.
# "This is Issue 9 of the magazine, published February 1994."
# There's no clean URL scheme to infer it from (1994-era static HTML),
# so parse what the page says about itself.
ISSUE_STATEMENT_RE = re.compile(
    r"Issue\s+(\d+)\s+of the magazine,\s+published\s+([A-Za-z]+\s+\d{4})",
    re.IGNORECASE,
)
# Looser fallback: a heading like "Contents of Issue 9".
ISSUE_LOOSE_RE = re.compile(r"\bIssue\s+(\d+)\b", re.IGNORECASE)

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def list_snapshots():
    """Enumerate every archived URL under the zine's subtree. collapse=urlkey
    gives one capture per distinct URL rather than every capture ever."""
    params = {
        "url": f"{SUBTREE}*",
        "output": "json",
        "collapse": "urlkey",
        "filter": ["statuscode:200", "mimetype:text/html"],
        "limit": "500",
    }
    resp = requests.get(CDX_URL, params=params, headers=HEADERS, timeout=90)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return []
    header, *data = rows
    return [dict(zip(header, r)) for r in data]


def replay_url(row, raw=False):
    """Wayback replay URL. The 'id_' suffix asks for the original bytes
    without Wayback's own navigation chrome injected, which keeps our
    text parsing clean."""
    stamp = row["timestamp"] + ("id_" if raw else "")
    return f"https://web.archive.org/web/{stamp}/{row['original']}"


def parse_issue(html: str):
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    published = None
    m = ISSUE_STATEMENT_RE.search(text)
    if m:
        issue_number = int(m.group(1))
        published = m.group(2)
    else:
        m2 = ISSUE_LOOSE_RE.search(text)
        if not m2:
            return None
        issue_number = int(m2.group(1))

    publish_date = None
    if published:
        parts = published.split()
        if len(parts) == 2 and parts[0].lower() in MONTHS:
            publish_date = f"{parts[1]}-{MONTHS[parts[0].lower()]:02d}-01"

    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()

    return {
        "issue_number": issue_number,
        "publish_date": publish_date,
        "title": title or f"XLR8R Issue {issue_number}",
    }


FIND_ISSUE_SQL = "SELECT id, source FROM issues WHERE issue_number = %(issue_number)s LIMIT 1;"

INSERT_ISSUE_SQL = """
INSERT INTO issues (identifier, issue_number, title, publish_date, source, source_url)
VALUES (%(identifier)s, %(issue_number)s, %(title)s, %(publish_date)s, 'hyperreal', %(source_url)s)
ON CONFLICT (identifier) DO UPDATE SET
    title = EXCLUDED.title,
    publish_date = COALESCE(EXCLUDED.publish_date, issues.publish_date),
    source_url = EXCLUDED.source_url
RETURNING id;
"""

INSERT_LINK_SQL = """
INSERT INTO content_links (issue_id, source, link_type, url, title)
VALUES (%(issue_id)s, 'hyperreal', %(link_type)s, %(url)s, %(title)s)
ON CONFLICT (issue_id, url) DO NOTHING;
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not args.dsn:
        sys.exit("Provide --dsn or run with --dry-run")

    conn = psycopg2.connect(args.dsn) if args.dsn else None
    if conn:
        conn.autocommit = True

    try:
        rows = list_snapshots()
    except requests.RequestException as e:
        sys.exit(f"CDX enumeration failed: {e}")

    print(f"Found {len(rows)} archived pages under {SUBTREE}", file=sys.stderr)

    found, created, attached = 0, 0, 0
    for row in rows:
        url = replay_url(row, raw=True)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=45)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"  skip {row['original']}: {e}", file=sys.stderr)
            continue

        info = parse_issue(resp.text)
        if not info:
            continue
        found += 1

        # Link to the normal replay URL (with Wayback's chrome), which is
        # the friendlier thing to hand a visitor.
        human_url = replay_url(row, raw=False)

        if args.dry_run or not conn:
            print(f"  issue {info['issue_number']} ({info['publish_date']}) -> {human_url}")
            continue

        with conn.cursor() as cur:
            cur.execute(FIND_ISSUE_SQL, {"issue_number": info["issue_number"]})
            existing = cur.fetchone()

            if existing:
                # Already have this issue from a better source (an actual
                # scan) -- just attach hyperreal as an extra content link
                # rather than overwriting the issue row.
                issue_id = existing[0]
            else:
                cur.execute(
                    INSERT_ISSUE_SQL,
                    {
                        "identifier": f"hyperreal-issue-{info['issue_number']}",
                        "issue_number": info["issue_number"],
                        "title": info["title"],
                        "publish_date": info["publish_date"],
                        "source_url": human_url,
                    },
                )
                issue_id = cur.fetchone()[0]
                created += 1

            cur.execute(
                INSERT_LINK_SQL,
                {
                    "issue_id": issue_id,
                    "link_type": "article_page",
                    "url": human_url,
                    "title": info["title"],
                },
            )
            attached += 1

        time.sleep(0.3)  # be polite to the Wayback Machine

    if conn:
        conn.close()

    print(
        f"pages_with_issue_number={found} issues_created={created} links_attached={attached}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
