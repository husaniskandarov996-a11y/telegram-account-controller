import sqlite3
from typing import List, Dict, Any, Optional
from datetime import datetime
from config import DB_PATH

def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    """Initializes the database tables and ensures multi-tenant columns exist."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL DEFAULT 0,
                tg_id INTEGER UNIQUE NOT NULL,
                first_name TEXT,
                username TEXT,
                phone TEXT,
                session_string TEXT NOT NULL,
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Schema migration: check if owner_id exists
        cursor.execute("PRAGMA table_info(accounts)")
        columns = [row["name"] for row in cursor.fetchall()]
        if "owner_id" not in columns:
            cursor.execute("ALTER TABLE accounts ADD COLUMN owner_id INTEGER NOT NULL DEFAULT 0")
            cursor.execute("UPDATE accounts SET owner_id = 6274816464 WHERE owner_id = 0")
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.commit()

def save_setting(key: str, value: str):
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO settings (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, str(value)))
        conn.commit()

def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row["value"] if row else default

def add_account(owner_id: int, tg_id: int, first_name: Optional[str], username: Optional[str], phone: Optional[str], session_string: str) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO accounts (owner_id, tg_id, first_name, username, phone, session_string, is_active)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(tg_id) DO UPDATE SET
                owner_id=excluded.owner_id,
                first_name=excluded.first_name,
                username=excluded.username,
                phone=excluded.phone,
                session_string=excluded.session_string,
                is_active=1
        """, (owner_id, tg_id, first_name, username, phone, session_string))
        conn.commit()
        return cursor.lastrowid

def get_all_accounts(owner_id: Optional[int] = None, only_active: bool = False) -> List[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM accounts WHERE 1=1"
        params = []
        if owner_id is not None:
            query += " AND owner_id = ?"
            params.append(owner_id)
        if only_active:
            query += " AND is_active = 1"
        query += " ORDER BY id ASC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

def get_account(account_id: int, owner_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        if owner_id is not None:
            cursor.execute("SELECT * FROM accounts WHERE id = ? AND owner_id = ?", (account_id, owner_id))
        else:
            cursor.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def get_account_by_tg_id(tg_id: int, owner_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        if owner_id is not None:
            cursor.execute("SELECT * FROM accounts WHERE tg_id = ? AND owner_id = ?", (tg_id, owner_id))
        else:
            cursor.execute("SELECT * FROM accounts WHERE tg_id = ?", (tg_id,))
        row = cursor.fetchone()
        return dict(row) if row else None

def delete_account(account_id: int, owner_id: Optional[int] = None) -> bool:
    with get_connection() as conn:
        cursor = conn.cursor()
        if owner_id is not None:
            cursor.execute("DELETE FROM accounts WHERE id = ? AND owner_id = ?", (account_id, owner_id))
        else:
            cursor.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
        conn.commit()
        return cursor.rowcount > 0

def set_account_active(account_id: int, is_active: bool, owner_id: Optional[int] = None):
    with get_connection() as conn:
        cursor = conn.cursor()
        if owner_id is not None:
            cursor.execute("UPDATE accounts SET is_active = ? WHERE id = ? AND owner_id = ?", (1 if is_active else 0, account_id, owner_id))
        else:
            cursor.execute("UPDATE accounts SET is_active = ? WHERE id = ?", (1 if is_active else 0, account_id))
        conn.commit()

def count_accounts(owner_id: Optional[int] = None) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        if owner_id is not None:
            cursor.execute("SELECT COUNT(*) as cnt FROM accounts WHERE owner_id = ? AND is_active = 1", (owner_id,))
        else:
            cursor.execute("SELECT COUNT(*) as cnt FROM accounts WHERE is_active = 1")
        row = cursor.fetchone()
        return row["cnt"] if row else 0

def get_global_stats() -> Dict[str, int]:
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(DISTINCT owner_id) as total_users FROM accounts")
        total_users = cursor.fetchone()["total_users"]
        cursor.execute("SELECT COUNT(*) as total_accounts FROM accounts")
        total_accounts = cursor.fetchone()["total_accounts"]
        cursor.execute("SELECT COUNT(*) as active_accounts FROM accounts WHERE is_active = 1")
        active_accounts = cursor.fetchone()["active_accounts"]
        return {
            "total_users": total_users,
            "total_accounts": total_accounts,
            "active_accounts": active_accounts
        }
