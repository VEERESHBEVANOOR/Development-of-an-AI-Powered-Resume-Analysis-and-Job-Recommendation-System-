import sqlite3
import json
from datetime import datetime

from config import SQLITE_DB


def _format_local_datetime(value):
    if not value:
        return ""
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{dt.strftime('%d/%m/%Y')} {hour}:{dt.strftime('%M')} {dt.strftime('%p').lower()}"


def save_upload(email, filename, summary, resume_text, insights=None, jobs=None):
    db = sqlite3.connect(SQLITE_DB)
    cur = db.cursor()
    cur.execute(
        """
        INSERT INTO uploads (email, filename, summary, resume_text, insights_json, jobs_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            email,
            filename,
            summary,
            resume_text,
            json.dumps(insights or {}, ensure_ascii=True),
            json.dumps(jobs or [], ensure_ascii=True),
        ),
    )
    db.commit()
    upload_id = cur.lastrowid
    db.close()
    return upload_id


def get_uploads_for_user(email):
    db = sqlite3.connect(SQLITE_DB)
    cur = db.cursor()
    cur.execute(
        """
        SELECT id, filename, datetime(created_at, 'localtime')
        FROM uploads
        WHERE email = ?
        ORDER BY created_at DESC
        """,
        (email,),
    )
    rows = cur.fetchall()
    db.close()
    return [(r[0], r[1], _format_local_datetime(r[2])) for r in rows]


def get_upload_by_id(upload_id, email):
    db = sqlite3.connect(SQLITE_DB)
    cur = db.cursor()
    cur.execute(
        """
        SELECT
            id,
            filename,
            summary,
            resume_text,
            datetime(created_at, 'localtime'),
            insights_json,
            jobs_json
        FROM uploads
        WHERE id = ? AND email = ?
        """,
        (upload_id, email),
    )
    row = cur.fetchone()
    db.close()
    if not row:
        return None
    row = list(row)
    row[4] = _format_local_datetime(row[4])
    return tuple(row)


def delete_upload(upload_id, email):
    db = sqlite3.connect(SQLITE_DB)
    cur = db.cursor()
    cur.execute(
        """
        DELETE FROM uploads
        WHERE id = ? AND email = ?
        """,
        (upload_id, email),
    )
    db.commit()
    deleted = cur.rowcount
    db.close()
    return deleted > 0
