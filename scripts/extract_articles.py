"""
Populate the `articles` table by pulling each issue's OCR'd full text
from archive.org (the "_djvu.txt" file that scanned items usually have)
and splitting it into per-article chunks.

Important: `body_text` is stored for full-text SEARCH only. The API
(`ArticleOut` in schemas.py) never returns it, and the issue page never
renders it -- only title/author/type/short excerpts should ever reach a
visitor. Storing raw OCR text server-side to power search is a different
thing from publishing full article text, and this pipeline is built to
keep the second door closed: don't add body_text to any response schema
without thinking hard about it first.

Segmentation approach (heuristic, not exact):
XLR8R issues have recurring named sections -- Audiofile, Machines,
Vis-Ed, Bitter Bastard, and Reviews -- which show up as short header-like
lines in the OCR text. We split on those. Everything before the first
recognized header becomes a single "feature" chunk (cover story / lead
features, which don't have a consistent header). This is a first pass:
OCR noise and layout quirks (multi-column pages interleaving text) will
produce some garbage chunks and mis-attributed authors. Treat the output
as something to spot-check and refine, not a finished dataset.

Usage:
    python extract_articles.py --dsn postgresql://user:pass@host/db --dry-run
    python extract_articles.py --dsn postgresql://user:pass@host/db
    python extract_articles.py --dsn ... --issue-id 42   # just one issue
"""
import argparse
import re
import sys
import time

import psycopg2
import psycopg2.extras
import requests

METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{filename}"

SECTION_HEADERS = {
    "audiofile": "audiofile",
    "machines": "machines",
    "vis-ed": "vis-ed",
    "vised": "vis-ed",
    "bitter bastard": "bitter-bastard",
    "reviews": "review",
}
HEADER_LINE_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(k) for k in SECTION_HEADERS) + r")\s*$",
    re.IGNORECASE,
)

# "by Firstname Lastname" near the top of a chunk, allowing OCR-typical
# punctuation noise around it.
BYLINE_RE = re.compile(r"^\s*by\s+([A-Z][A-Za-z.'-]+(?:\s+[A-Z][A-Za-z.'-]+){0,3})\s*$", re.MULTILINE)

MIN_CHUNK_CHARS = 200  # drop slivers that are almost certainly OCR noise, not a real article


def find_djvu_txt_url(identifier: str):
    resp = requests.get(METADATA_URL.format(identifier=identifier), timeout=30)
    resp.raise_for_status()
    meta = resp.json()
    for f in meta.get("files", []):
        name = f.get("name", "")
        fmt = f.get("format", "")
        if fmt == "DjVuTXT" or name.endswith("_djvu.txt"):
            return DOWNLOAD_URL.format(identifier=identifier, filename=name)
    return None


def fetch_text(url: str) -> str:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def split_into_chunks(full_text: str):
    """Returns a list of (article_type, title, author, body_text)."""
    lines = full_text.splitlines()

    # Find header line indices and their section type.
    boundaries = []  # (line_index, article_type)
    for i, line in enumerate(lines):
        m = HEADER_LINE_RE.match(line.strip())
        if m:
            boundaries.append((i, SECTION_HEADERS[m.group(1).lower()]))

    # Build (start, end, type) spans. Everything before the first
    # boundary is one lead "feature" chunk (may be empty if the issue
    # opens straight into a named section, which is fine).
    spans = []
    first = boundaries[0][0] if boundaries else len(lines)
    if first > 0:
        spans.append((0, first, "feature"))
    for idx, (start, kind) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        spans.append((start, end, kind))

    chunks = []
    for start, end, kind in spans:
        body = "\n".join(lines[start:end]).strip()
        if len(body) < MIN_CHUNK_CHARS:
            continue

        # Title: first non-empty line, skipping the header line itself.
        body_lines = [l.strip() for l in body.splitlines() if l.strip()]
        title_lines = [l for l in body_lines if l.lower() not in SECTION_HEADERS]
        title = title_lines[0][:200] if title_lines else kind.title()

        author_match = BYLINE_RE.search(body)
        author = author_match.group(1).strip() if author_match else None

        chunks.append((kind, title, author, body))

    return chunks


FETCH_ARCHIVE_ISSUES_SQL = """
SELECT id, identifier, source_url FROM issues
WHERE source = 'archive_org'
  {issue_filter}
ORDER BY id;
"""

DELETE_EXISTING_ARTICLES_SQL = "DELETE FROM articles WHERE issue_id = %(issue_id)s;"

INSERT_ARTICLE_SQL = """
INSERT INTO articles (issue_id, title, author, article_type, body_text, source_url)
VALUES (%(issue_id)s, %(title)s, %(author)s, %(article_type)s, %(body_text)s, %(source_url)s);
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dsn")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--issue-id", type=int, help="Process a single issue by its DB id")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="Delete existing articles for an issue before inserting (default: append)",
    )
    parser.add_argument(
        "--identifier",
        help="Test segmentation on a single archive.org identifier directly, no database needed "
             "(e.g. --identifier xlr8r-issue-10). Always dry-run.",
    )
    args = parser.parse_args()

    if args.identifier:
        try:
            txt_url = find_djvu_txt_url(args.identifier)
        except requests.RequestException as e:
            sys.exit(f"metadata fetch failed: {e}")
        if not txt_url:
            sys.exit("no OCR text file found for that identifier")
        full_text = fetch_text(txt_url)
        chunks = split_into_chunks(full_text)
        print(f"{args.identifier}: {len(chunks)} chunk(s) found", file=sys.stderr)
        for kind, title, author, body in chunks:
            print(f"  [{kind}] {title!r} by {author!r} ({len(body)} chars)")
        return

    if not args.dry_run and not args.dsn:
        sys.exit("Provide --dsn or run with --dry-run")

    conn = psycopg2.connect(args.dsn) if not args.dry_run else None
    if conn:
        conn.autocommit = True

    # Read issue list.
    if conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            issue_filter = "AND id = %(issue_id)s" if args.issue_id else ""
            cur.execute(
                FETCH_ARCHIVE_ISSUES_SQL.format(issue_filter=issue_filter),
                {"issue_id": args.issue_id},
            )
            issues = cur.fetchall()
    else:
        # dry-run without a DB: nothing to read issues from, so require --issue-id
        # to be paired with a manual identifier isn't supported here -- dry-run
        # still needs a DB to know which issues/identifiers exist.
        sys.exit("--dry-run currently still needs --dsn, to read the issue list. "
                 "It won't write anything -- just add --dsn alongside --dry-run.")

    print(f"Processing {len(issues)} archive.org issue(s)", file=sys.stderr)

    for issue in issues:
        identifier = issue["identifier"]
        try:
            txt_url = find_djvu_txt_url(identifier)
        except requests.RequestException as e:
            print(f"skip {identifier}: metadata fetch failed ({e})", file=sys.stderr)
            continue

        if not txt_url:
            print(f"skip {identifier}: no OCR text file found", file=sys.stderr)
            continue

        try:
            full_text = fetch_text(txt_url)
        except requests.RequestException as e:
            print(f"skip {identifier}: text fetch failed ({e})", file=sys.stderr)
            continue

        chunks = split_into_chunks(full_text)
        print(f"{identifier}: {len(chunks)} chunk(s) found", file=sys.stderr)

        if args.dry_run:
            for kind, title, author, body in chunks:
                print(f"  [{kind}] {title!r} by {author!r} ({len(body)} chars)")
            continue

        with conn.cursor() as cur:
            if args.replace:
                cur.execute(DELETE_EXISTING_ARTICLES_SQL, {"issue_id": issue["id"]})
            for kind, title, author, body in chunks:
                cur.execute(
                    INSERT_ARTICLE_SQL,
                    {
                        "issue_id": issue["id"],
                        "title": title,
                        "author": author,
                        "article_type": kind,
                        "body_text": body,
                        "source_url": issue["source_url"],
                    },
                )

        time.sleep(0.5)  # be polite to archive.org

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
