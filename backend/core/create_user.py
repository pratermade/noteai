"""
CLI to create users and run the one-time multi-user migration.

Usage:
    python -m backend.create_user --username alice
    python -m backend.create_user --username admin --migrate
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
import uuid

import aiosqlite

from .auth import hash_password
from .config import settings


async def _create_user(db: aiosqlite.Connection, username: str, password: str) -> str:
    share_key = str(uuid.uuid4())
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    user_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO users (id, username, password_hash, share_key, created_at) VALUES (?,?,?,?,?)",
        (user_id, username, hash_password(password), share_key, now),
    )
    await db.commit()
    return user_id


async def _run(username: str, migrate: bool) -> None:
    async with aiosqlite.connect(settings.database_url) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")

        # Ensure tables exist
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                share_key TEXT UNIQUE NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id TEXT NOT NULL REFERENCES users(id),
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)
        # Ensure notes has user_id column
        async with db.execute("PRAGMA table_info(notes)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "user_id" not in cols:
            await db.execute("ALTER TABLE notes ADD COLUMN user_id TEXT REFERENCES users(id)")
            await db.commit()
        await db.commit()

        # Check if username already exists
        async with db.execute("SELECT id FROM users WHERE username = ?", (username,)) as cur:
            existing = await cur.fetchone()
        if existing:
            print(f"Error: user '{username}' already exists.", file=sys.stderr)
            sys.exit(1)

        password = getpass.getpass(f"Password for {username}: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            sys.exit(1)
        if len(password) < 6:
            print("Password must be at least 6 characters.", file=sys.stderr)
            sys.exit(1)

        user_id = await _create_user(db, username, password)
        print(f"Created user '{username}' (id={user_id})")

        if migrate:
            # Assign all existing notes without a user_id to this user
            result = await db.execute(
                "UPDATE notes SET user_id = ? WHERE user_id IS NULL", (user_id,)
            )
            await db.commit()
            print(f"Migrated {result.rowcount} note(s) to user '{username}'")

            # Migrate per-user settings from app_settings to user_settings
            per_user_keys = ["server_timezone", "reminder_times", "reminder_hours",
                             "journal_reminder_times", "character_prompt"]
            migrated_settings = 0
            for key in per_user_keys:
                async with db.execute(
                    "SELECT value FROM app_settings WHERE key = ?", (key,)
                ) as cur:
                    row = await cur.fetchone()
                if row:
                    await db.execute(
                        "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)"
                        " ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
                        (user_id, key, row[0]),
                    )
                    migrated_settings += 1
            await db.commit()
            print(f"Migrated {migrated_settings} setting(s) to user '{username}'")

            # Store bot_user_id in app_settings so telegram bot knows which user to query
            await db.execute(
                "INSERT INTO app_settings (key, value) VALUES ('bot_user_id', ?)"
                " ON CONFLICT(key) DO NOTHING",
                (user_id,),
            )
            await db.commit()
            print(f"Set bot_user_id = {user_id}")
            print("Migration complete. Run POST /api/reindex as this user to backfill Chroma metadata.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a NoterAI user")
    parser.add_argument("--username", required=True, help="Username to create")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Assign all existing notes/settings to this user (run once on first setup)",
    )
    args = parser.parse_args()
    asyncio.run(_run(args.username, args.migrate))


if __name__ == "__main__":
    main()
