from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, field_validator


FOLDERS = [
    "Unfiled",
    "Reference",
    "Ideas",
    "Todo",
    "Review Later",
    "Projects",
    "Journal",
    "Resources",
    "Archive",
]


class NoteCreate(BaseModel):
    title: str
    content: str
    tags: list[str] = []
    folder: str = "Unfiled"
    note_type: Literal['markdown', 'attachment', 'url', 'video'] = 'markdown'
    reminder_at: str | None = None

    @field_validator("folder")
    @classmethod
    def folder_must_be_valid(cls, v: str) -> str:
        if v not in FOLDERS:
            raise ValueError(f"folder must be one of: {', '.join(FOLDERS)}")
        return v


class NoteUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    tags: list[str] | None = None
    folder: str | None = None
    note_type: Literal['markdown', 'attachment', 'url', 'video'] | None = None
    reminder_at: str | None = None
    reminder_done: bool | None = None

    @field_validator("folder")
    @classmethod
    def folder_must_be_valid(cls, v: str | None) -> str | None:
        if v is not None and v not in FOLDERS:
            raise ValueError(f"folder must be one of: {', '.join(FOLDERS)}")
        return v


class NoteResponse(BaseModel):
    id: str
    title: str
    content: str
    tags: list[str]
    folder: str
    created_at: str
    updated_at: str
    indexed_at: str | None
    note_type: Literal['markdown', 'attachment', 'url', 'video']
    note_summary: str | None
    reminder_at: str | None
    reminder_done: bool


class AttachmentResponse(BaseModel):
    id: str
    note_id: str
    filename: str
    source_url: str | None
    stored_path: str | None
    mime_type: str
    size_bytes: int
    page_count: int | None
    summary: str | None
    extracted_at: str | None
    indexed_at: str | None
    extraction_error: str | None
    created_at: str


class ShareRequest(BaseModel):
    title: str = ""
    text: str = ""
    url: str = ""


class SearchRequest(BaseModel):
    query: str
    n_results: int = 5
    tags: list[str] | None = None
    folder: str | None = None


class SearchResult(BaseModel):
    note_id: str
    title: str
    folder: str
    tags: list[str]
    score: float
    chunk_text: str
    source_type: Literal["note", "attachment"]
    source_label: str
    source_url: str | None = None
    attachment_id: str | None = None
    attachment_summary: str | None = None


class ReindexJob(BaseModel):
    job_id: str
    status: Literal["running", "completed", "completed_with_errors", "failed"]
    total: int
    completed: int
    failed: int
    attachments_completed: int = 0
    attachments_failed: int = 0
    started_at: str
    finished_at: str | None = None
    errors: list[dict] = []
