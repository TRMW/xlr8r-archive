"""
Remove duplicate article rows.

The content-pipeline service auto-deploys on every push, and before
extract_articles.py grew a --replace flag each run appended another
full copy of every article. This deletes the extra copies, keeping the
lowest id of each set.

Deliberately conservative: rows only count as duplicates when issue,
title, type, author AND body text all match, so two genuinely different
articles that happen to share a title are never collapsed into one.

Safe to re-run -- once there are no duplicates it deletes nothing.
"""
import os

import psycopg2

DEDUPE_SQL = """
DELETE FROM articles a
USING articles b
WHERE a.id > b.id
  AND a.issue_id     IS NOT DISTINCT FROM b.issue_id
  AND a.title        IS NOT DISTINCT FROM b.title
  AND a.article_type IS NOT DISTINCT FROM b.article_type
  AND a.author       IS NOT DISTINCT FROM b.author
  AND a.body_text    IS NOT DISTINCT FROM b.body_text;
"""

conn = psycopg2.connect(os.environ["DATABASE_URL"])
conn.autocommit = True
cur = conn.cursor()

cur.execute("SELECT count(*) FROM articles")
before = cur.fetchone()[0]

cur.execute(DEDUPE_SQL)
removed = cur.rowcount

cur.execute("SELECT count(*) FROM articles")
after = cur.fetchone()[0]

print(f"DEDUPE before={before} removed={removed} after={after}")

# Anything left that still shares (issue, title, type) is a real
# collision worth knowing about rather than a pipeline artifact.
cur.execute("""
    SELECT count(*) FROM (
      SELECT issue_id, title, article_type
      FROM articles GROUP BY 1,2,3 HAVING count(*) > 1
    ) x
""")
print("remaining same-title groups (not exact dupes):", cur.fetchone()[0])

conn.close()
