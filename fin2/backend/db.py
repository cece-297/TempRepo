import os
import sqlite3

DB = os.path.join(os.path.dirname(__file__), "app.db")

def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def init():
    with conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS plans(plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, kind TEXT, amount INTEGER, months INTEGER, start_date TEXT, summary TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        c.execute("""CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ts TEXT, event_json TEXT)""")
