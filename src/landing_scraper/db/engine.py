"""Postgres connection helpers (psycopg-based; no ORM mapping)."""
from __future__ import annotations

from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from ..config import settings


def conn_str() -> str:
    pw = f":{settings.postgres_password}" if settings.postgres_password else ""
    return (
        f"host={settings.postgres_host} port={settings.postgres_port} "
        f"user={settings.postgres_user}{pw} dbname={settings.postgres_db}"
    )


@contextmanager
def get_conn(*, autocommit: bool = False):
    conn = psycopg.connect(conn_str(), autocommit=autocommit, row_factory=dict_row)
    try:
        yield conn
        if not autocommit:
            conn.commit()
    except Exception:
        if not autocommit:
            conn.rollback()
        raise
    finally:
        conn.close()
