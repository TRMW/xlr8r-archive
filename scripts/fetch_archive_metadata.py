"""
Pull XLR8R issue metadata from the Internet Archive's public APIs and
upsert it into the `issues` table.

This only stores metadata and links back to archive.org — it does not
download or rehost the underlying scans/PDFs. That keeps the archive
site itself as an index/reader that points at the source, rather than
a mirror of copyrighted material.

Usage:
    python fetch_archive_metadata.py --dsn postgresql://user:pass@host/db
    python fetch_archive_metadata.py --dry-run   # print what would be inserted
"""
import argparse
import re
import sys
import time
from datetime import date

import psycopg2
import psycopg2.extras
import requests

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"

# Broad net: anything in archive.org tagged/collected under XLR8R.
# Refine this query after a first dry-run once you've eyeballed results —
# the back-issue bundle and individually uploaded issues use different
# metadata conventions.
SEARCH_PARAMS = {
    "q": '(subject:"XLR8R" OR collection:"magazine_rack" AND title:"XLR8R")',
    "fl[]": ["identifier", "title", "date", "publicdate"],
    "rows": "200",
    "page": "1",
    "output": "json",
}

ISSUE_NUM_RE = re.compile(r"(?:issue|#)\s*(\d+)", re.IGNORECASE)


def search_items():
    resp = requests.get(SEARCH_URL, params=SEARCH_PARAMS, timeout=30)
    resp.raise_for_status()
    docs = resp.json()["response"]["docs"]
    return docs


def fetch_item_metadata(identifier: str) -> dict:
    resp = requests.get(METADATA_URL.format(identifier=identifier), timeout=30)
    resp.raise_for_status()
    return resp.json()


def guess_issue_number(title: str):
    if not title:
        return None
    m = ISSUE_NUM_RE.search(title)
    return int(m.group(1)) if m else None


def build_row(doc: dict, meta: dict) -> dict:
    identifier = doc["identifier"]
    title = doc.get("title", "")

    publish_date = None
    if doc.get("date"):
        try:
            publish_date = date.fromisoformat(doc["date"][:10])
        except ValueError:
            pass

    return {
        "identifier": identifier,
        "issue_number": guess_issue_number(title),
        "title": title,
        "publish_date": publish_date,
        "source": "archive_org",
        "source_url": f"https://archive.org/details/{identifier}",
        "page_count": meta.get("metadata", {}).get("imagecount"),
    }


UPSERT_ISSUE_SQL = """
INSERT INTO issues (identifier, issue_number, title, publish_date, source, source_url, page_count)
VALUES (%(identifier)s, %(issue_number)s, %(title)s, %(publish_date)s, %(source)s, %(source_url)s, %(page_count)s)
ON CONFLICT (identifier) DO UPDATE SET
    issue_number = EXCLUDED.issue_number,
    title = EXCLUDED.title,
    publish_date = EXCLUDED.publish_date,
    page_count = EXCLUDED.page_count
RETURNING id;
"""

# We embed archive.org's own BookReader viewer rather than downloading
# the PDF -- the reader stays live on archive.org, so pages render but
# nothing gets copied onto our storage.
UPSERT_CONTENT_LINK_SQL = """
INSERT INTO content_links (issue_id, source, link_type, url, title)
VALUES (%(issue_id)s, 'archive_org', 'pdf_embed', %(url)s, 'Read on archive.org')
ON CONFLICT (issue_id, url) DO NOTHING;
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn", help="Postgres DSN, e.g. postgresql://user:pass@host/db")
    parser.add_argument("--dry-run", action="store_true", help="Print rows instead of writing to the DB")
    args = parser.parse_args()

    if not args.dry_run and not args.dsn:
        sys.exit("Provide --dsn or run with --dry-run")

    conn = None
    if not args.dry_run:
        conn = psycopg2.connect(args.dsn)
        conn.autocommit = True

    docs = search_items()
    print(f"Found {len(docs)} candidate items", file=sys.stderr)

    for doc in docs:
        identifier = doc["identifier"]
        try:
            meta = fetch_item_metadata(identifier)
        except requests.RequestException as e:
            print(f"skip {identifier}: {e}", file=sys.stderr)
            continue

        row = build_row(doc, meta)
        embed_url = f"https://archive.org/embed/{identifier}"

        if args.dry_run:
            print(row, "->", embed_url)
        else:
            with conn.cursor() as cur:
                cur.execute(UPSERT_ISSUE_SQL, row)
                issue_id = cur.fetchone()[0]
                cur.execute(UPSERT_CONTENT_LINK_SQL, {"issue_id": issue_id, "url": embed_url})

        time.sleep(0.5)  # be polite to archive.org's API

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
