"""Tests for the attachment summary feature.

Covers:
  - _make_summary() pure-function logic
  - database schema migration (summary column added to pre-existing DBs)
  - summary persisted and retrieved via update_attachment / get_attachment
  - AttachmentResponse and SearchResult models expose the summary field
"""
from __future__ import annotations

import sys
import os
import unittest
import tempfile

# Run from project root so relative imports work
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from backend.main import _make_summary
from backend.models import AttachmentResponse, SearchResult


# ---------------------------------------------------------------------------
# _make_summary unit tests
# ---------------------------------------------------------------------------

class TestMakeSummary(unittest.TestCase):

    def test_short_text_returned_as_is(self):
        text = "Hello world."
        self.assertEqual(_make_summary(text), text)

    def test_text_exactly_max_chars_returned_as_is(self):
        text = "x" * 400
        self.assertEqual(_make_summary(text), text)

    def test_strips_leading_trailing_whitespace(self):
        text = "  Hello world.  "
        self.assertEqual(_make_summary(text), "Hello world.")

    def test_empty_string(self):
        self.assertEqual(_make_summary(""), "")
        self.assertEqual(_make_summary("   "), "")

    def test_cuts_at_period_space(self):
        # Sentence boundary ". " falls in the second half of the 400-char window
        prefix = "A" * 201  # past midpoint
        sentence_end = ". "
        suffix = "B" * 200
        text = prefix + sentence_end + suffix  # > 400 chars total
        result = _make_summary(text)
        # Should end at the period, not include the trailing space or suffix
        self.assertTrue(result.endswith("."), repr(result))
        self.assertNotIn("B", result)

    def test_cuts_at_exclamation_space(self):
        prefix = "A" * 201
        suffix = "B" * 200
        text = prefix + "! " + suffix
        result = _make_summary(text)
        self.assertTrue(result.endswith("!"), repr(result))
        self.assertNotIn("B", result)

    def test_cuts_at_question_space(self):
        prefix = "A" * 201
        suffix = "B" * 200
        text = prefix + "? " + suffix
        result = _make_summary(text)
        self.assertTrue(result.endswith("?"), repr(result))
        self.assertNotIn("B", result)

    def test_cuts_at_period_newline(self):
        prefix = "A" * 201
        suffix = "B" * 200
        text = prefix + ".\n" + suffix
        result = _make_summary(text)
        self.assertTrue(result.endswith("."), repr(result))
        self.assertNotIn("B", result)

    def test_sentence_boundary_in_first_half_not_used(self):
        # ". " appears only before the midpoint — should NOT cut there; falls back to word boundary
        prefix = "A" * 100 + ". " + "C" * 100  # sentence boundary at pos ~100 (< 200)
        suffix = " " + "D" * 300               # space for word boundary, then long suffix
        text = prefix + suffix                  # > 400 chars, no ". " in second half
        result = _make_summary(text)
        # Word boundary fallback: ends with "…" or is truncated at a space
        self.assertFalse(result.endswith("."), repr(result))

    def test_falls_back_to_word_boundary(self):
        # No sentence separators anywhere — only word boundaries
        words = ("hello " * 100)[:450]  # 450 chars of "hello " repeated, has spaces
        result = _make_summary(words)
        self.assertTrue(result.endswith("\u2026"), repr(result))  # ends with ellipsis
        self.assertLessEqual(len(result), 401)  # at most max_chars + 1 for ellipsis

    def test_no_word_boundary_falls_back_to_hard_cut(self):
        # Solid block with no spaces or sentence separators
        text = "x" * 500
        result = _make_summary(text)
        self.assertTrue(result.endswith("\u2026"), repr(result))
        self.assertEqual(result, "x" * 400 + "\u2026")

    def test_custom_max_chars(self):
        text = "Hello world. This is a longer sentence that exceeds the limit."
        result = _make_summary(text, max_chars=20)
        self.assertLessEqual(len(result), 21)  # at most max_chars + "…"

    def test_result_never_exceeds_max_chars_plus_ellipsis(self):
        import random, string
        rng = random.Random(42)
        for _ in range(50):
            length = rng.randint(300, 800)
            text = "".join(rng.choices(string.printable, k=length))
            result = _make_summary(text, max_chars=400)
            # The result should be at most 401 characters (400 + possible "…")
            self.assertLessEqual(len(result), 401, f"Too long for input length {length}")

    def test_prefers_later_sentence_boundary(self):
        # Two sentence boundaries in the second half — should prefer the later one (rfind)
        prefix = "A" * 201
        mid = "First sentence. Second sentence. "
        suffix = "B" * 200
        text = prefix + mid + suffix
        result = _make_summary(text)
        # rfind finds the *last* ". " in the truncated window, so result ends at "Second sentence."
        self.assertIn("Second sentence", result)


# ---------------------------------------------------------------------------
# Database migration and persistence tests (async via IsolatedAsyncioTestCase)
# ---------------------------------------------------------------------------

class TestSummaryDatabaseMigration(unittest.IsolatedAsyncioTestCase):
    """Test that init_db adds the summary column to a pre-existing database."""

    async def test_migration_adds_summary_column(self):
        import aiosqlite
        from backend.database import init_db

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            # Create a database that looks like the pre-summary schema
            async with aiosqlite.connect(db_path) as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS notes (
                        id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        content TEXT NOT NULL,
                        tags TEXT NOT NULL DEFAULT '[]',
                        folder TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        indexed_at TEXT
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS attachments (
                        id TEXT PRIMARY KEY,
                        note_id TEXT NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
                        filename TEXT NOT NULL,
                        source_url TEXT,
                        stored_path TEXT,
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL DEFAULT 0,
                        page_count INTEGER,
                        extracted_text TEXT,
                        extracted_at TEXT,
                        indexed_at TEXT,
                        extraction_error TEXT,
                        created_at TEXT NOT NULL
                    )
                """)
                await conn.commit()

                # Verify summary column is absent
                async with conn.execute("PRAGMA table_info(attachments)") as cur:
                    cols_before = {row[1] for row in await cur.fetchall()}
                self.assertNotIn("summary", cols_before)

                # Run migration
                conn.row_factory = aiosqlite.Row
                await init_db(conn)

                # Verify summary column is now present
                async with conn.execute("PRAGMA table_info(attachments)") as cur:
                    cols_after = {row[1] for row in await cur.fetchall()}
                self.assertIn("summary", cols_after)
        finally:
            os.unlink(db_path)


class TestSummaryPersistence(unittest.IsolatedAsyncioTestCase):
    """Test that summary is stored and retrieved correctly."""

    async def asyncSetUp(self):
        import aiosqlite
        from backend.database import init_db

        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self._tmp.name
        self._tmp.close()

        self.conn = await aiosqlite.connect(self.db_path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON")
        await init_db(self.conn)

    async def asyncTearDown(self):
        await self.conn.close()
        os.unlink(self.db_path)

    async def test_summary_defaults_to_none_on_create(self):
        from backend.database import create_note, create_attachment, get_attachment

        note = await create_note(self.conn, "Test Note", "content", [], "")
        att = await create_attachment(
            self.conn,
            note_id=note.id,
            filename="test.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
        )
        self.assertIsNone(att.summary)

    async def test_summary_stored_and_retrieved(self):
        from backend.database import create_note, create_attachment, update_attachment, get_attachment

        note = await create_note(self.conn, "Test Note", "content", [], "")
        att = await create_attachment(
            self.conn,
            note_id=note.id,
            filename="test.pdf",
            mime_type="application/pdf",
            size_bytes=1024,
        )

        expected_summary = "This is the first sentence of the document."
        await update_attachment(self.conn, att.id, summary=expected_summary)

        fetched = await get_attachment(self.conn, att.id)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.summary, expected_summary)

    async def test_summary_updated_independently(self):
        from backend.database import create_note, create_attachment, update_attachment, get_attachment

        note = await create_note(self.conn, "Test Note", "content", [], "")
        att = await create_attachment(
            self.conn,
            note_id=note.id,
            filename="doc.pdf",
            mime_type="application/pdf",
            size_bytes=512,
        )

        await update_attachment(self.conn, att.id, summary="First summary.")
        await update_attachment(self.conn, att.id, summary="Updated summary.")

        fetched = await get_attachment(self.conn, att.id)
        self.assertEqual(fetched.summary, "Updated summary.")

    async def test_summary_null_can_be_set(self):
        from backend.database import create_note, create_attachment, update_attachment, get_attachment

        note = await create_note(self.conn, "Test Note", "content", [], "")
        att = await create_attachment(
            self.conn,
            note_id=note.id,
            filename="doc.pdf",
            mime_type="application/pdf",
            size_bytes=512,
        )

        await update_attachment(self.conn, att.id, summary="Some summary.")
        await update_attachment(self.conn, att.id, summary=None)

        fetched = await get_attachment(self.conn, att.id)
        self.assertIsNone(fetched.summary)


# ---------------------------------------------------------------------------
# Model field tests
# ---------------------------------------------------------------------------

class TestModelFields(unittest.TestCase):
    """Verify that the new summary fields exist on the Pydantic models."""

    def test_attachment_response_has_summary_field(self):
        self.assertIn("summary", AttachmentResponse.model_fields)

    def test_attachment_response_summary_is_nullable(self):
        # summary: str | None has no default, so Pydantic requires it to be
        # passed explicitly — but the value itself may be None.
        field = AttachmentResponse.model_fields["summary"]
        import annotated_types
        from typing import get_args, Union
        # The annotation should allow None (i.e. be Optional / Union with None)
        annotation = field.annotation
        self.assertIn(type(None), get_args(annotation))

    def test_search_result_has_attachment_summary_field(self):
        self.assertIn("attachment_summary", SearchResult.model_fields)

    def test_search_result_attachment_summary_is_optional(self):
        field = SearchResult.model_fields["attachment_summary"]
        self.assertFalse(field.is_required())

    def test_attachment_response_instantiation_with_summary(self):
        att = AttachmentResponse(
            id="abc",
            note_id="note1",
            filename="file.pdf",
            source_url=None,
            stored_path=None,
            mime_type="application/pdf",
            size_bytes=100,
            page_count=None,
            summary="Short summary.",
            extracted_at=None,
            indexed_at=None,
            extraction_error=None,
            created_at="2026-01-01T00:00:00+00:00",
        )
        self.assertEqual(att.summary, "Short summary.")

    def test_attachment_response_instantiation_without_summary(self):
        att = AttachmentResponse(
            id="abc",
            note_id="note1",
            filename="file.pdf",
            source_url=None,
            stored_path=None,
            mime_type="application/pdf",
            size_bytes=100,
            page_count=None,
            summary=None,
            extracted_at=None,
            indexed_at=None,
            extraction_error=None,
            created_at="2026-01-01T00:00:00+00:00",
        )
        self.assertIsNone(att.summary)

    def test_search_result_instantiation_with_attachment_summary(self):
        result = SearchResult(
            note_id="note1",
            title="My Note",
            folder="",
            tags=[],
            score=0.95,
            chunk_text="some chunk",
            source_type="attachment",
            source_label="doc.pdf",
            attachment_id="att1",
            attachment_summary="Brief summary of the attachment.",
        )
        self.assertEqual(result.attachment_summary, "Brief summary of the attachment.")

    def test_search_result_attachment_summary_defaults_to_none(self):
        result = SearchResult(
            note_id="note1",
            title="My Note",
            folder="",
            tags=[],
            score=0.9,
            chunk_text="some chunk",
            source_type="note",
            source_label="My Note",
        )
        self.assertIsNone(result.attachment_summary)


if __name__ == "__main__":
    unittest.main()
