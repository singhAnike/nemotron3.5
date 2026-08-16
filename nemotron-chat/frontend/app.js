// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let history = [];        // [{role, content}] sent to the backend
let pendingAttachments = []; // [{kind:'image'|'text', filename, data_url|text}]
let isStreaming = false;

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------

const messagesEl = document.getElementById('messages');
const emptyStateEl = document.getElementById('empty-state');
const promptEl = document.getElementById('prompt');
const sendBtn = document.getElementById('send-btn');
const attachBtn = document.getElementById('attach-btn');
const fileInput = document.getElementById('file-input');
const attachmentsEl = document.getElementById('attachments');
const statusDot = document.getElementById('status-dot');
const statusText = document.getElementById('status-text');
const newChatBtn = document.getElementById('new-chat');

marked.setOptions({
  breaks: true,
  highlight: (code, lang) => {
    try {
      return lang && hljs.getLanguage(lang)
        ? hljs.highlight(code, { language: lang }).value
        : hljs.highlightAuto(code).value;
    } catch { return code; }
  }
});

// ---------------------------------------------------------------------------
// Textarea auto-grow
// ---------------------------------------------------------------------------

promptEl.addEventListener('input', () => {
  promptEl.style.height = 'auto';
  promptEl.style.height = Math.min(promptEl.scrollHeight, 200) + 'px';
});

promptEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

sendBtn.addEventListener('click', send);
newChatBtn.addEventListener('click', resetChat);

// ---------------------------------------------------------------------------
// File attach
// ---------------------------------------------------------------------------

attachBtn.addEventListener('click', () => fileInput.click());

fileInput.addEventListener('change', async () => {
  const files = Array.from(fileInput.files);
  fileInput.value = '';
  for (const file of files) {
    await uploadFile(file);
  }
});

async function uploadFile(file) {
  const chipId = 'chip-' + Math.random().toString(36).slice(2);
  renderAttachChip(chipId, file.name, null, true);

  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/api/upload', { method: 'POST', body: formData });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Upload failed');

    pendingAttachments.push({ ...data, chipId });
    renderAttachChip(chipId, file.name, data);
  } catch (err) {
    removeChip(chipId);
    setStatus('error', err.message);
  }
}

function renderAttachChip(chipId, filename, data, loading = false) {
  let chip = document.getElementById(chipId);
  if (!chip) {
    chip = document.createElement('div');
    chip.className = 'attach-chip';
    chip.id = chipId;
    attachmentsEl.appendChild(chip);
  }
  const isImage = data && data.kind === 'image';
  chip.innerHTML = `
    ${isImage ? `<img src="${data.data_url}" />` : ''}
    <span>${loading ? 'uploading…' : truncateName(filename)}</span>
    <span class="remove" data-id="${chipId}">×</span>
  `;
  chip.querySelector('.remove').addEventListener('click', () => {
    pendingAttachments = pendingAttachments.filter(a => a.chipId !== chipId);
    removeChip(chipId);
  });
}

function removeChip(chipId) {
  document.getElementById(chipId)?.remove();
}

function truncateName(name) {
  return name.length > 22 ? name.slice(0, 19) + '…' : name;
}

// ---------------------------------------------------------------------------
// Sending messages
// ---------------------------------------------------------------------------

async function send() {
  const text = promptEl.value.trim();
  if ((!text && pendingAttachments.length === 0) || isStreaming) return;

  emptyStateEl?.remove();

  const images = pendingAttachments.filter(a => a.kind === 'image');
  const textFiles = pendingAttachments.filter(a => a.kind === 'text');

  // Build the user-facing bubble
  renderUserMessage(text, images, textFiles);

  // Build the API content: text parts + image parts (OpenAI multimodal format)
  let contextBlock = '';
  for (const f of textFiles) {
    contextBlock += `\n\n--- Context from ${f.filename}${f.truncated ? ' (truncated)' : ''} ---\n${f.text}`;
  }

  let apiContent;
  if (images.length > 0) {
    apiContent = [];
    if (text || contextBlock) apiContent.push({ type: 'text', text: text + contextBlock });
    for (const img of images) {
      apiContent.push({ type: 'image_url', image_url: { url: img.data_url } });
    }
  } else {
    apiContent = text + contextBlock;
  }

  history.push({ role: 'user', content: apiContent });
  pendingAttachments = [];
  attachmentsEl.innerHTML = '';
  promptEl.value = '';
  promptEl.style.height = 'auto';

  await streamAssistantReply();
}

function renderUserMessage(text, images, textFiles) {
  const group = document.createElement('div');
  group.className = 'msg-group user';

  let imagesHtml = '';
  if (images.length) {
    imagesHtml = `<div class="msg-images">${images.map(i => `<img src="${i.data_url}" />`).join('')}</div>`;
  }
  let filesHtml = '';
  if (textFiles.length) {
    filesHtml = `<div class="msg-files">${textFiles.map(f => `<span class="file-chip">📄 ${f.filename}</span>`).join('')}</div>`;
  }

  group.innerHTML = `
    <div class="msg-role">user</div>
    <div class="msg-body">${imagesHtml}${filesHtml}${text ? `<p>${escapeHtml(text)}</p>` : ''}</div>
  `;
  messagesEl.appendChild(group);
  scrollToBottom();
}

async function streamAssistantReply() {
  isStreaming = true;
  setStatus('busy', 'thinking');
  sendBtn.disabled = true;

  const group = document.createElement('div');
  group.className = 'msg-group assistant';
  group.innerHTML = `<div class="msg-role assistant">nemotron</div><div class="msg-body"><span class="cursor-blink"></span></div>`;
  messagesEl.appendChild(group);
  const bodyEl = group.querySelector('.msg-body');
  scrollToBottom();

  let fullText = '';

  try {
    const res = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: history })
    });

    if (!res.ok || !res.body) throw new Error('Request failed (' + res.status + ')');

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const events = buffer.split('\n\n');
      buffer = events.pop(); // last (possibly incomplete) chunk stays in buffer

      for (const evt of events) {
        const line = evt.trim();
        if (!line.startsWith('data:')) continue;
        const payload = JSON.parse(line.slice(5).trim());

        if (payload.type === 'token') {
          fullText += payload.content;
          bodyEl.innerHTML = marked.parse(fullText) + '<span class="cursor-blink"></span>';
          scrollToBottom(true);
        } else if (payload.type === 'error') {
          bodyEl.innerHTML = `<div class="error-msg">⚠ ${escapeHtml(payload.message)}</div>`;
          setStatus('error', 'error');
        } else if (payload.type === 'done') {
          bodyEl.innerHTML = marked.parse(fullText || '_(empty response)_');
        }
      }
    }

    if (fullText) history.push({ role: 'assistant', content: fullText });
    setStatus('ready', 'ready');
  } catch (err) {
    bodyEl.innerHTML = `<div class="error-msg">⚠ ${escapeHtml(err.message)}</div>`;
    setStatus('error', 'error');
  } finally {
    isStreaming = false;
    sendBtn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

function scrollToBottom(onlyIfNearBottom = false) {
  if (onlyIfNearBottom) {
    const nearBottom = messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < 200;
    if (!nearBottom) return;
  }
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function setStatus(state, label) {
  statusDot.className = 'status-dot' + (state === 'busy' ? ' busy' : state === 'error' ? ' error' : '');
  statusText.textContent = label;
}

function resetChat() {
  history = [];
  pendingAttachments = [];
  attachmentsEl.innerHTML = '';
  messagesEl.innerHTML = `
    <div class="empty-state" id="empty-state">
      <div class="empty-mark">N</div>
      <h1>Nemotron console</h1>
      <p>Ask a question, drop in a doc, or attach an image for context.</p>
    </div>`;
  setStatus('ready', 'ready');
}
