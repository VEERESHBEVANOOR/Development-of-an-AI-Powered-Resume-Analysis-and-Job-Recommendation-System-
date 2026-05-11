import sqlite3
from werkzeug.security import generate_password_hash, check_password_hash
from config import SQLITE_DB


def _ensure_column(cur, table, column, definition):
    cur.execute(f"PRAGMA table_info({table})")
    cols = {row[1] for row in cur.fetchall()}
    if column not in cols:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db():
    db = sqlite3.connect(SQLITE_DB)
    cur = db.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS uploads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            filename TEXT NOT NULL,
            summary TEXT NOT NULL,
            resume_text TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    # Week 3-6 additions
    _ensure_column(cur, "uploads", "insights_json", "TEXT")
    _ensure_column(cur, "uploads", "jobs_json", "TEXT")
    db.commit()
    db.close()


def create_user(name, email, password):
    db = sqlite3.connect(SQLITE_DB)
    cur = db.cursor()
    cur.execute(
        "INSERT INTO users (name, email, password) VALUES (?, ?, ?)",
        (name, email, generate_password_hash(password))
    )
    db.commit()
    db.close()


def validate_user(email, password):
    db = sqlite3.connect(SQLITE_DB)
    cur = db.cursor()
    cur.execute("SELECT password FROM users WHERE email=?", (email,))
    user = cur.fetchone()
    db.close()
    return user and check_password_hash(user[0], password)
