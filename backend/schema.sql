-- XLR8R archive schema

CREATE TABLE issues (
    id             SERIAL PRIMARY KEY,
    identifier     TEXT UNIQUE NOT NULL,        -- e.g. archive.org identifier "xlr8r-issue-10"
    issue_number   INTEGER,
    title          TEXT,
    publish_date   DATE,
    source         TEXT NOT NULL,               -- 'archive_org' | 'wayback' | 'hyperreal'
    source_url     TEXT NOT NULL,                -- canonical page to link out to (not rehosted)
    page_count     INTEGER,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every piece of content we've found for an issue, from any source.
-- We never store the underlying file here -- just enough to embed or
-- link to it live on the source (archive.org's BookReader, a Wayback
-- snapshot, a hyperreal.org scan page, etc). One issue can have several
-- rows: the primary scan plus related articles, photos, or mixes found
-- elsewhere.
CREATE TABLE content_links (
    id             SERIAL PRIMARY KEY,
    issue_id       INTEGER REFERENCES issues(id) ON DELETE CASCADE,
    source         TEXT NOT NULL,               -- 'archive_org' | 'wayback' | 'hyperreal' | 'other'
    link_type      TEXT NOT NULL,               -- 'pdf_embed' | 'viewer' | 'article_page' | 'image' | 'audio'
    url            TEXT NOT NULL,               -- embeddable/viewable URL, not a file to download
    title          TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (issue_id, url)
);

CREATE INDEX idx_content_links_issue ON content_links(issue_id);

CREATE TABLE articles (
    id             SERIAL PRIMARY KEY,
    issue_id       INTEGER REFERENCES issues(id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    author         TEXT,
    article_type   TEXT,                         -- 'feature' | 'review' | 'interview' | 'audiofile' | 'machines' | 'vis-ed' | 'bitter-bastard'
    page_start     INTEGER,
    page_end       INTEGER,
    body_text      TEXT,                         -- OCR / extracted text, used for search only
    source_url     TEXT,                          -- deep link to the article on its source
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    search_vector  TSVECTOR
);

CREATE TABLE artists (
    id             SERIAL PRIMARY KEY,
    name           TEXT UNIQUE NOT NULL
);

CREATE TABLE article_artists (
    article_id     INTEGER REFERENCES articles(id) ON DELETE CASCADE,
    artist_id      INTEGER REFERENCES artists(id) ON DELETE CASCADE,
    PRIMARY KEY (article_id, artist_id)
);

CREATE TABLE reviews (
    id             SERIAL PRIMARY KEY,
    article_id     INTEGER REFERENCES articles(id) ON DELETE CASCADE,
    release_artist TEXT,
    release_title  TEXT,
    label          TEXT,
    rating         TEXT,
    format         TEXT                          -- 'vinyl' | 'cd' | 'mp3' | 'digital'
);

-- Full text search
CREATE INDEX idx_articles_search ON articles USING GIN (search_vector);

CREATE FUNCTION articles_search_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('english', coalesce(NEW.title, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(NEW.author, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(NEW.body_text, '')), 'C');
    RETURN NEW;
END
$$ LANGUAGE plpgsql;

CREATE TRIGGER articles_search_update
    BEFORE INSERT OR UPDATE ON articles
    FOR EACH ROW EXECUTE FUNCTION articles_search_trigger();

CREATE INDEX idx_issues_number ON issues(issue_number);
CREATE INDEX idx_articles_issue ON articles(issue_id);
CREATE INDEX idx_articles_type ON articles(article_type);
