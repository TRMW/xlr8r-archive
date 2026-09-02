"""
Populate the `articles` table by pulling each issue's OCR'd full text
and splitting it into per-article chunks.

OCR text URLs come from `content_links` (link_type='ocr_text'), written
there by fetch_archive_metadata.py -- not re-derived from the issue's
`identifier` here. That matters because for issues pulled out of the
XLR8R101 bundle, `identifier` is a synthetic id ("XLR8R101-101") that
isn't a real archive.org item on its own; the actual OCR file lives
inside the bundle item under its own filename. Reading the URL straight
from content_links works the same way for bundle-derived and standalone
issues alike, with no special-casing needed here.

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
    python extract_articles.py --url https://archive.org/download/XLR8R101/XLR8R_101_djvu.txt
                                                          # test segmentation, no DB needed
"""
import argparse
import re
import sys
import time

import psycopg2
import psycopg2.extras
import requests

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


def fetch_text(url: str) -> str:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


def split_into_chunks(full_text: str):
    """Returns a list of (article_type, title, author, body_text)."""
    lines = full_text.splitlines()

    boundaries = []  # (line_index, article_type)
    for i, line in enumerate(lines):
        m = HEADER_LINE_RE.match(line.strip())
        if m:
            boundaries.append((i, SECTION_HEADERS[m.group(1).lower()]))

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

        body_lines = [l.strip() for l in body.splitlines() if l.strip()]
        title_lines = [l for l in body_lines if l.lower() not in SECTION_HEADERS]
        title = title_lines[0][:200] if title_lines else kind.title()

        author_match = BYLINE_RE.search(body)
        author = author_match.group(1).strip() if author_match else None

        chunks.append((kind, title, author, body))

    return chunks


FETCH_ISSUES_WITH_OCR_SQL = """
SELECT i.id, i.title, i.source_url, cl.url AS ocr_url
FROM issues i
JOIN content_links cl ON cl.issue_id = i.id AND cl.link_type = 'ocr_text'
WHERE 1=1
  {issue_filter}
ORDER BY i.id;
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
        "--url",
        help="Test segmentation on a direct OCR text URL, no database needed "
             "(e.g. --url https://archive.org/download/XLR8R101/XLR8R_101_djvu.txt)",
    )
    args = parser.parse_args()

    if args.url:
        full_text = fetch_text(args.url)
        chunks = split_into_chunks(full_text)
        print(f"{args.url}: {len(chunks)} chunk(s) found", file=sys.stderr)
        for kind, title, author, body in chunks:
            print(f"  [{kind}] {title!r} by {author!r} ({len(body)} chars)")
        return

    if not args.dry_run and not args.dsn:
        sys.exit("Provide --dsn or run with --dry-run")

    conn = psycopg2.connect(args.dsn) if not args.dry_run else None
    if conn:
        conn.autocommit = True

    if not conn:
        sys.exit("--dry-run currently still needs --dsn, to read the issue list. "
                 "It won't write anything -- just add --dsn alongside --dry-run.")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        issue_filter = "AND i.id = %(issue_id)s" if args.issue_id else ""
        cur.execute(
            FETCH_ISSUES_WITH_OCR_SQL.format(issue_filter=issue_filter),
            {"issue_id": args.issue_id},
        )
        issues = cur.fetchall()

    print(f"Processing {len(issues)} issue(s) with OCR text available", file=sys.stderr)

    for issue in issues:
        try:
            full_text = fetch_text(issue["ocr_url"])
        except requests.RequestException as e:
            print(f"skip issue {issue['id']}: text fetch failed ({e})", file=sys.stderr)
            continue

        chunks = split_into_chunks(full_text)
        print(f"issue {issue['id']} ({issue['title']}): {len(chunks)} chunk(s) found", file=sys.stderr)

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

        time.sleep(0.3)  # be polite to archive.org

    if conn:
        conn.close()


if __name__ == "__main__":
    main()
