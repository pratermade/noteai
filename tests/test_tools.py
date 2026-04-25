"""Tests for chat_api tool handlers.

Each test class patches settings.database_url to a temp SQLite file and
mocks the embedding/vector-store calls so no external services are needed.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import aiosqlite

USER_ID = "user-001"
USER_ID_2 = "user-002"
_NOW = "2026-01-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _init_db(db_path: str) -> None:
    from backend.database import init_db
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await init_db(conn)
        for uid, uname in [(USER_ID, "alice"), (USER_ID_2, "bob")]:
            await conn.execute(
                "INSERT INTO users (id, username, password_hash, share_key, created_at)"
                " VALUES (?,?,?,?,?)",
                (uid, uname, "hash", f"key-{uid}", _NOW),
            )
        await conn.commit()


async def _fetch_note(db_path: str, note_id: str) -> dict | None:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def _fetch_items(db_path: str, note_id: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM list_items WHERE note_id = ? ORDER BY position", (note_id,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]


class ToolTestCase(unittest.IsolatedAsyncioTestCase):
    """Base: temp DB + settings patch + embedding mocks."""

    async def asyncSetUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = tmp.name
        tmp.close()
        await _init_db(self.db_path)

        self._db_patcher = patch("backend.chat_api.settings.database_url", self.db_path)
        self._db_patcher.start()

        self._embed_patcher = patch(
            "backend.chat_api.embeddings.embed_texts",
            new_callable=AsyncMock,
            return_value=[[0.1] * 768],
        )
        self._embed_patcher.start()

        self._upsert_patcher = patch(
            "backend.chat_api.vector_store.upsert",
            new_callable=AsyncMock,
        )
        self._upsert_patcher.start()

    async def asyncTearDown(self):
        self._db_patcher.stop()
        self._embed_patcher.stop()
        self._upsert_patcher.stop()
        os.unlink(self.db_path)


# ---------------------------------------------------------------------------
# create_note
# ---------------------------------------------------------------------------

class TestCreateNote(ToolTestCase):

    async def test_returns_needs_input_when_content_is_none(self):
        from backend.chat_api import _tool_create_note
        result = json.loads(await _tool_create_note(None, USER_ID))
        self.assertEqual(result["needs_input"], "content")
        self.assertIn("prompt", result)

    async def test_creates_note_in_db(self):
        from backend.chat_api import _tool_create_note
        result = json.loads(await _tool_create_note("Hello world", USER_ID))
        self.assertIn("note_id", result)
        row = await _fetch_note(self.db_path, result["note_id"])
        self.assertIsNotNone(row)
        self.assertEqual(row["content"], "Hello world")
        self.assertEqual(row["user_id"], USER_ID)
        self.assertEqual(row["folder"], "Unfiled")
        self.assertEqual(row["note_type"], "markdown")

    async def test_auto_title_from_first_line(self):
        from backend.chat_api import _tool_create_note
        result = json.loads(await _tool_create_note("## My Title\nBody text here.", USER_ID))
        self.assertEqual(result["title"], "My Title")
        row = await _fetch_note(self.db_path, result["note_id"])
        self.assertEqual(row["title"], "My Title")

    async def test_auto_title_truncates_to_60_chars(self):
        from backend.chat_api import _tool_create_note
        long_line = "A" * 80
        result = json.loads(await _tool_create_note(long_line, USER_ID))
        self.assertEqual(result["title"], "A" * 60)

    async def test_auto_title_falls_back_to_untitled(self):
        from backend.chat_api import _tool_create_note
        result = json.loads(await _tool_create_note("   \n\n   ", USER_ID))
        self.assertEqual(result["title"], "Untitled")

    async def test_indexes_content(self):
        from backend.chat_api import _tool_create_note
        import backend.chat_api as chat_api
        await _tool_create_note("Index me please", USER_ID)
        chat_api.embeddings.embed_texts.assert_called_once()
        chat_api.vector_store.upsert.assert_called_once()

    async def test_indexed_at_set_after_indexing(self):
        from backend.chat_api import _tool_create_note
        result = json.loads(await _tool_create_note("Some content", USER_ID))
        row = await _fetch_note(self.db_path, result["note_id"])
        self.assertIsNotNone(row["indexed_at"])


# ---------------------------------------------------------------------------
# create_reminder
# ---------------------------------------------------------------------------

class TestCreateReminder(ToolTestCase):

    async def test_creates_note_with_reminder_at(self):
        from backend.chat_api import _tool_create_reminder
        due = "2026-05-01T09:00:00"
        result = json.loads(
            await _tool_create_reminder("Call dentist", due, "Don't forget!", USER_ID)
        )
        self.assertIn("note_id", result)
        self.assertEqual(result["reminder_at"], due)
        row = await _fetch_note(self.db_path, result["note_id"])
        self.assertEqual(row["title"], "Call dentist")
        self.assertEqual(row["reminder_at"], due)
        self.assertEqual(row["content"], "Don't forget!")
        self.assertEqual(row["folder"], "Unfiled")

    async def test_indexes_content_when_provided(self):
        from backend.chat_api import _tool_create_reminder
        import backend.chat_api as chat_api
        await _tool_create_reminder("Meeting", "2026-06-01T10:00:00", "Agenda: review Q2", USER_ID)
        chat_api.embeddings.embed_texts.assert_called_once()

    async def test_skips_indexing_for_empty_content(self):
        from backend.chat_api import _tool_create_reminder
        import backend.chat_api as chat_api
        await _tool_create_reminder("Reminder", "2026-06-01T10:00:00", "", USER_ID)
        chat_api.embeddings.embed_texts.assert_not_called()


# ---------------------------------------------------------------------------
# create_journal_entry
# ---------------------------------------------------------------------------

class TestCreateJournalEntry(ToolTestCase):

    async def test_returns_needs_input_when_content_is_none(self):
        from backend.chat_api import _tool_create_journal_entry
        result = json.loads(await _tool_create_journal_entry(None, USER_ID))
        self.assertEqual(result["needs_input"], "content")
        self.assertIn("prompt", result)

    async def test_creates_note_in_journal_folder(self):
        from backend.chat_api import _tool_create_journal_entry
        result = json.loads(await _tool_create_journal_entry("Today was good.", USER_ID))
        self.assertIn("note_id", result)
        row = await _fetch_note(self.db_path, result["note_id"])
        self.assertEqual(row["folder"], "Journal")
        self.assertEqual(row["note_type"], "markdown")
        self.assertEqual(row["content"], "Today was good.")

    async def test_auto_title_from_content(self):
        from backend.chat_api import _tool_create_journal_entry
        result = json.loads(await _tool_create_journal_entry("Productive day\nDid lots.", USER_ID))
        self.assertEqual(result["title"], "Productive day")

    async def test_indexes_content(self):
        from backend.chat_api import _tool_create_journal_entry
        import backend.chat_api as chat_api
        await _tool_create_journal_entry("Journal body text here.", USER_ID)
        chat_api.embeddings.embed_texts.assert_called_once()


# ---------------------------------------------------------------------------
# create_list
# ---------------------------------------------------------------------------

class TestCreateList(ToolTestCase):

    async def test_creates_list_note(self):
        from backend.chat_api import _tool_create_list
        result = json.loads(await _tool_create_list("Groceries", USER_ID))
        self.assertIn("note_id", result)
        row = await _fetch_note(self.db_path, result["note_id"])
        self.assertEqual(row["title"], "Groceries")
        self.assertEqual(row["note_type"], "list")
        self.assertEqual(row["folder"], "Lists")
        self.assertIsNotNone(row["indexed_at"])  # lists are pre-marked as indexed

    async def test_creates_list_with_initial_item(self):
        from backend.chat_api import _tool_create_list
        result = json.loads(await _tool_create_list("Shopping", USER_ID, "Milk"))
        self.assertIn("item_id", result)
        items = await _fetch_items(self.db_path, result["note_id"])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "Milk")
        self.assertEqual(items[0]["completed"], 0)

    async def test_creates_list_without_item_when_item_id_none(self):
        from backend.chat_api import _tool_create_list
        result = json.loads(await _tool_create_list("Empty list", USER_ID, None))
        self.assertNotIn("item_id", result)
        items = await _fetch_items(self.db_path, result["note_id"])
        self.assertEqual(len(items), 0)


# ---------------------------------------------------------------------------
# add_list_item / get_list_items / complete_list_item / delete_list_item
# ---------------------------------------------------------------------------

class TestListItemTools(ToolTestCase):

    async def asyncSetUp(self):
        await super().asyncSetUp()
        from backend.chat_api import _tool_create_list
        result = json.loads(await _tool_create_list("My List", USER_ID))
        self.note_id = result["note_id"]

    async def test_add_item(self):
        from backend.chat_api import _tool_add_list_item
        result = json.loads(await _tool_add_list_item(self.note_id, "Buy eggs", USER_ID))
        self.assertIn("item_id", result)
        items = await _fetch_items(self.db_path, self.note_id)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["content"], "Buy eggs")

    async def test_add_item_access_denied_for_other_user(self):
        from backend.chat_api import _tool_add_list_item
        result = json.loads(await _tool_add_list_item(self.note_id, "Steal item", USER_ID_2))
        self.assertIn("error", result)

    async def test_get_items(self):
        from backend.chat_api import _tool_add_list_item, _tool_get_list_items
        await _tool_add_list_item(self.note_id, "Item A", USER_ID)
        await _tool_add_list_item(self.note_id, "Item B", USER_ID)
        result = json.loads(await _tool_get_list_items(self.note_id, USER_ID))
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["items"][0]["content"], "Item A")

    async def test_get_items_access_denied_for_other_user(self):
        from backend.chat_api import _tool_get_list_items
        result = json.loads(await _tool_get_list_items(self.note_id, USER_ID_2))
        self.assertIn("error", result)

    async def test_complete_item(self):
        from backend.chat_api import _tool_add_list_item, _tool_complete_list_item
        add_result = json.loads(await _tool_add_list_item(self.note_id, "Do laundry", USER_ID))
        item_id = add_result["item_id"]
        result = json.loads(await _tool_complete_list_item(self.note_id, item_id, USER_ID))
        self.assertTrue(result["success"])
        items = await _fetch_items(self.db_path, self.note_id)
        self.assertEqual(items[0]["completed"], 1)

    async def test_complete_item_not_found(self):
        from backend.chat_api import _tool_complete_list_item
        result = json.loads(
            await _tool_complete_list_item(self.note_id, "nonexistent-id", USER_ID)
        )
        self.assertIn("error", result)

    async def test_delete_item(self):
        from backend.chat_api import _tool_add_list_item, _tool_delete_list_item
        add_result = json.loads(await _tool_add_list_item(self.note_id, "Delete me", USER_ID))
        item_id = add_result["item_id"]
        result = json.loads(await _tool_delete_list_item(self.note_id, item_id, USER_ID))
        self.assertTrue(result["success"])
        items = await _fetch_items(self.db_path, self.note_id)
        self.assertEqual(len(items), 0)

    async def test_delete_item_not_found(self):
        from backend.chat_api import _tool_delete_list_item
        result = json.loads(
            await _tool_delete_list_item(self.note_id, "nonexistent-id", USER_ID)
        )
        self.assertIn("error", result)

    async def test_delete_item_access_denied_for_other_user(self):
        from backend.chat_api import _tool_add_list_item, _tool_delete_list_item
        add_result = json.loads(await _tool_add_list_item(self.note_id, "Protected", USER_ID))
        item_id = add_result["item_id"]
        result = json.loads(
            await _tool_delete_list_item(self.note_id, item_id, USER_ID_2)
        )
        self.assertIn("error", result)


# ---------------------------------------------------------------------------
# _execute_tool dispatcher
# ---------------------------------------------------------------------------

class TestExecuteTool(ToolTestCase):

    async def test_unknown_tool_returns_error(self):
        from backend.chat_api import _execute_tool
        result = json.loads(await _execute_tool("nonexistent_tool", {}, USER_ID))
        self.assertIn("error", result)
        self.assertIn("nonexistent_tool", result["error"])

    async def test_no_user_context_returns_error_for_list_tools(self):
        from backend.chat_api import _execute_tool
        for tool in ("get_list_items", "add_list_item", "complete_list_item",
                     "delete_list_item", "create_list", "create_note",
                     "create_reminder", "create_journal_entry"):
            with self.subTest(tool=tool):
                result = json.loads(await _execute_tool(tool, {}, None))
                self.assertIn("error", result, f"{tool} should require user context")

    async def test_dispatches_create_note(self):
        from backend.chat_api import _execute_tool
        result = json.loads(
            await _execute_tool("create_note", {"content": "Via dispatcher"}, USER_ID)
        )
        self.assertIn("note_id", result)

    async def test_dispatches_create_reminder(self):
        from backend.chat_api import _execute_tool
        result = json.loads(await _execute_tool(
            "create_reminder",
            {"title": "Wake up", "due_date": "2026-06-01T07:00:00", "content": ""},
            USER_ID,
        ))
        self.assertIn("note_id", result)
        self.assertEqual(result["reminder_at"], "2026-06-01T07:00:00")

    async def test_dispatches_create_journal_entry(self):
        from backend.chat_api import _execute_tool
        result = json.loads(
            await _execute_tool("create_journal_entry", {"content": "Good day"}, USER_ID)
        )
        self.assertIn("note_id", result)

    async def test_dispatches_create_list_with_item(self):
        from backend.chat_api import _execute_tool
        result = json.loads(await _execute_tool(
            "create_list", {"title": "Tasks", "item_id": "First task"}, USER_ID
        ))
        self.assertIn("note_id", result)
        self.assertIn("item_id", result)


if __name__ == "__main__":
    unittest.main()
