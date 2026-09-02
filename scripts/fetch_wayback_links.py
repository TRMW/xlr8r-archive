"""
Find Wayback Machine snapshots of xlr8r.com's own per-issue pages and
attach them to the matching issue as a content link.

xlr8r.com used a clean URL scheme -- xlr8r.com/magazine/<issue number> --
for each issue's table of contents while the site was free (2003 onward,
pre-paywall). That number lets us match a snapshot straight to a row in
`issues` without any fuzzy matching.

We store the Wayback *page* URL (which replays the original page as it
looked, live from web.archive.org), never a downloaded copy.

Usage:
    python fetch_wayback_links.py --dsn postgresql://user:pass@host/db
    python fetch_wayback_links.py --dry-run
"""
import argparse
import re
import sys
import time

import psycopg2
import requests

CDX_URL = "https://web.archive.org/cdx/search/cdx"
MAGAZINE_URL_RE = re.compile(r"/magazine/(\d+)/?$")


def find_magazine_snapshots():
    """One snapshot per xlr8r.com/magazine/<n> URL (collapse=urlkey dedupes
    to the first capture we're offered for each distinct URL)."""
    params = {
        "url": "xlr8r.com/magazine/",
        "matchType": "prefix",
        "output": "json",
        "filter": ["statuscode:200", "mimetype:text/html"],
        "collapse": "urlkey",
        "limit": "5000",
    }
    resp = requests.get(CDX_URL, params=params, timeout=60)
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        return []
    header, *data = rows
    return [dict(zip(header, row)) for row in data]


def build_content_link(row: dict):
    original = row["original"]
    m = MAGAZINE_URL_RE.search(original)
    if not m:
        return None
    issue_number = int(m.group(1))
    snapshot_url = f"https://web.archive.org/web/{row['timestamp']}/{original}"
    return issue_number, {
        "source": "wayback",
        "link_type": "viewer",
        "url": snapshot_url,
        "title": f"xlr8r.com/magazine/{issue_number} (via Wayback Machine)",
    }


FIND_ISSUE_SQL = "SELECT id FROM issues WHERE issue_number = %(issue_number)s LIMIT 1;"

INSERT_LINK_SQL = """
INSERT INTO content_links (issue_id, source, link_type, url, title)
VALUES (%(issue_id)s, %(source)s, %(link_type)s, %(url)s, %(title)s)
ON CONFLICT (issue_id, url) DO NOTHING;
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.dry_run and not args.dsn:
        sys.exit("Provide --dsn or run with --dry-run")

    conn = psycopg2.connect(args.dsn) if not args.dry_run else None
    if conn:
        conn.autocommit = True

    rows = find_magazine_snapshots()
    print(f"Found {len(rows)} candidate snapshots", file=sys.stderr)

    matched, unmatched, no_issue_row = 0, 0, 0
    for row in rows:
        parsed = build_content_link(row)
        if parsed is None:
            unmatched += 1
            continue
        issue_number, link = parsed

        if args.dry_run:
            print(issue_number, "->", link["url"])
            matched += 1
            continue

        with conn.cursor() as cur:
            cur.execute(FIND_ISSUE_SQL, {"issue_number": issue_number})
            found = cur.fetchone()
            if not found:
                no_issue_row += 1
                continue
            link["issue_id"] = found[0]
            cur.execute(INSERT_LINK_SQL, link)
            matched += 1

        time.sleep(0.2)  # be polite to the CDX API

    if conn:
        conn.close()

    print(
        f"matched={matched} unmatched_url_pattern={unmatched} no_issue_row={no_issue_row}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
