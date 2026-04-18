/* Notes RAG — main app */

const API = '';
let state = {
  notes: [],
  currentNoteId: null,
  tags: [],
  folders: [],
  filterTag: null,
  filterFolder: null,
  searchMode: false,
  indexPollTimer: null,
  attPollTimer: null,
  reindexPollTimer: null,
  saveDirty: false,
  editMode: false,
  attachmentSummaries: [],
  currentNoteType: 'markdown',
  currentNoteSummary: null,
  reminderDone: false,
  tasksMode: false,
};

marked.use({ gfm: true, breaks: true });

// ── Selectors ─────────────────────────────────────────────────────────────

const $ = id => document.getElementById(id);
const noteListEl    = $('note-list');
const editorPanel   = $('editor-panel');
const noteListPanel = $('note-list-panel');
const titleInput    = $('note-title-input');
const contentArea   = $('note-content');
const folderInput    = $('folder-input');
const reminderInput  = $('reminder-input');
const btnReminderDone = $('btn-reminder-done');
const tagInput      = $('tag-input');
const tagChips      = $('tag-chips');
const saveBadge     = $('save-badge');
const folderList    = $('folder-list');
const tagListEl     = $('tag-list');
const searchInput   = $('search-input');
const dropZone      = $('drop-zone');
const fileInput     = $('file-input');
const attList       = $('attachment-list');
const reindexAllBtn = $('reindex-all-btn');
const reindexProgress = $('reindex-progress');
const sidebarEl     = $('sidebar');
const sidebarBackdrop = $('sidebar-backdrop');
const notePreview        = $('note-preview');
const noteSummarySection = $('note-summary-section');
const btnEditToggle      = $('btn-edit-toggle');

// ── Sidebar drawer (mobile) ────────────────────────────────────────────────

function openSidebar() {
  sidebarEl.classList.add('open');
  sidebarBackdrop.classList.add('open');
}

function closeSidebar() {
  sidebarEl.classList.remove('open');
  sidebarBackdrop.classList.remove('open');
}

document.querySelectorAll('.btn-hamburger').forEach(btn => {
  btn.addEventListener('click', openSidebar);
});

sidebarBackdrop.addEventListener('click', closeSidebar);

// ── Toast ──────────────────────────────────────────────────────────────────

function toast(msg, type = '') {
  const el = document.createElement('div');
  el.className = `toast${type ? ' ' + type : ''}`;
  el.textContent = msg;
  $('toast-container').appendChild(el);
  setTimeout(() => el.remove(), 4000);
}

// ── API helpers ────────────────────────────────────────────────────────────

async function apiFetch(path, opts = {}) {
  const res = await fetch(API + path, opts);
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail ?? detail; } catch {}
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function getYouTubeVideoId(url) {
  const m = (url || '').match(
    /(?:youtube\.com\/watch\?.*v=|youtu\.be\/|youtube\.com\/shorts\/)([A-Za-z0-9_-]{11})/
  );
  return m ? m[1] : null;
}

// ── Notes list ─────────────────────────────────────────────────────────────

async function loadNotes() {
  const params = new URLSearchParams();
  if (state.filterTag)    params.set('tag',    state.filterTag);
  if (state.filterFolder !== null) params.set('folder', state.filterFolder);
  try {
    state.notes = await apiFetch(`/api/notes?${params}`);
    renderNoteList(state.notes);
  } catch (e) {
    console.error('loadNotes', e);
    toast('Failed to load notes: ' + e.message, 'error');
  }
}

function renderNoteList(notes) {
  if (!notes.length) {
    noteListEl.innerHTML = '<div class="empty-state">No notes found.</div>';
    return;
  }
  noteListEl.innerHTML = '';
  for (const n of notes) {
    const card = document.createElement('div');
    card.className = 'note-card' + (n.id === state.currentNoteId ? ' active' : '');
    card.dataset.id = n.id;
    const tagHtml = n.tags.map(t => `<span class="tag-chip">${esc(t)}</span>`).join('');
    const noteType = n.note_type || 'markdown';
    card.innerHTML = `
      <div class="note-card-title">${esc(n.title || 'Untitled')}</div>
      <div class="note-card-meta">
        <span class="note-type-badge type-${noteType}">${noteType}</span>
        ${n.folder ? `<span>📁 ${esc(n.folder)}</span>` : ''}
        ${n.reminder_at && !n.reminder_done ? `<span title="Reminder: ${esc(n.reminder_at)}">🔔</span>` : ''}
        ${tagHtml}
        <span>${relTime(n.updated_at)}</span>
        ${n.indexed_at ? '' : '<span style="color:var(--warn)">⏳ unindexed</span>'}
      </div>`;
    card.addEventListener('click', () => openNote(n.id));
    noteListEl.appendChild(card);
  }
}

function renderSearchResults(results) {
  if (!results.length) {
    noteListEl.innerHTML = '<div class="empty-state">No results.</div>';
    return;
  }
  noteListEl.innerHTML = '';
  for (const r of results) {
    const card = document.createElement('div');
    card.className = 'note-card';
    card.dataset.id = r.note_id;
    const tagHtml = r.tags.map(t => `<span class="tag-chip">${esc(t)}</span>`).join('');
    const isAtt = r.source_type === 'attachment';
    card.innerHTML = `
      <div class="note-card-title">${isAtt ? '📎 ' : ''}${esc(r.title)}</div>
      <div class="note-card-meta">
        ${r.folder ? `<span>📁 ${esc(r.folder)}</span>` : ''}
        ${tagHtml}
        <span class="note-card-score">score ${r.score.toFixed(3)}</span>
      </div>
      ${isAtt ? `<div class="note-card-meta" style="font-size:11px">📄 ${esc(r.source_label)}${r.source_url ? ` · <a href="${esc(r.source_url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(r.source_url)}</a>` : ''}</div>` : ''}
      ${r.attachment_summary ? `<div class="note-card-summary">${esc(r.attachment_summary)}</div>` : ''}
      <div class="note-card-snippet">${esc(r.chunk_text)}</div>`;
    card.addEventListener('click', () => openNote(r.note_id));
    noteListEl.appendChild(card);
  }
}

// ── Editor ─────────────────────────────────────────────────────────────────

function renderMarkdown(content) {
  const raw = marked.parse(content || '');
  return DOMPurify.sanitize(raw, { ADD_ATTR: ['target', 'rel'] });
}

function renderPreview() {
  notePreview.innerHTML = renderMarkdown(contentArea.value);
  notePreview.querySelectorAll('a').forEach(a => {
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
  });
}

function renderSummarySection() {
  const items = [];
  if (state.currentNoteSummary && state.currentNoteType === 'markdown') {
    items.push(`<div class="note-summary-item summary-collapsed">
       <div class="note-summary-label">Summary <span class="summary-toggle">▸</span></div>
       <div class="note-summary-text">${renderMarkdown(state.currentNoteSummary)}</div>
     </div>`);
  }
  for (const s of state.attachmentSummaries) {
    items.push(`<div class="note-summary-item summary-collapsed">
       <div class="note-summary-label">${esc(s.filename)} <span class="summary-toggle">▸</span></div>
       <div class="note-summary-text">${renderMarkdown(s.summary)}</div>
     </div>`);
  }
  if (!items.length) {
    noteSummarySection.style.display = 'none';
    return;
  }
  noteSummarySection.innerHTML = items.join('');
  noteSummarySection.querySelectorAll('.note-summary-item').forEach(item => {
    item.addEventListener('click', () => item.classList.toggle('summary-collapsed'));
  });
  noteSummarySection.style.display = 'flex';
}

function setEditMode(isEdit) {
  state.editMode = isEdit;
  if (isEdit) {
    notePreview.style.display = 'none';
    contentArea.style.display = '';
    btnEditToggle.textContent = 'Preview';
    contentArea.focus();
  } else {
    renderPreview();
    notePreview.style.display = 'block';
    contentArea.style.display = 'none';
    btnEditToggle.textContent = 'Edit';
  }
}

async function openNote(id) {
  clearTimers();
  state.currentNoteId = id;
  state.saveDirty = false;
  try {
    const note = await apiFetch(`/api/notes/${id}`);
    titleInput.value    = note.title;
    contentArea.value   = note.content;
    folderInput.value   = note.folder;
    reminderInput.value = note.reminder_at || '';
    state.reminderDone  = note.reminder_done || false;
    updateReminderDoneBtn();
    renderTagChips(note.tags);
    state.attachmentSummaries = [];
    state.currentNoteType = note.note_type || 'markdown';
    state.currentNoteSummary = note.note_summary || null;
    const videoId = state.currentNoteType === 'video' ? getYouTubeVideoId(note.content) : null;
    const videoEmbed = $('video-embed');
    const videoIframe = $('video-iframe');
    if (videoId) {
      videoIframe.src = `https://www.youtube.com/embed/${videoId}`;
      videoEmbed.style.display = '';
    } else {
      videoIframe.src = '';
      videoEmbed.style.display = 'none';
    }
    dropZone.style.display = '';
    renderSummarySection();
    setEditMode(false);
    setBadge('');
    editorPanel.style.display = 'flex';
    editorPanel.style.flexDirection = 'column';
    noteListPanel.style.display = 'none';
    closeSidebar();
    highlightActiveCard(id);
    await loadAttachments(id);
    if (!note.indexed_at) startIndexPoll(id);
  } catch (e) {
    console.error('openNote', e);
    toast('Could not open note: ' + e.message, 'error');
  }
}

function closeEditor() {
  clearTimers();
  state.currentNoteId = null;
  state.editMode = false;
  notePreview.innerHTML = '';
  $('video-iframe').src = '';
  $('video-embed').style.display = 'none';
  editorPanel.style.display = 'none';
  if (state.tasksMode) {
    openTasksPanel();
  } else {
    noteListPanel.style.display = 'flex';
    noteListPanel.style.flexDirection = 'column';
  }
  highlightActiveCard(null);
}

function highlightActiveCard(id) {
  document.querySelectorAll('.note-card').forEach(c => {
    c.classList.toggle('active', c.dataset.id === id);
  });
}

// ── Tag chips ──────────────────────────────────────────────────────────────

function getCurrentTags() {
  return [...tagChips.querySelectorAll('.tag-chip-removable')].map(c => c.dataset.tag);
}

function renderTagChips(tags) {
  tagChips.innerHTML = '';
  for (const t of tags) addTagChip(t);
}

function addTagChip(tag) {
  const chip = document.createElement('span');
  chip.className = 'tag-chip-removable';
  chip.dataset.tag = tag;
  chip.innerHTML = `${esc(tag)} <button title="Remove tag">×</button>`;
  chip.querySelector('button').addEventListener('click', () => {
    chip.remove();
    state.saveDirty = true;
  });
  tagChips.appendChild(chip);
}

tagInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' || e.key === ',') {
    e.preventDefault();
    const val = tagInput.value.trim().replace(/,/g, '');
    if (val && !getCurrentTags().includes(val)) addTagChip(val);
    tagInput.value = '';
    state.saveDirty = true;
  }
});

// ── Save ───────────────────────────────────────────────────────────────────

async function saveNote() {
  const title       = titleInput.value.trim() || 'Untitled';
  const content     = contentArea.value;
  const folder      = folderInput.value.trim();
  const tags        = getCurrentTags();
  const reminder_at = reminderInput.value || null;
  const reminder_done = state.reminderDone;

  setBadge('saving');
  try {
    if (state.currentNoteId) {
      await apiFetch(`/api/notes/${state.currentNoteId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content, folder, tags, reminder_at, reminder_done }),
      });
    } else {
      const note = await apiFetch('/api/notes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, content, folder, tags, reminder_at }),
      });
      state.currentNoteId = note.id;
    }
    state.saveDirty = false;
    setBadge('saved');
    setEditMode(false);
    startIndexPoll(state.currentNoteId);
    await loadNotes();
    await loadSidebar();
    return true;
  } catch (e) {
    console.error('saveNote', e);
    setBadge('error');
    toast('Save failed: ' + e.message, 'error');
    return false;
  }
}

function setBadge(type) {
  const labels = { saving: 'saving…', saved: 'saved', indexed: 'indexed', error: 'error' };
  const classes = { saving: 'badge-saving', saved: 'badge-saved', indexed: 'badge-indexed', error: 'badge-error' };
  if (!type) { saveBadge.style.display = 'none'; return; }
  saveBadge.style.display = '';
  saveBadge.className = `badge ${classes[type] || ''}`;
  saveBadge.textContent = labels[type] || type;
}

// ── Index polling ──────────────────────────────────────────────────────────

function startIndexPoll(noteId) {
  clearInterval(state.indexPollTimer);
  setBadge('saved');
  let attempts = 0;
  let failures = 0;
  state.indexPollTimer = setInterval(async () => {
    attempts++;
    try {
      const note = await apiFetch(`/api/notes/${noteId}`);
      failures = 0;
      if (note.indexed_at) {
        setBadge('indexed');
        clearInterval(state.indexPollTimer);
        renderNoteList(state.notes.map(n => n.id === noteId ? note : n));
      }
    } catch (err) {
      console.warn('index poll failed', err);
      failures++;
      if (failures >= 3) {
        clearInterval(state.indexPollTimer);
        toast('Could not reach server — please reload.', 'error');
      }
    }
    if (attempts > 60) clearInterval(state.indexPollTimer);
  }, 2000);
}

// ── Delete note ────────────────────────────────────────────────────────────

async function deleteCurrentNote() {
  if (!state.currentNoteId) return;
  try {
    await apiFetch(`/api/notes/${state.currentNoteId}`, { method: 'DELETE' });
    closeEditor();
    await loadNotes();
    await loadSidebar();
    toast('Note deleted.', 'success');
  } catch (e) {
    console.error('deleteCurrentNote', e);
    toast('Delete failed: ' + e.message, 'error');
  }
}

const deleteBtn = $('btn-delete');
deleteBtn.addEventListener('click', () => {
  if (deleteBtn.dataset.confirm === '1') {
    deleteBtn.dataset.confirm = '';
    deleteBtn.textContent = 'Delete';
    deleteCurrentNote();
  } else {
    deleteBtn.dataset.confirm = '1';
    deleteBtn.textContent = 'Sure?';
    setTimeout(() => {
      if (deleteBtn.dataset.confirm === '1') {
        deleteBtn.dataset.confirm = '';
        deleteBtn.textContent = 'Delete';
      }
    }, 5000);
  }
});

// ── Archive note ───────────────────────────────────────────────────────────

async function archiveCurrentNote() {
  if (!state.currentNoteId) return;
  folderInput.value = 'Archive';
  state.saveDirty = true;
  const ok = await saveNote();
  if (ok) {
    closeEditor();
    toast('Moved to Archive.', 'success');
  }
}

$('btn-archive').addEventListener('click', archiveCurrentNote);

// ── Sidebar ────────────────────────────────────────────────────────────────

async function loadSidebar() {
  try {
    const [tags, folders] = await Promise.all([
      apiFetch('/api/tags'),
      apiFetch('/api/folders'),
    ]);
    state.tags    = tags;
    state.folders = folders;
    renderSidebar();
  } catch (e) {
    console.error('loadSidebar', e);
  }
}

async function openTasksPanel() {
  clearTimers();
  state.currentNoteId = null;
  state.tasksMode = true;
  editorPanel.style.display = 'none';
  noteListPanel.style.display = 'none';
  $('tasks-panel').style.display = 'flex';
  $('tasks-panel').style.flexDirection = 'column';
  $('btn-tasks').classList.add('active');
  closeSidebar();
  try {
    const tasks = await apiFetch('/api/tasks');
    renderTasksPanel(tasks);
  } catch (e) {
    console.error('openTasksPanel', e);
  }
}

function renderTasksPanel(tasks) {
  const listEl = $('tasks-list');
  listEl.innerHTML = '';
  if (!tasks.length) {
    listEl.innerHTML = '<div class="empty-state">No upcoming tasks.</div>';
    return;
  }
  const today = new Date().toISOString().slice(0, 10);
  for (const t of tasks) {
    const row = document.createElement('div');
    row.className = 'task-item';
    const overdue = t.reminder_at < today;
    const dateColor = overdue ? 'var(--danger,#dc2626)' : 'var(--muted)';
    row.innerHTML = `
      <input type="checkbox" class="task-checkbox" title="Mark done" />
      <span class="task-item-title">${esc(t.title)}</span>
      <span class="task-item-date" style="color:${dateColor}">${t.reminder_at}</span>`;
    row.querySelector('.task-item-title').addEventListener('click', () => openNote(t.id));
    row.querySelector('.task-checkbox').addEventListener('change', async (e) => {
      if (!e.target.checked) return;
      try {
        await apiFetch(`/api/notes/${t.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reminder_done: true }),
        });
        row.remove();
        if (!$('tasks-list').querySelector('.task-item')) {
          $('tasks-list').innerHTML = '<div class="empty-state">No upcoming tasks.</div>';
        }
      } catch (err) {
        e.target.checked = false;
        toast('Could not update task', 'error');
      }
    });
    listEl.appendChild(row);
  }
}

function renderSidebar() {
  // Folders
  folderList.innerHTML = '';
  const allItem = document.createElement('div');
  allItem.className = 'nav-item' + (state.filterFolder === null && !state.filterTag ? ' active' : '');
  allItem.textContent = 'All notes';
  allItem.addEventListener('click', () => { setFilter(null, null); closeSidebar(); });
  folderList.appendChild(allItem);

  for (const f of state.folders) {
    const item = document.createElement('div');
    item.className = 'nav-item' + (state.filterFolder === f ? ' active' : '');
    item.textContent = '📁 ' + f;
    item.addEventListener('click', () => { setFilter(null, f); closeSidebar(); });
    folderList.appendChild(item);
  }

  // Tags
  tagListEl.innerHTML = '';
  for (const t of state.tags) {
    const item = document.createElement('div');
    item.className = 'nav-item' + (state.filterTag === t ? ' active' : '');
    item.innerHTML = `<span class="tag-chip">${esc(t)}</span>`;
    item.addEventListener('click', () => { setFilter(t, null); closeSidebar(); });
    tagListEl.appendChild(item);
  }
}

function setFilter(tag, folder) {
  state.filterTag = tag;
  state.filterFolder = folder;
  state.tasksMode = false;
  $('tasks-panel').style.display = 'none';
  $('btn-tasks').classList.remove('active');
  searchInput.value = '';
  state.searchMode = false;
  noteListPanel.style.display = 'flex';
  noteListPanel.style.flexDirection = 'column';
  editorPanel.style.display = 'none';
  loadNotes();
  renderSidebar();
}

// ── Search ─────────────────────────────────────────────────────────────────

let searchDebounce = null;
searchInput.addEventListener('input', () => {
  clearTimeout(searchDebounce);
  const q = searchInput.value.trim();
  if (!q) {
    state.searchMode = false;
    loadNotes();
    return;
  }
  searchDebounce = setTimeout(() => runSearch(q), 400);
});

async function runSearch(q) {
  state.searchMode = true;
  try {
    const results = await apiFetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: q, n_results: 10 }),
    });
    renderSearchResults(results);
  } catch (e) {
    console.error('runSearch', e);
    toast('Search error: ' + e.message, 'error');
  }
}

// ── Attachments ────────────────────────────────────────────────────────────

async function loadAttachments(noteId) {
  try {
    const atts = await apiFetch(`/api/notes/${noteId}/attachments`);
    renderAttachments(atts);
    const hasPending = atts.some(a => !a.indexed_at);
    if (hasPending) startAttPoll(noteId);
    else clearInterval(state.attPollTimer);
    state.attachmentSummaries = atts.filter(a => a.summary).map(a => ({ filename: a.filename, summary: a.summary }));
    renderSummarySection();
    dropZone.style.display = atts.length ? 'none' : '';
  } catch (e) {
    console.error('loadAttachments', e);
  }
}

function renderAttachments(atts) {
  attList.innerHTML = '';
  for (const att of atts) renderAttRow(att);
}

function renderAttRow(att) {
  const isWeb  = att.mime_type === 'text/html';
  const status = attStatus(att);
  const row = document.createElement('div');
  row.className = 'att-row';
  row.id = `att-${att.id}`;
  const sizeStr = att.size_bytes ? formatBytes(att.size_bytes) : '';
  const pages   = att.page_count ? ` · ${att.page_count}p` : '';
  row.innerHTML = `
    <span class="att-icon">${isWeb ? '🌐' : '📄'}</span>
    <div class="att-info">
      <div class="att-name" title="${esc(att.filename)}">${esc(att.filename)}</div>
      <div class="att-meta">${sizeStr}${pages}</div>
      ${att.summary ? `<div class="att-summary">${esc(att.summary)}</div>` : ''}
    </div>
    <span class="att-status ${status.cls}">${status.label}</span>
    <div class="att-actions">
      ${!isWeb && att.stored_path ? `<button class="att-btn" onclick="downloadAtt('${att.id}')">↓</button>` : ''}
      ${att.source_url ? `<a href="${esc(att.source_url)}" target="_blank" class="att-btn" style="text-decoration:none">↗</a>` : ''}
      <button class="att-btn danger" onclick="deleteAtt('${att.id}')">✕</button>
    </div>`;
  attList.appendChild(row);
}

function attStatus(att) {
  if (att.extraction_error) return { cls: 'error', label: 'extraction failed' };
  if (att.indexed_at)   return { cls: 'indexed',    label: 'indexed' };
  if (att.extracted_at) return { cls: 'indexing',   label: 'indexing…' };
  return { cls: 'extracting', label: 'extracting…' };
}

function startAttPoll(noteId) {
  clearInterval(state.attPollTimer);
  let attempts = 0;
  let failures = 0;
  state.attPollTimer = setInterval(async () => {
    attempts++;
    try {
      await loadAttachments(noteId);
      failures = 0;
    } catch (err) {
      console.warn('attachment poll failed', err);
      failures++;
      if (failures >= 3) {
        clearInterval(state.attPollTimer);
        toast('Could not reach server — please reload.', 'error');
      }
    }
    if (attempts > 90) clearInterval(state.attPollTimer);
  }, 2000);
}

async function downloadAtt(attId) {
  window.location.href = `/api/attachments/${attId}/download`;
}

// ── Delete attachment (inline confirm) ────────────────────────────────────

const _deleteAttConfirm = new Set();

async function deleteAtt(attId) {
  if (!_deleteAttConfirm.has(attId)) {
    _deleteAttConfirm.add(attId);
    const row = document.getElementById(`att-${attId}`);
    const btn = row?.querySelector('.att-btn.danger');
    if (btn) {
      btn.textContent = 'Sure?';
      setTimeout(() => {
        if (_deleteAttConfirm.has(attId)) {
          _deleteAttConfirm.delete(attId);
          btn.textContent = '✕';
        }
      }, 5000);
    }
    return;
  }
  _deleteAttConfirm.delete(attId);
  try {
    await apiFetch(`/api/attachments/${attId}`, { method: 'DELETE' });
    await loadAttachments(state.currentNoteId);
  } catch (e) {
    console.error('deleteAtt', e);
    toast('Delete failed: ' + e.message, 'error');
  }
}

// Drag-and-drop upload
dropZone.addEventListener('click', () => fileInput.click());
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  const file = e.dataTransfer.files[0];
  if (file) uploadFile(file);
});
fileInput.addEventListener('change', () => {
  if (fileInput.files[0]) uploadFile(fileInput.files[0]);
  fileInput.value = '';
});

function uploadFile(file) {
  if (!state.currentNoteId) { toast('Save the note before adding attachments.', 'error'); return; }
  if (file.type !== 'application/pdf') { toast('Only PDF files are accepted.', 'error'); return; }

  // Optimistic row
  const tempId = 'tmp-' + Date.now();
  const row = document.createElement('div');
  row.className = 'att-row';
  row.id = tempId;
  row.innerHTML = `
    <span class="att-icon">📄</span>
    <div class="att-info"><div class="att-name">${esc(file.name)}</div>
    <div class="att-meta">${formatBytes(file.size)}</div></div>
    <span class="att-status uploading">uploading…</span>
    <div class="att-actions"></div>`;
  attList.appendChild(row);

  const xhr = new XMLHttpRequest();
  const formData = new FormData();
  formData.append('file', file);

  xhr.upload.onprogress = e => {
    if (e.lengthComputable) {
      const pct = Math.round(e.loaded / e.total * 100);
      const statusEl = row.querySelector('.att-status');
      if (statusEl) statusEl.textContent = `uploading ${pct}%`;
    }
  };
  xhr.onload = async () => {
    row.remove();
    if (xhr.status === 202) {
      await loadAttachments(state.currentNoteId);
      startAttPoll(state.currentNoteId);
    } else {
      toast('Upload failed: ' + xhr.statusText, 'error');
    }
  };
  xhr.onerror = () => { row.remove(); toast('Upload error', 'error'); };
  xhr.open('POST', `/api/notes/${state.currentNoteId}/attachments`);
  xhr.send(formData);
}

// ── Reindex ────────────────────────────────────────────────────────────────

$('btn-reindex').addEventListener('click', async () => {
  if (!state.currentNoteId) return;
  setBadge('saving');
  try {
    await apiFetch(`/api/notes/${state.currentNoteId}/reindex`, { method: 'POST' });
    setBadge('indexed');
    toast('Note re-indexed.', 'success');
  } catch (e) {
    console.error('reindexNote', e);
    setBadge('error');
    toast('Reindex failed: ' + e.message, 'error');
  }
});

reindexAllBtn.addEventListener('click', async () => {
  try {
    const job = await apiFetch('/api/reindex', { method: 'POST' });
    reindexProgress.style.display = 'block';
    pollReindexJob(job.job_id, job.total);
  } catch (e) {
    console.error('reindexAll', e);
    toast('Reindex failed: ' + e.message, 'error');
  }
});

function pollReindexJob(jobId, total) {
  clearInterval(state.reindexPollTimer);
  let failures = 0;
  state.reindexPollTimer = setInterval(async () => {
    try {
      const job = await apiFetch(`/api/reindex/status?job_id=${jobId}`);
      failures = 0;
      const pct = total > 0 ? Math.round(job.completed / total * 100) : 0;
      reindexProgress.innerHTML = `
        <progress value="${job.completed}" max="${total}"></progress>
        <span>${job.completed} / ${total} notes (${pct}%)</span>`;
      if (job.status !== 'running') {
        clearInterval(state.reindexPollTimer);
        reindexProgress.style.display = 'none';
        if (job.status === 'completed_with_errors') {
          const errTitles = job.errors.map(e => e.title).join(', ');
          toast(`Reindex done with errors: ${errTitles}`, 'error');
        } else {
          toast('All notes re-indexed.', 'success');
        }
        await loadNotes();
      }
    } catch (err) {
      console.warn('reindex poll failed', err);
      failures++;
      if (failures >= 3) {
        clearInterval(state.reindexPollTimer);
        reindexProgress.style.display = 'none';
        toast('Could not reach server — please reload.', 'error');
      }
    }
  }, 3000);
}

// ── Journal dictation ──────────────────────────────────────────────────────

const dictateBtn    = $('btn-dictate');
const dictateStatus = $('dictate-status');
let mediaRecorder   = null;
let audioChunks     = [];

function setDictateStatus(msg) {
  if (msg) {
    dictateStatus.textContent = msg;
    dictateStatus.style.display = '';
  } else {
    dictateStatus.style.display = 'none';
  }
}

async function startDictation() {
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    toast('Microphone access denied.', 'error');
    return;
  }
  audioChunks = [];
  mediaRecorder = new MediaRecorder(stream);
  mediaRecorder.addEventListener('dataavailable', e => { if (e.data.size) audioChunks.push(e.data); });
  mediaRecorder.start(250);  // deliver chunks every 250ms so we always have data on stop
  dictateBtn.classList.add('recording');
  setDictateStatus('Recording… tap 🎤 to stop');
}

async function stopDictation() {
  if (!mediaRecorder || mediaRecorder.state === 'inactive') return;
  const mr = mediaRecorder;
  mediaRecorder = null;
  dictateBtn.classList.remove('recording');

  // Collect final chunk then stop
  await new Promise(resolve => {
    mr.addEventListener('dataavailable', e => { if (e.data.size) audioChunks.push(e.data); });
    mr.addEventListener('stop', resolve, { once: true });
    if (mr.state === 'recording') mr.stop();
    else resolve();
  });
  mr.stream.getTracks().forEach(t => t.stop());

  const mimeType = mr.mimeType || 'audio/webm';
  const blob = new Blob(audioChunks, { type: mimeType });
  console.log('dictate: blob size', blob.size, 'type', mimeType);

  if (blob.size < 1000) {
    setDictateStatus('');
    toast('Recording too short or no audio captured.', 'error');
    return;
  }

  const form = new FormData();
  form.append('audio', blob, 'dictation.webm');
  setDictateStatus(`Transcribing… (${(blob.size / 1024).toFixed(0)} KB)`);
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 90000);
  try {
    const data = await apiFetch('/api/journal/dictate', { method: 'POST', body: form, signal: controller.signal });
    clearTimeout(timeoutId);
    setDictateStatus('');
    await loadNotes();
    await loadSidebar();
    await openNote(data.id);
  } catch (e) {
    clearTimeout(timeoutId);
    setDictateStatus('');
    toast('Dictation failed: ' + (e.name === 'AbortError' ? 'timed out after 90s' : e.message), 'error');
  }
}

dictateBtn.addEventListener('click', () => {
  if (mediaRecorder && mediaRecorder.state !== 'inactive') {
    stopDictation();
  } else {
    startDictation();
  }
});

// ── Event bindings ─────────────────────────────────────────────────────────

$('btn-new-note').addEventListener('click', () => {
  clearTimers();
  state.currentNoteId = null;
  titleInput.value  = '';
  contentArea.value = '';
  folderInput.value = 'Unfiled';
  reminderInput.value = '';
  state.reminderDone = false;
  updateReminderDoneBtn();
  renderTagChips([]);
  setBadge('');
  attList.innerHTML = '';
  notePreview.innerHTML = '';
  state.tasksMode = false;
  $('tasks-panel').style.display = 'none';
  $('btn-tasks').classList.remove('active');
  editorPanel.style.display = 'flex';
  editorPanel.style.flexDirection = 'column';
  noteListPanel.style.display = 'none';
  closeSidebar();
  setEditMode(true);
  titleInput.focus();
});

$('btn-tasks').addEventListener('click', openTasksPanel);
$('btn-save').addEventListener('click', saveNote);
$('btn-home').addEventListener('click', closeEditor);
$('btn-refresh').addEventListener('click', loadNotes);
notePreview.addEventListener('click', () => setEditMode(true));
btnEditToggle.addEventListener('click', () => setEditMode(!state.editMode));

document.addEventListener('keydown', e => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); saveNote(); }
  if (e.key === 'Escape') {
    if (state.editMode) setEditMode(false);
    else if (state.currentNoteId) closeEditor();
  }
});

titleInput.addEventListener('input', () => { state.saveDirty = true; });
folderInput.addEventListener('change', () => { state.saveDirty = true; });
contentArea.addEventListener('input', () => {
  state.saveDirty = true;
});

function updateReminderDoneBtn() {
  if (!reminderInput.value) {
    btnReminderDone.style.display = 'none';
    return;
  }
  btnReminderDone.style.display = '';
  btnReminderDone.textContent = state.reminderDone ? '✓ Done' : 'Mark Done';
  btnReminderDone.disabled = state.reminderDone;
}

reminderInput.addEventListener('change', () => {
  state.reminderDone = false;
  state.saveDirty = true;
  updateReminderDoneBtn();
});

btnReminderDone.addEventListener('click', async () => {
  state.reminderDone = true;
  updateReminderDoneBtn();
  await saveNote();
});

// ── Utilities ──────────────────────────────────────────────────────────────

function clearTimers() {
  clearInterval(state.indexPollTimer);
  clearInterval(state.attPollTimer);
}

function esc(str) {
  return String(str ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function relTime(iso) {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return 'just now';
  if (diff < 3_600_000) return `${Math.floor(diff/60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff/3_600_000)}h ago`;
  return `${Math.floor(diff/86_400_000)}d ago`;
}

function formatBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  return (n/1048576).toFixed(1) + ' MB';
}

// ── Settings ───────────────────────────────────────────────────────────────

const settingsPanel = $('settings-panel');
const settingsHoursInput = $('settings-reminder-hours');

$('btn-settings-toggle').addEventListener('click', () => {
  const open = settingsPanel.style.display !== 'none';
  settingsPanel.style.display = open ? 'none' : 'block';
});

async function loadSettings() {
  try {
    const data = await apiFetch('/api/settings');
    settingsHoursInput.value = data.reminder_hours.join(', ');
  } catch (e) {
    console.warn('loadSettings failed', e);
  }
}

$('btn-settings-save').addEventListener('click', async () => {
  const raw = settingsHoursInput.value;
  const hours = raw.split(',').map(s => parseInt(s.trim(), 10)).filter(n => !isNaN(n));
  if (hours.some(h => h < 0 || h > 23)) {
    toast('Hours must be between 0 and 23.', 'error');
    return;
  }
  try {
    const data = await apiFetch('/api/settings', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reminder_hours: hours }),
    });
    settingsHoursInput.value = data.reminder_hours.join(', ');
    toast('Settings saved.', 'success');
  } catch (e) {
    toast('Save failed: ' + e.message, 'error');
  }
});

// ── Init ───────────────────────────────────────────────────────────────────

async function init() {
  await loadNotes();
  await loadSidebar();
  await loadSettings();
  apiFetch('/api/version').then(d => {
    const el = $('build-number');
    if (el) el.textContent = 'build ' + d.version;
  }).catch(() => {});

  // Check for ?note=<id> from share redirect
  const params = new URLSearchParams(location.search);
  const openId = params.get('note');
  if (openId) {
    history.replaceState({}, '', '/');
    openNote(openId);
  }
}

init();
