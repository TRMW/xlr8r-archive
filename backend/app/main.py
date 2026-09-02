from contextlib import asynccontextmanager
from typing import Optional

from fastapi import Depends, FastAPI, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, text
from sqlalchemy.orm import Session

from . import models, schemas
from .db import engine, get_db
from .migrate import run_migrations


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_migrations(engine)
    yield


app = FastAPI(title="XLR8R Archive API", lifespan=lifespan)

# The frontend is a set of static files, not served from this app, so
# browser requests to the API come from a different origin. Wide open
# for now since everything served is public read data anyway -- tighten
# to your actual frontend domain once you deploy it.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
    expose_headers=["X-Total-Count"],
)


@app.get("/issues", response_model=list[schemas.IssueOut])
def list_issues(
    response: Response,
    db: Session = Depends(get_db),
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    total = db.query(func.count(models.Issue.id)).scalar()
    response.headers["X-Total-Count"] = str(total)
    return (
        db.query(models.Issue)
        .order_by(models.Issue.issue_number)
        .offset(offset)
        .limit(limit)
        .all()
    )


@app.get("/issues/{issue_id}", response_model=schemas.IssueOut)
def get_issue(issue_id: int, db: Session = Depends(get_db)):
    return db.query(models.Issue).filter(models.Issue.id == issue_id).first()


@app.get("/issues/{issue_id}/full", response_model=schemas.IssueDetailOut)
def get_issue_full(issue_id: int, db: Session = Depends(get_db)):
    """Single issue page payload: metadata + articles (with artist tags)
    + every content link (primary scan embed, plus anything else found
    for this issue from other sources) in one response."""
    return db.query(models.Issue).filter(models.Issue.id == issue_id).first()


@app.get("/issues/{issue_id}/articles", response_model=list[schemas.ArticleOut])
def get_issue_articles(issue_id: int, db: Session = Depends(get_db)):
    return db.query(models.Article).filter(models.Article.issue_id == issue_id).all()


@app.get("/issues/{issue_id}/content", response_model=list[schemas.ContentLinkOut])
def get_issue_content(issue_id: int, db: Session = Depends(get_db)):
    return db.query(models.ContentLink).filter(models.ContentLink.issue_id == issue_id).all()


@app.post("/issues/{issue_id}/content", response_model=schemas.ContentLinkOut)
def add_issue_content(issue_id: int, link: schemas.ContentLinkIn, db: Session = Depends(get_db)):
    row = models.ContentLink(
        issue_id=issue_id,
        source=link.source,
        link_type=link.link_type,
        url=link.url,
        title=link.title,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@app.get("/articles/search", response_model=list[schemas.ArticleOut])
def search_articles(
    q: str,
    article_type: Optional[str] = None,
    db: Session = Depends(get_db),
    limit: int = Query(25, le=100),
):
    query = db.query(models.Article).filter(
        models.Article.search_vector.op("@@")(func.plainto_tsquery("english", q))
    )
    if article_type:
        query = query.filter(models.Article.article_type == article_type)
    return query.limit(limit).all()


@app.get("/artists/{name}/articles", response_model=list[schemas.ArticleOut])
def articles_by_artist(name: str, db: Session = Depends(get_db)):
    return (
        db.query(models.Article)
        .join(models.Article.artists)
        .filter(models.Artist.name.ilike(name))
        .all()
    )


@app.get("/health")
def health(db: Session = Depends(get_db)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/stats", response_model=schemas.StatsOut)
def get_stats(db: Session = Depends(get_db)):
    return {
        "issues": db.query(func.count(models.Issue.id)).scalar(),
        "articles": db.query(func.count(models.Article.id)).scalar(),
        "artists": db.query(func.count(models.Artist.id)).scalar(),
    }
