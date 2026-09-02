from sqlalchemy import Column, Integer, String, Date, DateTime, Text, ForeignKey, Table
from sqlalchemy.orm import relationship, declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

article_artists = Table(
    "article_artists",
    Base.metadata,
    Column("article_id", ForeignKey("articles.id"), primary_key=True),
    Column("artist_id", ForeignKey("artists.id"), primary_key=True),
)


class Issue(Base):
    __tablename__ = "issues"

    id = Column(Integer, primary_key=True)
    identifier = Column(String, unique=True, nullable=False)
    issue_number = Column(Integer)
    title = Column(String)
    publish_date = Column(Date)
    source = Column(String, nullable=False)
    source_url = Column(String, nullable=False)
    page_count = Column(Integer)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    articles = relationship("Article", back_populates="issue", cascade="all, delete-orphan")
    content_links = relationship("ContentLink", back_populates="issue", cascade="all, delete-orphan")


class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, ForeignKey("issues.id", ondelete="CASCADE"))
    title = Column(String, nullable=False)
    author = Column(String)
    article_type = Column(String)
    page_start = Column(Integer)
    page_end = Column(Integer)
    body_text = Column(Text)
    source_url = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    issue = relationship("Issue", back_populates="articles")
    artists = relationship("Artist", secondary=article_artists, back_populates="articles")
    review = relationship("Review", back_populates="article", uselist=False)


class Artist(Base):
    __tablename__ = "artists"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)

    articles = relationship("Article", secondary=article_artists, back_populates="artists")


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True)
    article_id = Column(Integer, ForeignKey("articles.id", ondelete="CASCADE"))
    release_artist = Column(String)
    release_title = Column(String)
    label = Column(String)
    rating = Column(String)
    format = Column(String)

    article = relationship("Article", back_populates="review")


class ContentLink(Base):
    __tablename__ = "content_links"

    id = Column(Integer, primary_key=True)
    issue_id = Column(Integer, ForeignKey("issues.id", ondelete="CASCADE"))
    source = Column(String, nullable=False)
    link_type = Column(String, nullable=False)
    url = Column(String, nullable=False)
    title = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    issue = relationship("Issue", back_populates="content_links")
