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

Segmentation approach:
XLR8R's real per-article boundary is its byline convention -- a line
reading "WORDS: <author> PHOTO(S)/IMAGE(S)/ILLUSTRATION(S): <credit>".
A named department like Audiofile is many separate articles, each with
its own byline, not one block of text -- splitting on the department
header alone (an earlier version of this script did that) produces one
giant chunk per department, and picking "the first line" of that chunk
as a title grabs whatever OCR text happened to be first, which is
usually mid-sentence junk from an unrelated article.

Titles come from the block of ALL-CAPS lines directly above a byline
(this magazine sets titles/deks in caps, distinct from mixed-case body
prose). Content with no byline (capsule reviews, occasional short
byline-less pieces) falls back to coarse section-span chunking so
nothing is silently dropped, just less precisely titled -- reviews in
particular are dense, heavily OCR-corrupted blurbs not worth trying to
split individually.

This is still a heuristic, not a real layout parser -- OCR noise will
still produce some rough edges, particularly for older/lower-quality
scans. Verified against real fetched OCR text before being pointed at
production; expect it to need occasional review, not to be perfect.

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

# --- Segmentation -----------------------------------------------------

SECTION_KEYWORDS = {
    "audiofile": "audiofile",
    "machines": "machines",
    "vis-ed": "vis-ed",
    "vised": "vis-ed",
    "bitter bastard": "bitter-bastard",
    "reviews": "review",
}
STANDALONE_NOISE_WORDS = set(SECTION_KEYWORDS.keys()) | {"prefix"}

BYLINE_RE = re.compile(
    r"^\s*WORDS:\s*(?P<author>[A-Z][A-Za-z.&'\- ]*?)\s+"
    r"(?:PHOTOS?|IMAGES?|ILLUSTRATIONS?)\s*:\s*.+$"
)

# OCR-garbled page furniture -- running headers like "II PREFIX II AUDIOFILE"
# repeat on every page within a section. Not real content.
NOISE_RE = re.compile(r"^\s*II\s+[A-Z][A-Z \-]*\s*(II\s*)?[A-Z \-]*\s*$")

HEADER_LINE_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(k) for k in SECTION_KEYWORDS) + r")\s*$",
    re.IGNORECASE,
)

TITLE_LINE_MAX = 20  # chars; lines at/under this merge into a multi-line title
MIN_CHUNK_CHARS = 150
ALLOWED_TITLE_CHARS = re.compile(r"^[A-Za-z0-9 &'.,\-!?:()/\"]+$")


def is_shouty(line: str) -> bool:
    """True if the line's letters are overwhelmingly uppercase -- this
    magazine sets titles and deks entirely in caps, distinct from normal
    mixed-case body prose, which is a reliable signal to lean on."""
    letters = [c for c in line if c.isalpha()]
    if len(letters) < 2:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.8


def is_noise_line(line: str) -> bool:
    if NOISE_RE.match(line):
        return True
    if BYLINE_RE.match(line):
        return True
    return line.strip().lower() in STANDALONE_NOISE_WORDS


def coarse_section_spans(lines):
    """(start, end, kind) wherever a section name shows up as a heading --
    either alone on its own line, or embedded in a running-header noise
    line like 'II PREFIX II AUDIOFILE' (a section's own opening header is
    often OCR'd merged with that running-header prefix, so a
    keyword-alone match misses it). Single source of truth for which
    named section a line belongs to."""
    boundaries = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        m = HEADER_LINE_RE.match(stripped)
        if m:
            boundaries.append((i, SECTION_KEYWORDS[m.group(1).lower()]))
            continue
        if NOISE_RE.match(stripped):
            low = stripped.lower()
            matched = False
            for key, kind in SECTION_KEYWORDS.items():
                if re.search(rf"\b{re.escape(key)}\b", low):
                    boundaries.append((i, kind))
                    matched = True
                    break
            if not matched and re.search(r"\bprefix\b", low):
                boundaries.append((i, "feature"))

    spans = []
    first = boundaries[0][0] if boundaries else len(lines)
    if first > 0:
        spans.append((0, first, "feature"))
    for idx, (start, kind) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        spans.append((start, end, kind))
    return spans


def build_section_map(spans, num_lines):
    kind_by_line = ["feature"] * num_lines
    end_by_line = [num_lines] * num_lines
    for start, end, kind in spans:
        for i in range(start, min(end, num_lines)):
            kind_by_line[i] = kind
            end_by_line[i] = end
    return kind_by_line, end_by_line


def _clean_title(parts):
    title = " ".join(parts).strip(" -\u2013\u2014.")
    title = re.sub(r"[\u00ae\u00a9|]", "", title)
    title = re.sub(r"\s{2,}", " ", title).strip()
    if not title:
        return None
    # Reject titles that are mostly OCR garbage (stray symbols, junk from
    # misread cover-line/masthead text) rather than publish something like
    # a garbled cover strapline as if it were a real article title.
    if not ALLOWED_TITLE_CHARS.match(title):
        return None
    if len(title.split()) < 2 and len(title) < 4:
        return None
    return title


def extract_title(lines, byline_index):
    """Walk upward from a byline line, collecting the block of consecutive
    shouty (all-caps) lines directly above it -- that's the title/dek
    block, top-to-bottom order title-then-dek. Short leading lines (title
    fragments wrapped across lines, e.g. 'FAT' / 'FREDDY'S' / 'DROP')
    merge together; once cumulative length passes TITLE_LINE_MAX we've
    moved from the title into the longer dek sentence, so stop there."""
    block = []
    i = byline_index - 1
    while i >= 0 and i >= byline_index - 12:
        line = lines[i].strip()
        if not line:
            i -= 1
            continue
        if is_noise_line(line):
            i -= 1
            continue
        if not is_shouty(line):
            break
        block.insert(0, line)
        i -= 1

    if not block:
        return None

    kept, total = [], 0
    for line in block:
        if kept and total + len(line) > TITLE_LINE_MAX:
            break
        kept.append(line)
        total += len(line)

    return _clean_title(kept)


def forward_title(lines, start_index, limit_index):
    """Same idea as extract_title but scanning forward, for byline-less
    spans (no WORDS: line to anchor on)."""
    block = []
    i = start_index
    while i < limit_index and i < start_index + 12:
        line = lines[i].strip()
        if not line:
            i += 1
            if block:
                break
            continue
        if is_noise_line(line):
            i += 1
            continue
        if not is_shouty(line):
            break
        block.append(line)
        i += 1

    kept, total = [], 0
    for line in block:
        if kept and total + len(line) > TITLE_LINE_MAX:
            break
        kept.append(line)
        total += len(line)

    return _clean_title(kept)


def split_into_articles(full_text: str):
    """Returns a list of (article_type, title, author, body_text)."""
    lines = full_text.splitlines()
    spans = coarse_section_spans(lines)
    section_kind, section_end = build_section_map(spans, len(lines))

    byline_matches = [i for i, line in enumerate(lines) if BYLINE_RE.match(line.strip())]

    articles = []
    claimed = []  # every byline's range, claimed whether or not it became an article

    for idx, byline_i in enumerate(byline_matches):
        author = BYLINE_RE.match(lines[byline_i].strip()).group("author").strip()
        body_start = byline_i + 1
        next_byline = byline_matches[idx + 1] if idx + 1 < len(byline_matches) else len(lines)
        # Never let a body run past the end of its own section -- otherwise
        # the last byline article before a Reviews/Machines/etc. transition
        # swallows everything up to the next byline, which could be an
        # entire following section with no byline of its own yet.
        body_end = min(next_byline, section_end[byline_i])
        claim_start = max(0, byline_i - 12)
        claimed.append((claim_start, body_end))

        body = "\n".join(lines[body_start:body_end]).strip()
        if len(body) < MIN_CHUNK_CHARS:
            continue

        title = extract_title(lines, byline_i) or f"Untitled ({author})"
        kind = section_kind[byline_i]
        articles.append((kind, title, author, body))

    # Fallback: anything substantial inside a coarse section span that
    # wasn't claimed by a byline article becomes its own chunk (author
    # unknown) instead of being silently dropped.
    for start, end, kind in spans:
        cursor = start
        for cs, ce in sorted(c for c in claimed if c[0] < end and c[1] > start):
            gap_end = min(cs, end)
            if gap_end > cursor:
                gap_text = "\n".join(lines[cursor:gap_end]).strip()
                if len(gap_text) >= MIN_CHUNK_CHARS:
                    title = forward_title(lines, cursor, gap_end) or kind.title()
                    articles.append((kind, title, None, gap_text))
            cursor = max(cursor, ce)
        if cursor < end:
            gap_text = "\n".join(lines[cursor:end]).strip()
            if len(gap_text) >= MIN_CHUNK_CHARS:
                title = forward_title(lines, cursor, end) or kind.title()
                articles.append((kind, title, None, gap_text))

    return articles


# --- Fetching + DB plumbing --------------------------------------------


def fetch_text(url: str) -> str:
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    resp.encoding = resp.encoding or "utf-8"
    return resp.text


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
        articles = split_into_articles(full_text)
        print(f"{args.url}: {len(articles)} article(s) found", file=sys.stderr)
        for kind, title, author, body in articles:
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

        articles = split_into_articles(full_text)
        print(f"issue {issue['id']} ({issue['title']}): {len(articles)} article(s) found", file=sys.stderr)

        if args.dry_run:
            for kind, title, author, body in articles:
                print(f"  [{kind}] {title!r} by {author!r} ({len(body)} chars)")
            continue

        with conn.cursor() as cur:
            if args.replace:
                cur.execute(DELETE_EXISTING_ARTICLES_SQL, {"issue_id": issue["id"]})
            for kind, title, author, body in articles:
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
