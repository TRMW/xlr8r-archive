# XLR8R archive

A per-issue index over existing public archives of XLR8R magazine
(Internet Archive, Wayback Machine snapshots of the old xlr8r.com, and the
hyperreal.org zine scans). Each issue page assembles metadata, articles,
and every piece of content found for it -- but content is always embedded
or linked live from its source (archive.org's own BookReader viewer, a
Wayback snapshot, etc), never downloaded and rehosted. That gets you a
single unified page per issue without copying anyone's files.

## Layout

```
backend/schema.sql                  Postgres schema (auto-applied on first boot by migrate.py)
backend/app/migrate.py               One-shot schema bootstrap, runs on app startup
scripts/fetch_archive_metadata.py   Issue metadata + embed link from archive.org
scripts/fetch_wayback_links.py      Matches Wayback snapshots of xlr8r.com/magazine/<n> to issues
scripts/fetch_hyperreal_links.py    Crawls the hyperreal.org zine-era pages, matches/creates issues
scripts/extract_articles.py         Splits each issue's OCR text into per-article rows
backend/app/                        FastAPI app (models, schemas, endpoints)
backend/Procfile                    Production start command
backend/railway.toml                Railway deploy config (start command, healthcheck, restart policy)
frontend/shared.css                 Design tokens + components shared by both pages
frontend/issue.html                 Single-issue page (masthead, embedded reader, content links, articles)
frontend/index.html                 Site home: live stats, search, paginated issue grid
frontend/package.json               Pulls in `serve` so Railway can host the static files
frontend/railway.toml               Railway deploy config for the frontend service
```

## Setup

```bash
createdb xlr8r_archive

cd backend
pip install -r requirements.txt
export DATABASE_URL=postgresql://localhost/xlr8r_archive
uvicorn app.main:app --reload
```

The app applies `schema.sql` itself on first startup against an empty
database (see `app/migrate.py`) — no manual `psql` step needed, locally
or on Railway. It only ever does this once: it checks whether the
`issues` table already exists and skips entirely if so. Schema changes
after that point need a real migration step of their own, not an edit
to `schema.sql`, since it won't be re-run against an existing database.

## Populate content

Run in this order -- the later scrapers look up issues by number, so
`fetch_archive_metadata.py` should go first to seed most of the `issues`
table (`fetch_hyperreal_links.py` will create rows for issues archive.org
doesn't have, but only after the crawl finds them):

```bash
pip install -r scripts/requirements.txt

python scripts/fetch_archive_metadata.py --dry-run       # sanity check first
python scripts/fetch_archive_metadata.py --dsn $DATABASE_URL

python scripts/fetch_wayback_links.py --dry-run
python scripts/fetch_wayback_links.py --dsn $DATABASE_URL

python scripts/fetch_hyperreal_links.py --dry-run
python scripts/fetch_hyperreal_links.py --dsn $DATABASE_URL

# test the segmentation heuristic against one real issue, no DB needed:
python scripts/extract_articles.py --identifier xlr8r-issue-10

python scripts/extract_articles.py --dry-run --dsn $DATABASE_URL
python scripts/extract_articles.py --dsn $DATABASE_URL
```

Notes on each:
- **archive.org**: the search query is a first pass — check dry-run output
  against what's actually in the collection (issues 79-138, missing
  87/128, plus a handful of individually uploaded early issues) and
  tighten it if it's pulling in noise.
- **Wayback**: only matches pages that fit the `/magazine/<number>` URL
  pattern the live site used — solid because the match is unambiguous,
  but it only captures the issue's own table-of-contents page, not every
  individual article page from elsewhere on the old site.
- **hyperreal**: this corner of the site is small (1994-era static HTML,
  no clean URL scheme), so it matches by parsing each page's own "Issue N
  of the magazine, published ..." text. Expect a handful of matches, not
  a huge haul — that's the actual size of what's there, not a bug.
- **extract_articles**: pulls each issue's OCR text layer from
  archive.org (`_djvu.txt`) and splits it on XLR8R's recurring section
  names (Audiofile, Machines, Vis-Ed, Bitter Bastard, Reviews). This is a
  heuristic, not a real layout parser — OCR noise and multi-column pages
  will produce some garbage chunks and occasional wrong bylines. Use
  `--identifier <archive.org id>` to sanity-check the split on one issue
  before running it broadly, and `--replace` to re-run cleanly once
  you've tuned it.

  **`body_text` is for search only.** It exists so `/articles/search` can
  do full-text matching server-side; `ArticleOut` deliberately doesn't
  include it and the frontend never renders it. Storing OCR'd article
  text to power search is a different thing from publishing full article
  text to visitors — keep that boundary if you extend the API or
  frontend later (excerpts around a search match are fine; full body
  text isn't).

## Frontend

Two static pages, no build step, sharing `shared.css` for a consistent
look. Both default `window.XLR8R_API_BASE` to the deployed backend
(`https://backend-production-8c32.up.railway.app`) right in the HTML —
override it by setting `window.XLR8R_API_BASE` in a `<script>` tag
before that default line runs, e.g. for pointing a local checkout at a
different environment.

- **`index.html`** — the site home. Masthead shows live counts from
  `GET /stats` (issues / articles / artists indexed), a search box hitting
  `GET /articles/search`, and a paginated grid of every issue from
  `GET /issues`, using the `X-Total-Count` response header to know when
  to stop paginating.
- **`issue.html?id=<issue id>`** — calls `GET /issues/{id}/full` and
  renders the masthead, an embedded reader (iframed straight from
  archive.org — nothing downloaded), any other content found for the
  issue, and its article list.

The backend has CORS wide open (`allow_origins=["*"]`) since these pages
are static files served separately from the API.

## API

- `GET /issues` — paginated issue list
- `GET /issues/{id}` — single issue's metadata
- `GET /issues/{id}/full` — the assembled issue page: metadata + articles (with artist tags) + all content links
- `GET /issues/{id}/articles` — articles in an issue
- `GET /issues/{id}/content` — every content link for an issue (embeds, viewers, related pages)
- `POST /issues/{id}/content` — attach another piece of content to an issue (e.g. a Wayback snapshot of a related feature, once you find one)
- `GET /articles/search?q=...` — full-text search
- `GET /artists/{name}/articles` — articles mentioning an artist
- `GET /stats` — total counts of issues/articles/artists indexed

`fetch_archive_metadata.py` now populates `content_links` with an
archive.org `/embed/{identifier}` URL per issue (their BookReader viewer)
instead of a direct file download link -- that's what your issue page
should `<iframe>` to let people read the scan without you storing it.

## Deploying to Railway

The backend is set up to run on Railway with no extra config beyond
pointing the service at `backend/` as its root directory:

- Root directory: `backend`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
  (already in `railway.toml`/`Procfile`, Railway will pick it up)
- Healthcheck: `/health` (checks DB connectivity too, not just that the
  process is up)
- Env var: `DATABASE_URL` — point it at a Postgres service in the same
  project (Railway's reference syntax `${{Postgres.DATABASE_URL}}` works)
- Schema is applied automatically on first boot — nothing manual to run
  against the provisioned Postgres instance

The frontend runs as a second Railway service in the same project,
pointed at the same repo:

- Root directory: `frontend`
- Start command: `npm start` → `serve . -l tcp://0.0.0.0:$PORT`
  (`package.json` pulls in the `serve` package at build time so nothing
  hits the network at boot)
- Healthcheck: `/`
- No env vars needed — the API base URL is baked into the HTML files
  directly (see Frontend section above)

If you ever re-provision Postgres from a raw `postgres:16` image (rather
than Railway's managed Postgres template) with a volume mounted at
`/var/lib/postgresql/data`: set `PGDATA=/var/lib/postgresql/data/pgdata`
on that service. Without it, `initdb` refuses to run because the mount
point isn't empty (it has a `lost+found` directory), and the container
crash-loops on every boot.

## Not built yet

- OCR quality review / segmentation tuning — `extract_articles.py` is a
  first pass; expect to spot-check its output against a few issues and
  adjust the header list or byline pattern
- Individual article-page matching from Wayback (currently only the
  per-issue `/magazine/<n>` page is captured, not every article on the
  old site)
- Deployment (DB hosting, API hosting, static hosting for the frontend)
- A visible takedown/contact path for rights holders
