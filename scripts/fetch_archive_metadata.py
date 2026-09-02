"""
Pull XLR8R issue metadata from the Internet Archive's public APIs and
upsert it into the `issues` table, along with content links for reading
and searching each issue.

This only stores metadata and links back to archive.org — it does not
download or rehost the underlying scans/PDFs. That keeps the archive
site itself as an index/reader that points at the source, rather than
a mirror of copyrighted material.

Two distinct sources on archive.org, handled differently:

1. THE BUNDLE ("XLR8R101" / "XLR8R Back Issues"): a single archive.org
   item containing per-issue files for issues 67-138 (except 87 and
   128, which are empty placeholders in the bundle). This is NOT one
   item per issue -- it's one item with files named like
   "XLR8R_101_djvu.txt" and "XLR8R_101.pdf" inside it. Its BookReader
   supports deep-linking to a specific issue via a sub-path
   (archive.org/details/XLR8R101/XLR8R_101), which is what we use for
   both the human-facing link and the embed.

2. STANDALONE ITEMS: individually uploaded issues that predate or fall
   outside the bundle's range (e.g. "xlr8r-issue-10", from 1994), each
   its own normal archive.org item with its own BookReader.

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

BUNDLE_IDENTIFIER = "XLR8R101"
BUNDLE_FILE_RE = re.compile(r"^XLR8R_(\d+)\.pdf$")

# Standalone per-issue items follow this identifier convention (e.g.
# "xlr8r-issue-10"). Narrow and precise on purpose -- a broader
# subject/collection search pulls in unrelated items (single-article
# archives, user manuals that happen to share keywords, etc).
STANDALONE_SEARCH_PARAMS = {
    "q": "identifier:xlr8r-issue-*",
    "fl[]": ["identifier", "title", "date"],
    "rows": "200",
    "output": "json",
}

ISSUE_NUM_RE = re.compile(r"(?:issue|#)\s*(\d+)", re.IGNORECASE)


def fetch_item_metadata(identifier: str) -> dict:
    resp = requests.get(METADATA_URL.format(identifier=identifier), timeout=30)
    resp.raise_for_status()
    return resp.json()


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

INSERT_LINK_SQL = """
INSERT INTO content_links (issue_id, source, link_type, url, title)
VALUES (%(issue_id)s, 'archive_org', %(link_type)s, %(url)s, %(title)s)
ON CONFLICT (issue_id, url) DO NOTHING;
"""


def upsert_issue(conn, row, dry_run):
    if dry_run:
        print(row)
        return None
    with conn.cursor() as cur:
        cur.execute(UPSERT_ISSUE_SQL, row)
        return cur.fetchone()[0]


def upsert_link(conn, issue_id, link_type, url, title, dry_run):
    if dry_run:
        print("  ->", link_type, url)
        return
    with conn.cursor() as cur:
        cur.execute(
            INSERT_LINK_SQL,
            {"issue_id": issue_id, "link_type": link_type, "url": url, "title": title},
        )


def process_bundle(conn, dry_run):
    print(f"Fetching bundle metadata for {BUNDLE_IDENTIFIER}...", file=sys.stderr)
    meta = fetch_item_metadata(BUNDLE_IDENTIFIER)
    files = meta.get("files", [])

    issue_numbers = sorted(
        {
            int(m.group(1))
            for f in files
            if (m := BUNDLE_FILE_RE.match(f.get("name", "")))
        }
    )
    # The bundle lists an XLR8R_87.pdf, but it's a near-empty placeholder
    # (a few hundred bytes) -- filter out files under 1KB as not-really-there.
    real_sizes = {f["name"]: int(f.get("size", 0)) for f in files}

    print(f"Found {len(issue_numbers)} issues in the bundle", file=sys.stderr)

    count = 0
    for num in issue_numbers:
        pdf_name = f"XLR8R_{num}.pdf"
        if real_sizes.get(pdf_name, 0) < 1024:
            print(f"  skip issue {num}: placeholder/empty file in bundle", file=sys.stderr)
            continue

        row = {
            "identifier": f"{BUNDLE_IDENTIFIER}-{num}",
            "issue_number": num,
            "title": f"XLR8R Issue {num}",
            "publish_date": None,
            "source": "archive_org",
            "source_url": f"https://archive.org/details/{BUNDLE_IDENTIFIER}/XLR8R_{num}",
            "page_count": None,
        }
        issue_id = upsert_issue(conn, row, dry_run)

        embed_url = f"https://archive.org/embed/{BUNDLE_IDENTIFIER}/XLR8R_{num}"
        upsert_link(conn, issue_id, "pdf_embed", embed_url, "Read on archive.org", dry_run)

        ocr_name = f"XLR8R_{num}_djvu.txt"
        if ocr_name in real_sizes:
            ocr_url = f"https://archive.org/download/{BUNDLE_IDENTIFIER}/{ocr_name}"
            upsert_link(conn, issue_id, "ocr_text", ocr_url, None, dry_run)

        count += 1
        if not dry_run:
            time.sleep(0.05)  # these are all one API call already fetched; just be gentle on writes

    print(f"Bundle: upserted {count} issues", file=sys.stderr)


def guess_issue_number(title: str):
    if not title:
        return None
    m = ISSUE_NUM_RE.search(title)
    return int(m.group(1)) if m else None


def process_standalone(conn, dry_run):
    print("Searching for standalone per-issue items...", file=sys.stderr)
    resp = requests.get(SEARCH_URL, params=STANDALONE_SEARCH_PARAMS, timeout=30)
    resp.raise_for_status()
    docs = resp.json()["response"]["docs"]
    print(f"Found {len(docs)} standalone items", file=sys.stderr)

    for doc in docs:
        identifier = doc["identifier"]
        try:
            meta = fetch_item_metadata(identifier)
        except requests.RequestException as e:
            print(f"  skip {identifier}: {e}", file=sys.stderr)
            continue

        title = doc.get("title", "")
        publish_date = None
        if doc.get("date"):
            try:
                publish_date = date.fromisoformat(doc["date"][:10])
            except ValueError:
                pass

        row = {
            "identifier": identifier,
            "issue_number": guess_issue_number(title),
            "title": title,
            "publish_date": publish_date,
            "source": "archive_org",
            "source_url": f"https://archive.org/details/{identifier}",
            "page_count": meta.get("metadata", {}).get("imagecount"),
        }
        issue_id = upsert_issue(conn, row, dry_run)

        embed_url = f"https://archive.org/embed/{identifier}"
        upsert_link(conn, issue_id, "pdf_embed", embed_url, "Read on archive.org", dry_run)

        for f in meta.get("files", []):
            name = f.get("name", "")
            if f.get("format") == "DjVuTXT" or name.endswith("_djvu.txt"):
                ocr_url = f"https://archive.org/download/{identifier}/{name}"
                upsert_link(conn, issue_id, "ocr_text", ocr_url, None, dry_run)
                break

        if not dry_run:
            time.sleep(0.5)  # separate API calls per item -- be polite


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

    process_bundle(conn, args.dry_run)
    process_standalone(conn, args.dry_run)

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
