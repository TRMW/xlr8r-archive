from datetime import date
from typing import Optional
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, computed_field

# Titles extract_articles.py falls back to when it couldn't find a real
# headline (either a byline-less coarse chunk, or a byline it couldn't
# title). A search-inside link built from one of these would just jump to
# the first place that word appears, not to this specific piece -- for
# these, the plain issue link is the honest option.
GENERIC_ARTICLE_TITLES = {"feature", "audiofile", "machines", "vis-ed", "bitter-bastard", "review"}


class IssueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    identifier: str
    issue_number: Optional[int]
    title: Optional[str]
    publish_date: Optional[date]
    source: str
    source_url: str
    page_count: Optional[int]


class ArtistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str


class ArticleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    issue_id: Optional[int]
    title: str
    author: Optional[str]
    article_type: Optional[str]
    source_url: Optional[str]
    artists: list[ArtistOut] = []

    @computed_field
    @property
    def reader_url(self) -> Optional[str]:
        """Deep-link into the issue's archive.org reader at this specific
        article, using BookReader's documented '#search/<phrase>' fragment
        (see internetarchive/bookreader#1254) to jump straight to the page
        where the title phrase appears -- rather than just opening the
        issue at its first page. Falls back to the plain issue link for
        generic/unsplit chunks, where the title isn't a precise enough
        phrase to jump to one specific spot."""
        if not self.source_url:
            return None
        title = (self.title or "").strip()
        is_generic = (
            title.lower() in GENERIC_ARTICLE_TITLES
            or title.startswith("Untitled (")
        )
        if not title or is_generic:
            return self.source_url
        return f"{self.source_url}#search/{quote(title)}"


class ContentLinkIn(BaseModel):
    source: str
    link_type: str
    url: str
    title: Optional[str] = None


class ContentLinkOut(ContentLinkIn):
    model_config = ConfigDict(from_attributes=True)

    id: int


class IssueDetailOut(IssueOut):
    """Everything we have for one issue: metadata, its articles (with
    artist tags), and every embeddable/linkable piece of content found
    for it across sources."""

    articles: list[ArticleOut] = []
    content_links: list[ContentLinkOut] = []


class StatsOut(BaseModel):
    issues: int
    articles: int
    artists: int
