// ---------------------------------------------------------------------------
// Agent mode: workspace session + tool-calling loop UI
// ---------------------------------------------------------------------------

let agentSessionId = null;
let agentWorkspace = null;
let currentTurnBody = null;   // the .msg-body element we keep appending this turn's events into
let currentTurnText = '';     // accumulated assistant prose for the current turn

const workspacePathEl = document.getElementById('workspace-path');
const workspaceSetBtn = document.getElementById('workspace-set-btn');
const workspaceActiveEl = document.getElementById('workspace-active');

workspaceSetBtn.addEventListener('click', openWorkspace);
workspacePathEl.addEventListener('keydown', (e) => { if (e.key === 'Enter') openWorkspace(); });

async function openWorkspace() {
  const path = workspacePathEl.value.trim();
  if (!path) return;

  workspaceSetBtn.disabled = true;
  workspaceSetBtn.textContent = '…';
  try {
    const res = await fetch('/api/agent/workspace', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path })
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'Could not open workspace');

    agentSessionId = data.session_id;
    agentWorkspace = data.workspace;
    workspaceActiveEl.innerHTML = `<span class="ws-dot"></span>${escapeHtml(agentWorkspace)}`;
    setStatus('ready', 'ready');
  } catch (err) {
    workspaceActiveEl.innerHTML = `<span class="ws-error">${escapeHtml(err.message)}</span>`;
  } finally {
    workspaceSetBtn.disabled = false;
    workspaceSetBtn.textContent = 'Open';
  }
}

function agentReset() {
  agentSessionId = null;
  agentWorkspace = null;
  currentTurnBody = null;
  currentTurnText = '';
  workspaceActiveEl.innerHTML = '';
  workspacePathEl.value = '';
}

// ---------------------------------------------------------------------------
// Sending a message in agent mode
// ---------------------------------------------------------------------------

async function agentSend(text) {
  if (!agentSessionId) {
    renderAgentSystemNote('Open a workspace folder first (left panel), then ask the agent to explore or change code.');
    return;
  }

  renderUserMessage(text, [], []);
  startNewTurn();

  await runAgentStream('/api/agent/message', {
    session_id: agentSessionId,
    text,
  });
}

function startNewTurn() {
  const group = document.createElement('div');
  group.className = 'msg-group assistant';
  group.innerHTML = `<div class="msg-role assistant">nemotron</div><div class="msg-body"><span class="cursor-blink"></span></div>`;
  messagesEl.appendChild(group);
  currentTurnBody = group.querySelector('.msg-body');
  currentTurnText = '';
  scrollToBottom();
}

function renderAgentSystemNote(text) {
  const group = document.createElement('div');
  group.className = 'msg-group assistant';
  group.innerHTML = `<div class="msg-role assistant">nemotron</div><div class="msg-body"><div class="error-msg">${escapeHtml(text)}</div></div>`;
  messagesEl.appendChild(group);
  scrollToBottom();
}

async function runAgentStream(url, body) {
  isStreaming = true;
  setStatus('busy', 'working');
  sendBtn.disabled = true;
  removeCursor();
  appendCursor();

  try {
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!res.ok || !res.body) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || `Request failed (${res.status})`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let awaitingApproval = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      const events = buffer.split('\n\n');
      buffer = events.pop();

      for (const evt of events) {
        const line = evt.trim();
        if (!line.startsWith('data:')) continue;
        const payload = JSON.parse(line.slice(5).trim());
        handleAgentEvent(payload);
        if (payload.type === 'awaiting_approval') awaitingApproval = true;
      }
    }

    if (awaitingApproval) {
      setStatus('ready', 'awaiting approval');
    } else {
      setStatus('ready', 'ready');
    }
  } catch (err) {
    removeCursor();
    appendToTurn(`<div class="error-msg">⚠ ${escapeHtml(err.message)}</div>`);
    setStatus('error', 'error');
  } finally {
    isStreaming = false;
    sendBtn.disabled = false;
  }
}

function handleAgentEvent(payload) {
  switch (payload.type) {
    case 'token':
      currentTurnText += payload.content;
      removeCursor();
      renderTurnText();
      appendCursor();
      scrollToBottom(true);
      break;

    case 'tool_call':
      removeCursor();
      appendToTurn(renderToolCallLine(payload.name, payload.args));
      scrollToBottom(true);
      break;

    case 'tool_result':
      appendToolResult(payload.content);
      scrollToBottom(true);
      break;

    case 'pending_approval':
      removeCursor();
      appendToTurn(renderApprovalCard(payload));
      wireApprovalButtons(payload.tool_call_id);
      scrollToBottom();
      break;

    case 'awaiting_approval':
      removeCursor();
      break;

    case 'done':
      removeCursor();
      renderTurnText();
      break;

    case 'error':
      removeCursor();
      appendToTurn(`<div class="error-msg">⚠ ${escapeHtml(payload.message)}</div>`);
      break;
  }
}

function renderTurnText() {
  if (!currentTurnBody) return;
  const existing = currentTurnBody.querySelector('.turn-prose');
  const html = marked.parse(currentTurnText || '');
  if (existing) {
    existing.innerHTML = html;
  } else {
    const div = document.createElement('div');
    div.className = 'turn-prose';
    div.innerHTML = html;
    currentTurnBody.appendChild(div);
  }
}

function appendToTurn(html) {
  if (!currentTurnBody) return;
  const wrap = document.createElement('div');
  wrap.innerHTML = html;
  currentTurnBody.appendChild(wrap.firstElementChild);
}

function appendCursor() {
  if (!currentTurnBody) return;
  const c = document.createElement('span');
  c.className = 'cursor-blink';
  currentTurnBody.appendChild(c);
}

function removeCursor() {
  currentTurnBody?.querySelector('.cursor-blink')?.remove();
}

function renderToolCallLine(name, args) {
  const argsPreview = Object.entries(args || {})
    .map(([k, v]) => `${k}: ${truncateStr(String(v), 60)}`)
    .join(', ');
  return `<div class="tool-call-line">
    <span class="tool-call-icon">▹</span>
    <span class="tool-call-name">${escapeHtml(name)}</span>
    <span class="tool-call-args">${escapeHtml(argsPreview)}</span>
  </div>`;
}

function appendToolResult(content) {
  if (!currentTurnBody) return;
  const lines = currentTurnBody.querySelectorAll('.tool-call-line');
  const lastLine = lines[lines.length - 1];
  if (!lastLine) return;

  const details = document.createElement('details');
  details.className = 'tool-result';
  details.innerHTML = `<summary>result</summary><pre>${escapeHtml(content)}</pre>`;
  lastLine.after(details);
}

function renderApprovalCard(payload) {
  const diffHtml = payload.diff.split('\n').map(line => {
    let cls = 'diff-ctx';
    if (line.startsWith('+') && !line.startsWith('+++')) cls = 'diff-add';
    else if (line.startsWith('-') && !line.startsWith('---')) cls = 'diff-del';
    else if (line.startsWith('@@')) cls = 'diff-hunk';
    return `<span class="${cls}">${escapeHtml(line)}</span>`;
  }).join('\n');

  return `<div class="approval-card" data-tool-call-id="${payload.tool_call_id}">
    <div class="approval-header">
      <span class="approval-tool">${escapeHtml(payload.name)}</span>
      <span class="approval-path">${escapeHtml(payload.path || '')}</span>
    </div>
    <pre class="diff-block">${diffHtml}</pre>
    <div class="approval-actions">
      <button class="approve-btn" data-action="approve" data-id="${payload.tool_call_id}">Approve</button>
      <button class="reject-btn" data-action="reject" data-id="${payload.tool_call_id}">Reject</button>
    </div>
  </div>`;
}

function wireApprovalButtons(toolCallId) {
  const card = currentTurnBody.querySelector(`.approval-card[data-tool-call-id="${toolCallId}"]`);
  if (!card) return;
  card.querySelectorAll('button').forEach(btn => {
    btn.addEventListener('click', async () => {
      card.querySelectorAll('button').forEach(b => b.disabled = true);
      const approved = btn.dataset.action === 'approve';
      card.classList.add(approved ? 'resolved-approved' : 'resolved-rejected');
      card.querySelector('.approval-actions').innerHTML =
        `<span class="resolved-label">${approved ? '✓ approved' : '✕ rejected'}</span>`;

      appendCursor();
      scrollToBottom(true);

      await runAgentStream('/api/agent/approve', {
        session_id: agentSessionId,
        tool_call_id: toolCallId,
        approved,
      });
    }, { once: true });
  });
}

function truncateStr(s, n) {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

window.agentSend = agentSend;
window.agentReset = agentReset;
