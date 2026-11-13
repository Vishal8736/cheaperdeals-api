from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

SCHEMA = "cheapedeal"


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC datetime (stored naive so SQLite and Postgres behave the same)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalise_url(url: str) -> str:
    """Accept what hosts hand out: postgres:// (Heroku style), postgresql://, and Neon's pooled host (-pooler). The pooler refuses the
    `options` start-up parameter we use for the schema, so use the direct host of the same database."""
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if url.startswith("postgresql") and ".neon.tech" in url:
        url = re.sub(r"(@ep-[a-z0-9-]+?)-pooler(\.)", r"\1\2", url)
    return url


def make_engine(url: str) -> Engine:
    url = normalise_url(url)
    if url.startswith("postgresql"):
        bootstrap = create_engine(url, pool_pre_ping=True)
        with bootstrap.begin() as c:
            c.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
        bootstrap.dispose()
        return create_engine(
            url,
            pool_pre_ping=True,
            connect_args={"options": f"-c search_path={SCHEMA}"},
        )
    if url.startswith("sqlite"):
        from sqlalchemy import event

        eng = create_engine(url, connect_args={"check_same_thread": False, "timeout": 20})

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # WAL: readers (the live dashboard) never block the writer
            cur = dbapi_conn.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
            except Exception:
                pass
            cur.close()

        return eng
    return create_engine(url)


class Database:
    def __init__(self, url: str):
        self.engine = make_engine(url)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

    def create_all(self) -> None:
        from . import models  # noqa: F401  (register tables)

        Base.metadata.create_all(self.engine)
        self.add_missing_columns()

    def add_missing_columns(self) -> None:
        """Tiny forward-only migration: add columns that newer versions introduced to tables an older version created.
        Only adds nullable / defaulted columns, never drops or changes anything."""
        from sqlalchemy import inspect

        insp = inspect(self.engine)
        dialect = self.engine.dialect
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {col.type.compile(dialect)}'
                default = getattr(col.default, "arg", None) if col.default is not None else None
                if isinstance(default, bool):
                    ddl += " DEFAULT " + ("TRUE" if default else "FALSE") if dialect.name == "postgresql" else f" DEFAULT {int(default)}"
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                elif isinstance(default, str):
                    ddl += " DEFAULT '" + default.replace("'", "''") + "'"
                with self.engine.begin() as c:
                    c.execute(text(ddl))

    def drop_all(self) -> None:  # tests only
        Base.metadata.drop_all(self.engine)

    @contextmanager
    def session(self):
        s: Session = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
