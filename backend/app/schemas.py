from datetime import date
from typing import Optional

from pydantic import BaseModel, ConfigDict


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
