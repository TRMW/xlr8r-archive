"""
One-off production audit. Checks data integrity in the DB and exercises
the live API over Railway's internal network (the API isn't reachable
from a dev sandbox, so this runs from inside the project instead).

Run by pointing the content-pipeline service's start command at it.
"""
import os
import sys

import psycopg2
import requests

API = "http://backend.railway.internal:8080"

conn = psycopg2.connect(os.environ["DATABASE_URL"])
cur = conn.cursor()


def q(sql):
    cur.execute(sql)
    return cur.fetchall()


print("=" * 60)
print("DATA INTEGRITY")
print("=" * 60)

print("issues            :", q("SELECT count(*) FROM issues")[0][0])
print("articles          :", q("SELECT count(*) FROM articles")[0][0])
print("content_links     :", q("SELECT count(*) FROM content_links")[0][0])
print("artists           :", q("SELECT count(*) FROM artists")[0][0])

# The search index is populated by a DB trigger. If articles were ever
# inserted without it firing, search silently returns nothing.
null_vec = q("SELECT count(*) FROM articles WHERE search_vector IS NULL")[0][0]
print("articles w/o search_vector:", null_vec, "<-- MUST BE 0")

print("articles w/o source_url   :", q("SELECT count(*) FROM articles WHERE source_url IS NULL")[0][0])
print("issues w/o pdf_embed      :", q("""
    SELECT count(*) FROM issues i WHERE NOT EXISTS (
      SELECT 1 FROM content_links c WHERE c.issue_id=i.id AND c.link_type='pdf_embed')
""")[0][0])
print("issues w/o any article    :", q("""
    SELECT count(*) FROM issues i WHERE NOT EXISTS (
      SELECT 1 FROM articles a WHERE a.issue_id=i.id)
""")[0][0])
print("duplicate issue_numbers   :", q("""
    SELECT count(*) FROM (
      SELECT issue_number FROM issues WHERE issue_number IS NOT NULL
      GROUP BY issue_number HAVING count(*) > 1) x
""")[0][0])

print("\nissue id range    :", q("SELECT min(id), max(id) FROM issues")[0])
print("issue_number range:", q("SELECT min(issue_number), max(issue_number) FROM issues")[0])

print("\nsample embed urls:")
for r in q("SELECT url FROM content_links WHERE link_type='pdf_embed' ORDER BY id LIMIT 3"):
    print("   ", r[0])

print("\nreal tsquery test (SQL level):")
for term in ("techno", "aphex", "review", "detroit"):
    n = q(f"SELECT count(*) FROM articles WHERE search_vector @@ plainto_tsquery('english','{term}')")[0][0]
    print(f"    {term:10s} -> {n} matches")

print("\n" + "=" * 60)
print("LIVE API (via internal network)")
print("=" * 60)

checks = [
    ("/health", None),
    ("/stats", None),
    ("/issues?limit=3", None),
    ("/articles/search?q=techno", "search"),
    ("/articles/search?q=aphex", "search"),
    ("/articles/search?q=", "search"),
    ("/issues/999999/full", "expect404"),
]

for path, kind in checks:
    try:
        r = requests.get(API + path, timeout=20)
        body = r.text[:180]
        flag = ""
        if kind == "expect404" and r.status_code != 404:
            flag = "  <-- EXPECTED 404"
        if kind == "search" and r.status_code != 200:
            flag = "  <-- SEARCH BROKEN"
        print(f"[{r.status_code}] {path}{flag}")
        print(f"      {body}")
    except Exception as e:
        print(f"[ERR] {path}: {e}")

# Every issue id the listing returns must resolve on the detail endpoint.
print("\nchecking every issue link resolves...")
try:
    ids = [i["id"] for i in requests.get(API + "/issues?limit=500", timeout=30).json()]
    bad = []
    for i in ids:
        r = requests.get(f"{API}/issues/{i}/full", timeout=20)
        if r.status_code != 200:
            bad.append((i, r.status_code))
    print(f"    checked {len(ids)} issues, broken: {bad if bad else 'NONE'}")
except Exception as e:
    print("    failed:", e)

print("\nAUDIT_COMPLETE")
conn.close()
