/* ============================================
   智能体平台 - 前端交互
   功能：Markdown 渲染 / 代码高亮 / 轮询 / 审批
   ============================================ */

const API_BASE = '';
let currentTaskId = null;
let pollTimer = null;
let eventTimer = null;
let messageTimer = null;
let streamSource = null;
let knownEventIds = new Set();
let knownMessageIds = new Set();
let lastTaskStatus = null;
let isApproving = false;
let recentTasks = [];
let lastActivityTime = Date.now();

/* ---- Markdown 配置 ---- */
function setupMarkdown() {
    if (typeof marked === 'undefined') return;

    marked.setOptions({
        breaks: true,
        gfm: true,
        highlight(code, lang) {
            if (lang && typeof hljs !== 'undefined' && hljs.getLanguage(lang)) {
                try { return hljs.highlight(code, { language: lang }).value; }
                catch (_) { /* fall through */ }
            }
            if (typeof hljs !== 'undefined') {
                try { return hljs.highlightAuto(code).value; }
                catch (_) { /* fall through */ }
            }
            return escapeHtml(code);
        }
    });
}

function renderMarkdown(text) {
    if (typeof marked === 'undefined') return escapeHtml(text);
    try { return marked.parse(text); }
    catch (_) { return escapeHtml(text); }
}

/* ---- 初始化 ---- */
document.addEventListener('DOMContentLoaded', () => {
    setupMarkdown();
    initTabs();
    initForm();
    initMobileMenu();
    initNewChat();
    initHintChips();
    initPreviewPanel();
    initPreviewResize();
    initPreviewFullscreen();
    autoResizeTextarea();
    loadRecentConversations();
});

/* ---- 移动端菜单 ---- */
function initMobileMenu() {
    const toggle = document.getElementById('menuToggle');
    const sidebar = document.getElementById('sidebar');
    const backdrop = document.getElementById('sidebarBackdrop');

    if (!toggle || !sidebar) return;

    const open = () => {
        sidebar.classList.add('open');
        backdrop.classList.add('open');
    };
    const close = () => {
        sidebar.classList.remove('open');
        backdrop.classList.remove('open');
    };

    toggle.addEventListener('click', () => {
        sidebar.classList.contains('open') ? close() : open();
    });
    backdrop.addEventListener('click', close);
}

/* ---- 新建对话 ---- */
function initNewChat() {
    const btn = document.getElementById('newChatBtn');
    if (!btn) return;
    btn.addEventListener('click', resetChat);
}

function resetChat() {
    if (pollTimer) clearInterval(pollTimer);
    if (eventTimer) clearInterval(eventTimer);
    if (messageTimer) clearInterval(messageTimer);

    pollTimer = null;
    eventTimer = null;
    messageTimer = null;
    currentTaskId = null;
    lastTaskStatus = null;
    lastActivityTime = Date.now();
    knownEventIds.clear();
    knownMessageIds.clear();
    isApproving = false;

    const stream = document.getElementById('chatStream');
    stream.innerHTML = '';

    const empty = document.getElementById('chatEmpty');
    if (empty) {
        empty.style.display = '';
        stream.appendChild(empty);
    }

    const approvalBar = document.getElementById('approval-bar');
    if (approvalBar) approvalBar.style.display = 'none';

    document.getElementById('runningBar')?.style.setProperty('display', 'none');

    document.getElementById('taskInput').value = '';
    document.getElementById('taskInput').focus();

    if (window.innerWidth < 768) {
        document.getElementById('sidebar')?.classList.remove('open');
        document.getElementById('sidebarBackdrop')?.classList.remove('open');
    }
}

/* ---- 最近对话列表 ---- */
async function loadRecentConversations() {
    try {
        const tasks = await api('/tasks?limit=20');
        recentTasks = tasks || [];
        renderRecentConversations();
    } catch (err) {
        console.error('Load recent conversations error:', err);
    }
}

function renderRecentConversations() {
    const container = document.getElementById('recentItems');
    if (!container) return;

    if (!recentTasks.length) {
        container.innerHTML = '<div class="recent-empty">暂无历史对话</div>';
        return;
    }

    container.innerHTML = recentTasks.map(t => `
        <button class="recent-item ${t.task_id === currentTaskId ? 'active' : ''}" onclick="loadTaskDetail('${t.task_id}')">
            <span class="recent-item-icon">💬</span>
            <span class="recent-item-title">${escapeHtml(t.user_input || '无标题')}</span>
            <span class="delete-btn" onclick="event.stopPropagation(); deleteTask('${t.task_id}')">✕</span>
        </button>
    `).join('');
}

/* ---- 输入框自动高度 ---- */
function autoResizeTextarea() {
    const ta = document.getElementById('taskInput');
    if (!ta) return;
    ta.addEventListener('input', () => {
        ta.style.height = 'auto';
        ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
    });
}

/* ---- 预览面板 ---- */
function initPreviewPanel() {
    document.getElementById('previewClose')?.addEventListener('click', closePreview);
    document.getElementById('previewBackdrop')?.addEventListener('click', closePreview);
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closePreview();
    });
}

/* ---- 预览面板 拖拽调整宽度 + 全屏 ---- */
function initPreviewResize() {
    const panel = document.getElementById('previewPanel');
    const handle = document.getElementById('previewResizeHandle');
    if (!panel || !handle) return;

    let isResizing = false;
    let startX = 0;
    let startWidth = 0;

    const startResize = (clientX) => {
        isResizing = true;
        startX = clientX;
        startWidth = panel.offsetWidth;
        panel.style.transition = 'none';
        document.body.style.cursor = 'ew-resize';
        document.body.style.userSelect = 'none';
    };

    const moveResize = (clientX) => {
        if (!isResizing) return;
        const dx = startX - clientX;
        const newWidth = Math.max(280, Math.min(startWidth + dx, window.innerWidth - 20));
        panel.style.setProperty('--preview-width', newWidth + 'px');
        panel.style.width = newWidth + 'px';
    };

    const stopResize = () => {
        if (!isResizing) return;
        isResizing = false;
        panel.style.transition = '';
        document.body.style.cursor = '';
        document.body.style.userSelect = '';
    };

    handle.addEventListener('mousedown', (e) => {
        startResize(e.clientX);
        e.preventDefault();
    });
    document.addEventListener('mousemove', (e) => moveResize(e.clientX));
    document.addEventListener('mouseup', stopResize);

    handle.addEventListener('touchstart', (e) => {
        startResize(e.touches[0].clientX);
    }, { passive: true });
    document.addEventListener('touchmove', (e) => {
        moveResize(e.touches[0].clientX);
    }, { passive: true });
    document.addEventListener('touchend', stopResize);
}

function initPreviewFullscreen() {
    const btn = document.getElementById('previewFullscreen');
    const panel = document.getElementById('previewPanel');
    if (!btn || !panel) return;

    btn.addEventListener('click', () => {
        if (panel.classList.contains('preview-fullscreen')) {
            panel.classList.remove('preview-fullscreen');
            btn.textContent = '⛶ 全屏';
        } else {
            panel.classList.add('preview-fullscreen');
            btn.textContent = '⛶ 退出全屏';
        }
    });
}

/* ---- 标签页 ---- */
function initTabs() {
    document.querySelectorAll('.nav-item, .more-item').forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const tab = item.dataset.tab;
            switchTab(tab);
        });
    });
}

function switchTab(tab) {
    document.querySelectorAll('.nav-item, .more-item').forEach(n => n.classList.remove('active'));
    document.querySelector(`.nav-item[data-tab="${tab}"], .more-item[data-tab="${tab}"]`)?.classList.add('active');

    document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
    const panel = document.getElementById(`panel-${tab}`);
    if (panel) panel.classList.add('active');

    if (tab === 'history') loadTaskHistory();
    if (tab === 'skills') loadSkills();
    if (tab === 'review') loadReviews();

    if (window.innerWidth < 768) {
        document.getElementById('sidebar')?.classList.remove('open');
        document.getElementById('sidebarBackdrop')?.classList.remove('open');
    }
}

/* ---- 空状态提示 chips ---- */
function initHintChips() {
    document.querySelectorAll('.empty-hint-chip').forEach(chip => {
        chip.addEventListener('click', () => {
            const msg = chip.dataset.msg || chip.textContent.trim();
            document.getElementById('taskInput').value = msg;
            document.getElementById('taskInput').focus();
        });
    });
}

/* ---- 表单提交 ---- */
function initForm() {
    const form = document.getElementById('taskForm');
    const input = document.getElementById('taskInput');
    if (!form || !input) return;

    form.addEventListener('submit', (e) => {
        e.preventDefault();
        submitTask();
    });

    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            submitTask();
        }
    });
}

/* ---- API 封装 ---- */
async function api(path, options = {}) {
    const res = await fetch(API_BASE + path, {
        headers: { 'Content-Type': 'application/json' },
        ...options,
        body: options.body ? JSON.stringify(options.body) : undefined,
    });
    if (!res.ok) {
        const text = await res.text();
        throw new Error(`HTTP ${res.status}: ${text}`);
    }
    const ct = res.headers.get('content-type') || '';
    if (ct.includes('application/json')) return res.json();
    return res.text();
}

/* ---- Toast ---- */
function showToast(message, type = 'info') {
    const container = document.getElementById('toastContainer');
    const icons = { success: '✅', error: '❌', info: '💡' };
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.innerHTML =
        `<span class="toast-icon">${icons[type] || 'ℹ️'}</span><span class="toast-message">${escapeHtml(message)}</span>`;
    container.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('removing');
        toast.addEventListener('animationend', () => toast.remove());
    }, 3500);
}

/* ---- 提交任务 ---- */
async function submitTask() {
    const input = document.getElementById('taskInput');
    const message = input.value.trim();
    if (!message) return;

    const btn = document.getElementById('submitBtn');
    btn.disabled = true;
    btn.innerHTML = '<span class="btn-icon">⏳</span>';

    try {
        const res = await api('/chat', { method: 'POST', body: { message } });
        if (res.task_id) {
            currentTaskId = res.task_id;
            lastTaskStatus = null;
            knownEventIds.clear();
            knownMessageIds.clear();

            input.value = '';
            input.style.height = 'auto';

            addSystemMessage('任务已提交，Agent 正在思考...');
            startPolling(res.task_id);
            showToast('任务已提交', 'success');

            // 刷新最近对话列表
            loadRecentConversations();
        } else {
            showToast('提交失败: ' + (res.message || '未知错误'), 'error');
        }
    } catch (err) {
        showToast('提交失败: ' + err.message, 'error');
        console.error(err);
    } finally {
        btn.disabled = false;
        btn.innerHTML = `<svg viewBox="0 0 16 16" width="16" height="16" fill="currentColor"><path d="M1.5 8l6-4.5L14.5 8l-6 4.5L1.5 8zm0 0L8 2.5 14.5 8 8 13.5 1.5 8z"/></svg>`;
        input.focus();
    }
}

/* ---- 轮询 / 流式 ---- */
function startPolling(taskId) {
    stopPolling();
    knownEventIds.clear();
    knownMessageIds.clear();
    lastTaskStatus = null;
    lastActivityTime = Date.now();
    isApproving = false;

    // 优先尝试 SSE 流式
    const streamUrl = `/tasks/${taskId}/stream`;
    streamSource = new EventSource(streamUrl);

    streamSource.addEventListener('message', (e) => {
        try {
            const data = JSON.parse(e.data);
            appendStreamMessage(data);
        } catch (_) { /* ignore malformed */ }
    });

    streamSource.addEventListener('task_started', (e) => {
        try {
            const data = JSON.parse(e.data);
            appendStreamEvent('runner_started', data);
        } catch (_) {}
    });

    streamSource.addEventListener('chain', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.name && data.name !== 'agent') {
                appendStreamEvent('chain', data);
            }
        } catch (_) {}
    });

    streamSource.addEventListener('model', (e) => { /* 可选：模型调用事件 */ });
    streamSource.addEventListener('tool', (e) => { /* 可选：工具事件 */ });

    streamSource.addEventListener('event', (e) => {
        try {
            const data = JSON.parse(e.data);
            appendStreamEvent(data.event_type || 'event', data);
        } catch (_) {}
    });

    streamSource.addEventListener('waiting_approval', () => {
        lastActivityTime = Date.now();
        updateRunningBar();
    });

    streamSource.addEventListener('task_completed', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.content) appendStreamMessage({role: 'assistant', content: data.content});
        } catch (_) {}
        updateRunningBar();
        stopPolling();
        loadRecentConversations();
    });

    streamSource.addEventListener('task_failed', (e) => {
        try {
            const data = JSON.parse(e.data);
            appendStreamEvent('task_failed', data);
        } catch (_) {}
        stopPolling();
    });

    streamSource.addEventListener('heartbeat', () => {
        // 保持连接活跃，静默处理
    });

    streamSource.addEventListener('info', (e) => {
        try {
            const data = JSON.parse(e.data);
            if (data.fallback === 'polling') {
                // 服务端未开启 streaming，降级到轮询
                startLegacyPolling(taskId);
                streamSource.close();
                streamSource = null;
            }
        } catch (_) {}
    });

    streamSource.addEventListener('error', () => {
        // SSE 连接异常，降级到轮询
        if (streamSource && streamSource.readyState === EventSource.CLOSED) {
            startLegacyPolling(taskId);
            streamSource = null;
        }
    });

    // 兜底：若 SSE 在 2s 内未收到任何事件，降级到传统轮询
    const fallbackTimer = setTimeout(() => {
        if (streamSource) {
            startLegacyPolling(taskId);
            streamSource.close();
            streamSource = null;
        }
    }, 2000);

    streamSource._fallbackTimer = fallbackTimer;
}

function startLegacyPolling(taskId) {
    eventTimer = setInterval(async () => {
        try {
            const events = await api(`/tasks/${taskId}/events`);
            appendNewEvents(events);
        } catch (_) { /* silent */ }
    }, 1500);

    messageTimer = setInterval(async () => {
        try {
            const messages = await api(`/tasks/${taskId}/messages`);
            appendNewMessages(messages);
        } catch (_) { /* silent */ }
    }, 1000);

    pollTimer = setInterval(async () => {
        try {
            const task = await api(`/tasks/${taskId}`);
            renderTaskStatus(task);
            updateRunningBar();
            if (['completed', 'failed', 'cancelled'].includes(task.status)) {
                stopPolling();
                loadRecentConversations();
            }
        } catch (err) {
            console.error('Poll error:', err);
            if (err.message.includes('404') || err.message.includes('Failed to fetch')) {
                stopPolling();
            }
        }
    }, 1500);
}

function stopPolling() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (eventTimer) { clearInterval(eventTimer); eventTimer = null; }
    if (messageTimer) { clearInterval(messageTimer); messageTimer = null; }
    if (streamSource) {
        if (streamSource._fallbackTimer) clearTimeout(streamSource._fallbackTimer);
        streamSource.close();
        streamSource = null;
    }
    document.getElementById('runningBar')?.style.setProperty('display', 'none');
}

/* ---- 渲染事件 ---- */
function appendNewEvents(events) {
    if (!events || !events.length) return;
    const stream = document.getElementById('chatStream');
    const empty = document.getElementById('chatEmpty');
    if (empty) empty.style.display = 'none';

    let hasNew = false;
    for (const ev of events) {
        const evId = `${ev.event_type}-${ev.created_at}`;
        if (knownEventIds.has(evId)) continue;
        knownEventIds.add(evId);

        // 静默跳过纯内部生命周期事件，但仍更新状态
        const INTERNAL_EVENTS = new Set([
            'task_created', 'runner_started', 'agent_invoke_start', 'agent_invoke_done'
        ]);
        if (INTERNAL_EVENTS.has(ev.event_type)) continue;

        switch (ev.event_type) {
            case 'task_completed':
            case 'task_completed_with_reviews':
                lastTaskStatus = 'completed';
                updateRunningBar();
                continue;
            case 'task_failed':
                lastTaskStatus = 'failed';
                updateRunningBar();
                continue;
            case 'task_cancelled':
                lastTaskStatus = 'cancelled';
                updateRunningBar();
                continue;
            case 'artifact_created':
                continue; // 产物事件不在聊天区刷屏，静默跳过
        }

        hasNew = true;
        const div = document.createElement('div');
        div.className = 'system-message';
        const time = new Date(ev.created_at).toLocaleTimeString('zh-CN');
        let detail = '';

        switch (ev.event_type) {
            case 'status_changed':
                detail = `状态 → ${ev.data?.new_status || ''}`;
                break;
            case 'interrupt_detected':
                detail = '检测到中断，等待审批...';
                break;
            case 'graph_interrupt':
                detail = '任务执行中断';
                break;
            case 'resume_started':
                detail = '审批已通过，继续执行...';
                break;
            default:
                detail = ev.event_type;
        }

        div.textContent = `[${time}] ${detail}`;
        stream.appendChild(div);
    }

    if (hasNew) {
        lastActivityTime = Date.now();
        updateRunningBar();
        scrollToBottom();
    }
}

/* ---- 渲染消息（核心 — Markdown） ---- */
function appendNewMessages(messages) {
    if (!messages || !messages.length) return;
    const stream = document.getElementById('chatStream');
    const empty = document.getElementById('chatEmpty');
    if (empty) empty.style.display = 'none';

    let hasNew = false;
    for (const msg of messages) {
        const msgId =
            `${msg.role}-${msg.created_at}-${msg.content}-${msg.extra?.tool_call_id || ''}`;
        if (knownMessageIds.has(msgId)) continue;
        knownMessageIds.add(msgId);
        hasNew = true;

        const wrapper = document.createElement('div');
        const time = new Date(msg.created_at).toLocaleTimeString('zh-CN');

        switch (msg.role) {
            case 'user':
                wrapper.className = 'message user-message';
                wrapper.innerHTML = `
                <div class="message-avatar">U</div>
                <div class="message-content">
                    <div class="message-header">
                        <span class="message-role">你</span>
                        <span class="message-time">${time}</span>
                    </div>
                    <div class="message-body">${escapeHtml(msg.content)}</div>
                </div>`;
                break;

            case 'assistant':
                wrapper.className = 'message agent-message';
                const mdHtml = renderMarkdown(msg.content);
                wrapper.innerHTML = `
                <div class="message-avatar">✦</div>
                <div class="message-content">
                    <div class="message-header">
                        <span class="message-role">Agent</span>
                        <span class="message-time">${time}</span>
                    </div>
                    <div class="message-body assistant-body">${mdHtml}</div>
                </div>`;
                // 高亮代码块
                requestAnimationFrame(() => {
                    wrapper.querySelectorAll('pre code').forEach(block => {
                        if (typeof hljs !== 'undefined') {
                            hljs.highlightElement(block);
                        }
                        wrapCodeBlock(block);
                    });
                });
                break;

            case 'tool':
                wrapper.className = 'message agent-message';
                wrapper.innerHTML = buildToolMessage(msg, time);
                bindToolCards(wrapper);
                break;

            default:
                wrapper.className = 'system-message';
                wrapper.textContent = msg.content;
        }

        stream.appendChild(wrapper);
    }

    linkifyArtifacts(stream, currentTaskId);
    if (hasNew) {
        lastActivityTime = Date.now();
        updateRunningBar();
        scrollToBottom();
    }
}

/* ---- 代码块包装（加语言标签 + 复制按钮） ---- */
function wrapCodeBlock(codeEl) {
    const pre = codeEl.parentElement;
    if (pre.dataset.wrapped) return;
    pre.dataset.wrapped = '1';

    const raw = codeEl.textContent;
    // 检测语言
    let lang = '';
    const classes = (codeEl.className || '').match(/language-(\w+)/);
    if (classes) lang = classes[1];
    if (!lang && codeEl.dataset.hljsLanguage) lang = codeEl.dataset.hljsLanguage;

    const header = document.createElement('div');
    header.className = 'code-block-header';

    const label = document.createElement('span');
    label.textContent = lang || 'code';
    header.appendChild(label);

    const copyBtn = document.createElement('button');
    copyBtn.className = 'code-copy-btn';
    copyBtn.innerHTML = '📋 复制';
    copyBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(raw).then(() => {
            copyBtn.classList.add('copied');
            copyBtn.innerHTML = '✓ 已复制';
            setTimeout(() => {
                copyBtn.classList.remove('copied');
                copyBtn.innerHTML = '📋 复制';
            }, 2000);
        }).catch(() => {
            fallbackCopy(raw, copyBtn);
        });
    });

    header.appendChild(copyBtn);
    pre.insertBefore(header, codeEl);
}

function fallbackCopy(text, btn) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
        document.execCommand('copy');
        btn.classList.add('copied');
        btn.innerHTML = '✓ 已复制';
        setTimeout(() => {
            btn.classList.remove('copied');
            btn.innerHTML = '📋 复制';
        }, 2000);
    } catch (_) { /* ignore */ }
    document.body.removeChild(ta);
}

/* ---- SSE 流式消息/事件渲染 ---- */
function appendStreamMessage(msgData) {
    const stream = document.getElementById('chatStream');
    const empty = document.getElementById('chatEmpty');
    if (empty) empty.style.display = 'none';

    const wrapper = document.createElement('div');
    const time = new Date().toLocaleTimeString('zh-CN');

    switch (msgData.role) {
        case 'user':
            wrapper.className = 'message user-message';
            wrapper.innerHTML = `
            <div class="message-avatar">U</div>
            <div class="message-content">
                <div class="message-header">
                    <span class="message-role">你</span>
                    <span class="message-time">${time}</span>
                </div>
                <div class="message-body">${escapeHtml(msgData.content)}</div>
            </div>`;
            break;
        case 'assistant':
            wrapper.className = 'message agent-message';
            const mdHtml = renderMarkdown(msgData.content);
            wrapper.innerHTML = `
            <div class="message-avatar">${msgData.agent && msgData.agent !== 'coordinator' ? escapeHtml(msgData.agent) : '✦'}</div>
            <div class="message-content">
                <div class="message-header">
                    <span class="message-role">${msgData.agent && msgData.agent !== 'coordinator' ? escapeHtml(msgData.agent) : 'Agent'}</span>
                    <span class="message-time">${time}</span>
                </div>
                <div class="message-body assistant-body">${mdHtml}</div>
            </div>`;
            requestAnimationFrame(() => {
                wrapper.querySelectorAll('pre code').forEach(block => {
                    if (typeof hljs !== 'undefined') hljs.highlightElement(block);
                    wrapCodeBlock(block);
                });
            });
            break;
        case 'tool':
            wrapper.className = 'message agent-message';
            wrapper.innerHTML = buildToolMessage({
                content: msgData.content,
                extra: { name: msgData.name || 'tool', status: 'success', agent: msgData.agent }
            }, time);
            break;
        case 'system':
            wrapper.className = 'system-message';
            wrapper.textContent = msgData.content;
            break;
        default:
            return;
    }

    stream.appendChild(wrapper);
    lastActivityTime = Date.now();
    scrollToBottom();
}

function appendStreamEvent(eventType, data) {
    const stream = document.getElementById('chatStream');
    const empty = document.getElementById('chatEmpty');
    if (empty) empty.style.display = 'none';

    const INTERNAL_EVENTS = new Set([
        'task_created', 'runner_started', 'agent_invoke_start', 'agent_invoke_done'
    ]);
    if (INTERNAL_EVENTS.has(eventType)) return;

    const div = document.createElement('div');
    div.className = 'system-message';
    const time = new Date().toLocaleTimeString('zh-CN');

    switch (eventType) {
        case 'task_completed':
        case 'task_completed_auto':
            lastTaskStatus = 'completed';
            updateRunningBar();
            return;
        case 'task_failed':
            div.textContent = `[${time}] 任务失败: ${data.error || '未知错误'}`;
            lastTaskStatus = 'failed';
            updateRunningBar();
            break;
        case 'waiting_approval':
            div.textContent = `[${time}] 等待审批中...`;
            break;
        case 'graph_interrupt':
            div.textContent = `[${time}] 任务执行中断`;
            break;
        case 'chain':
            if (data.name && data.name !== 'agent') {
                div.textContent = `[${time}] 子智能体 [${data.name}] 开始执行`;
            }
            break;
        default:
            div.textContent = `[${time}] ${eventType}`;
    }

    stream.appendChild(div);
    lastActivityTime = Date.now();
    updateRunningBar();
    scrollToBottom();
}

/* ---- Tool 消息卡片 ---- */
function buildToolMessage(msg, time) {
    const name = msg.extra?.name || 'tool';
    const toolCallId = msg.extra?.tool_call_id || '';
    const status = msg.extra?.status || 'success';
    const content = msg.content || '(无返回内容)';
    const args = msg.extra?.args;
    const isPending = status === 'pending';
    const statusLabel = isPending ? '执行中...' : (status === 'success' ? '已完成' : status);
    const autoOpen = isPending ? ' open' : '';

    return `
    <div class="message-avatar">🔧</div>
    <div class="message-content">
        <div class="message-header">
            <span class="message-role">Tool · ${escapeHtml(name)}</span>
            <span class="message-time">${time}</span>
        </div>
        <div class="tool-card-wrapper">
            <div class="tool-card${autoOpen}" ${toolCallId ? `data-tool-call-id="${toolCallId}"` : ''}>
                <div class="tool-card-header">
                    <span class="tool-icon">🔧</span>
                    <span class="tool-name">${escapeHtml(name)}</span>
                    <span class="tool-status ${status}"><span class="tool-status-dot"></span>${statusLabel}</span>
                    <span class="tool-toggle">${isPending ? '⏳' : '▼'}</span>
                </div>
                <div class="tool-card-body">
                    <div class="tool-body-inner">
                        ${args ? `<div class="tool-section">
                            <div class="tool-section-label">调用参数</div>
                            <pre>${escapeHtml(typeof args === 'string' ? args : JSON.stringify(args, null, 2))}</pre>
                        </div>` : ''}
                        <div class="tool-section">
                            <div class="tool-section-label">返回结果</div>
                            ${renderMarkdown(content)}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>`;
}

function bindToolCards(wrapper) {
    const headers = wrapper.querySelectorAll('.tool-card-header');
    headers.forEach(h => {
        h.addEventListener('click', () => {
            h.parentElement.classList.toggle('open');
        });
    });
}

/* ---- 文件预览面板 ---- */
let currentPreviewTaskId = null;

function openPreview(taskId, artifactPath, fileName) {
    currentPreviewTaskId = taskId;
    const panel = document.getElementById('previewPanel');
    const panelTasks = document.getElementById('panel-tasks');
    const nameEl = document.getElementById('previewFileName');
    const contentEl = document.getElementById('previewContent');
    const dl = document.getElementById('previewDownload');

    panel.classList.add('open');
    if (panelTasks) panelTasks.classList.add('has-preview');
    nameEl.textContent = fileName || artifactPath.split('/').pop() || '文件';
    contentEl.className = 'preview-content loading';
    contentEl.innerHTML = '加载中...';
    dl.style.display = '';

    const ext = (fileName || artifactPath).split('.').pop().toLowerCase();
    const imageExts = ['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp', 'bmp', 'ico'];
    if (imageExts.includes(ext)) {
        // 图片直接渲染，不走文本预览接口
        contentEl.className = 'preview-content';
        contentEl.innerHTML = '';
        const img = document.createElement('img');
        img.src = `/tasks/${taskId}/artifacts/${artifactPath}`;
        img.alt = fileName;
        img.style.maxWidth = '100%';
        img.style.borderRadius = 'var(--radius-sm)';
        contentEl.appendChild(img);
        dl.href = `/tasks/${taskId}/artifacts/${artifactPath}`;
        dl.download = fileName;
        return;
    }

    api(`/tasks/${taskId}/preview/${artifactPath}`)
        .then(data => {
            const text = data.content || '';
            if (ext === 'md' || ext === 'markdown') {
                contentEl.className = 'preview-content markdown-body';
                contentEl.innerHTML = renderMarkdown(text);
            } else {
                contentEl.className = 'preview-content';
                const pre = document.createElement('pre');
                pre.style.cssText = 'margin:0;background:transparent;padding:0;border:none;overflow:auto;';
                const code = document.createElement('code');
                code.textContent = text;
                code.className = `language-${ext}`;
                pre.appendChild(code);
                contentEl.innerHTML = '';
                contentEl.appendChild(pre);
                if (typeof hljs !== 'undefined') {
                    try { hljs.highlightElement(code); } catch (_) {}
                }
            }
            dl.href = `/tasks/${taskId}/artifacts/${artifactPath}`;
            dl.download = data.name;
        })
        .catch(err => {
            contentEl.className = 'preview-content error';
            contentEl.textContent = `加载失败: ${err.message}`;
        });
}

function closePreview() {
    const panel = document.getElementById('previewPanel');
    const panelTasks = document.getElementById('panel-tasks');
    panel.classList.remove('open', 'preview-fullscreen');
    if (panelTasks) panelTasks.classList.remove('has-preview');
    currentPreviewTaskId = null;
}

/* ---- 消息内容中的文件路径转链接 ---- */
function linkifyArtifacts(container, taskId) {
    if (!container || !taskId) return;
    // 支持 /workspace/xxx 或 workspace/xxx，自动去除末尾标点/反引号
    const regex = /(\/workspace\/|workspace\/)[^\s)>\]]+/g;
    container.querySelectorAll('.message-content').forEach(body => {
        if (body.dataset.linked === '1') return;
        body.dataset.linked = '1';
        const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
        const textNodes = [];
        while (walker.nextNode()) textNodes.push(walker.currentNode);
        textNodes.forEach(node => {
            const text = node.textContent;
            if (!regex.test(text)) return;
            regex.lastIndex = 0;
            const frag = document.createDocumentFragment();
            let last = 0;
            let m;
            while ((m = regex.exec(text)) !== null) {
                if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
                const raw = m[0].replace(/[`'")>\]\s]+$/g, '');
                const path = raw.replace(/^\//, '');
                const name = path.split('/').pop();
                const a = document.createElement('a');
                a.className = 'artifact-link';
                a.href = '#';
                a.textContent = `📄 ${name}`;
                a.addEventListener('click', (e) => {
                    e.preventDefault();
                    openPreview(taskId, path, name);
                });
                frag.appendChild(a);
                last = m.index + m[0].length;
            }
            if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
            if (frag.childNodes.length) node.replaceWith(frag);
        });
    });
}

/* ---- 状态渲染 ---- */
function renderTaskStatus(task) {
    if (lastTaskStatus === task.status) return;
    lastTaskStatus = task.status;

    updateApprovalBar(task);
    updateRunningBar();
}

function updateApprovalBar(task) {
    const bar = document.getElementById('approval-bar');
    if (!bar) return;

    if (task.status === 'waiting_approval' && !isApproving) {
        bar.innerHTML = `
        <div class="approval-card">
            <div class="approval-title">⚠️ 需要人工审批</div>
            <div class="approval-detail">任务执行需要审批才能继续，请检查以下操作并做出决定。</div>
            <div class="approval-buttons">
                <button class="btn btn-success" id="btn-approve-${task.task_id}">✓ 批准</button>
                <button class="btn btn-danger" id="btn-reject-${task.task_id}">✕ 拒绝</button>
            </div>
        </div>`;
        bar.style.display = 'block';

        document.getElementById(`btn-approve-${task.task_id}`)?.addEventListener('click',
            () => approveTask(task.task_id));
        document.getElementById(`btn-reject-${task.task_id}`)?.addEventListener('click',
            () => rejectTask(task.task_id));
    } else if (['completed', 'failed', 'cancelled'].includes(task.status)) {
        bar.style.display = 'none';
    }
}

function updateRunningBar() {
    const runningBar = document.getElementById('runningBar');
    const runningText = runningBar?.querySelector('.running-text');
    if (!runningBar || !runningText) return;

    if (!currentTaskId || lastTaskStatus !== 'running') {
        runningBar.style.display = 'none';
        return;
    }

    const elapsed = Date.now() - lastActivityTime;
    if (elapsed > 30000) {
        runningText.textContent = '执行时间较长，请耐心等待...';
    } else {
        runningText.textContent = 'Agent 正在执行...';
    }
    runningBar.style.display = 'flex';
}

/* ---- 审批 ---- */
async function approveTask(taskId) {
    if (isApproving) return;
    isApproving = true;
    updateApprovalButtons();
    try {
        const res = await api(`/tasks/${taskId}/approve`, { method: 'POST' });
        const msgs = {
            'approved_and_resumed': '已批准，任务继续执行',
            'approved': '已批准，正在后台处理...',
            'already_processed': '该任务已被处理',
        };
        showToast(msgs[res.status] || '操作成功', 'success');
    } catch (err) {
        showToast('批准失败: ' + err.message, 'error');
    } finally {
        isApproving = false;
        updateApprovalButtons();
    }
}

async function rejectTask(taskId) {
    if (isApproving) return;
    if (!confirm('确定要拒绝此任务吗？')) return;
    isApproving = true;
    updateApprovalButtons();
    try {
        await api(`/tasks/${taskId}/reject`, { method: 'POST' });
        showToast('已拒绝', 'info');
    } catch (err) {
        showToast('拒绝失败: ' + err.message, 'error');
    } finally {
        isApproving = false;
        updateApprovalButtons();
    }
}

async function deleteTask(taskId) {
    if (isApproving) return;
    if (!confirm('确定要删除该任务吗？此操作不可恢复。')) return;
    isApproving = true;
    updateApprovalButtons();
    try {
        await api(`/tasks/${taskId}`, { method: 'DELETE' });
        showToast('任务已删除', 'success');
        if (currentTaskId === taskId) {
            resetChat();
        }
        loadRecentConversations();
        loadTaskHistory();
    } catch (err) {
        showToast('删除失败: ' + err.message, 'error');
    } finally {
        isApproving = false;
        updateApprovalButtons();
    }
}

function updateApprovalButtons() {
    if (!currentTaskId) return;
    const approveBtn = document.getElementById(`btn-approve-${currentTaskId}`);
    const rejectBtn = document.getElementById(`btn-reject-${currentTaskId}`);
    [approveBtn, rejectBtn].forEach(btn => {
        if (!btn) return;
        btn.disabled = isApproving;
        if (isApproving) {
            btn.innerHTML = '<span class="btn-icon">⏳</span> 处理中...';
        } else {
            const isApprove = btn.id.includes('approve');
            btn.innerHTML = isApprove ? '✓ 批准' : '✕ 拒绝';
        }
    });
}

/* ---- 用户消息（本地追加） ---- */
function addUserMessage(text) {
    const stream = document.getElementById('chatStream');
    const empty = document.getElementById('chatEmpty');
    if (empty) empty.style.display = 'none';
    const div = document.createElement('div');
    div.className = 'message user-message';
    div.innerHTML = `
    <div class="message-avatar">U</div>
    <div class="message-content">
        <div class="message-header">
            <span class="message-role">你</span>
            <span class="message-time">${new Date().toLocaleTimeString('zh-CN')}</span>
        </div>
        <div class="message-body">${escapeHtml(text)}</div>
    </div>`;
    stream.appendChild(div);
    scrollToBottom();
}

function addSystemMessage(text) {
    const stream = document.getElementById('chatStream');
    const div = document.createElement('div');
    div.className = 'system-message';
    div.textContent = text;
    stream.appendChild(div);
    scrollToBottom();
}

/* ---- 滚动 ---- */
function scrollToBottom() {
    const container = document.getElementById('chatContainer');
    if (!container) return;
    requestAnimationFrame(() => {
        container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
    });
}

/* ---- 历史任务 ---- */
async function loadTaskHistory() {
    try {
        const tasks = await api('/tasks?limit=20');
        const list = document.getElementById('historyList');
        if (!tasks.length) {
            list.innerHTML = `<div class="empty-state-card">
                <div class="empty-icon">📭</div><h3>暂无历史任务</h3>
                <p>前往对话页开始你的第一个任务</p></div>`;
            return;
        }
        list.innerHTML = tasks.map(t => `
        <div class="history-item" onclick="loadTaskDetail('${t.task_id}')">
            <button class="delete-btn" onclick="event.stopPropagation(); deleteTask('${t.task_id}')">✕</button>
            <div class="history-item-header">
                <span class="history-item-title">${escapeHtml(t.user_input)}</span>
                <span class="status-badge status-${t.status}">
                    <span class="status-dot"></span>${getStatusText(t.status)}
                </span>
            </div>
            <div class="history-item-meta">
                <span class="task-id">#${t.task_id.slice(0, 8)}</span>
                <span>${new Date(t.updated_at).toLocaleString('zh-CN')}</span>
            </div>
        </div>`).join('');
    } catch (err) {
        console.error('Load history error:', err);
    }
}

async function loadTaskDetail(taskId) {
    switchTab('tasks');
    currentTaskId = taskId;
    lastTaskStatus = null;
    knownEventIds.clear();
    knownMessageIds.clear();
    isApproving = false;

    stopPolling();

    const stream = document.getElementById('chatStream');
    stream.innerHTML = '';
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.id = 'chatEmpty';
    empty.innerHTML = `
    <div class="empty-logo">
        <svg viewBox="0 0 24 24" width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="color: var(--text-primary);">
            <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/>
        </svg>
    </div>
    <h2 class="empty-title">通用智能体框架</h2>
    <p class="empty-subtitle">我可以帮你完成各种任务，开工吧。</p>`;
    stream.appendChild(empty);

    // 高亮当前侧边栏项
    renderRecentConversations();

    try {
        const task = await api(`/tasks/${taskId}`);
        const messages = await api(`/tasks/${taskId}/messages`);
        const events = await api(`/tasks/${taskId}/events`);

        stream.innerHTML = '';
        const newEmpty = document.createElement('div');
        newEmpty.className = 'empty-state';
        newEmpty.id = 'chatEmpty';
        newEmpty.style.display = 'none';
        stream.appendChild(newEmpty);

        knownEventIds.clear();
        knownMessageIds.clear();
        events.forEach(ev => {
            const evId = `${ev.event_type}-${ev.created_at}`;
            knownEventIds.add(evId);
        });
        messages.forEach(msg => {
            const msgId =
                `${msg.role}-${msg.created_at}-${msg.content}-${msg.extra?.tool_call_id || ''}`;
            knownMessageIds.add(msgId);
        });

        appendNewEvents(events);
        appendNewMessages(messages);
        updateApprovalBar(task);
        startPolling(taskId);
        linkifyArtifacts(document.getElementById('chatStream'), taskId);
        scrollToBottom();
    } catch (err) {
        stream.innerHTML = '';
        const errEmpty = document.createElement('div');
        errEmpty.className = 'empty-state';
        errEmpty.id = 'chatEmpty';
        errEmpty.innerHTML = `
        <div class="empty-logo">⚠️</div>
        <h2 class="empty-title">加载失败</h2>
        <p class="empty-subtitle">${escapeHtml(err.message)}</p>`;
        stream.appendChild(errEmpty);
    }
}

/* ---- 状态文本 ---- */
function getStatusText(status) {
    const map = {
        completed: '已完成',
        running: '运行中',
        waiting_approval: '待审批',
        failed: '失败',
        cancelled: '已取消',
    };
    return map[status] || status;
}

/* ---- Skills ---- */
async function loadSkills() {
    try {
        const { skills } = await api('/skills');
        const list = document.getElementById('skillsList');
        if (!skills || !skills.length) {
            list.innerHTML =
                `<div class="empty-state-card"><div class="empty-icon">📚</div><h3>暂无 Skills</h3><p>尚未加载任何技能卡片</p></div>`;
            return;
        }
        const icons = ['📖', '⚙️', '🛠️', '📋', '🔧', '💡'];
        list.innerHTML = skills.map((s, i) => `
        <div class="skill-card">
            <div class="skill-card-icon">${icons[i % icons.length]}</div>
            <div class="skill-name">${escapeHtml(s.name)}</div>
            <div class="skill-desc">${escapeHtml(s.description || '')}</div>
            <span class="skill-path">${escapeHtml(s.path || '')}</span>
        </div>`).join('');
    } catch (err) {
        console.error('Load skills error:', err);
    }
}

/* ---- 记忆搜索 ---- */
async function searchMemory() {
    const query = document.getElementById('memoryQuery').value.trim();
    if (!query) return;
    const resultEl = document.getElementById('memoryResult');
    resultEl.innerHTML =
        '<div class="empty-state-card" style="padding:24px"><p style="color:var(--text-disabled)">搜索中...</p></div>';

    try {
        const res = await api('/memory/search', { method: 'POST', body: { query } });
        if (!res.results || !res.results.length) {
            resultEl.innerHTML =
                '<p style="color:var(--text-disabled);text-align:center;padding:32px">未找到相关记录</p>';
            return;
        }
        resultEl.innerHTML = `
        <p style="color:var(--text-secondary);margin-bottom:12px;font-size:13px">
            找到 <strong>${res.count}</strong> 条结果：</p>
        <pre>${escapeHtml(JSON.stringify(res.results, null, 2))}</pre>`;
    } catch (err) {
        showToast('搜索失败: ' + err.message, 'error');
    }
}

/* ---- Review ---- */
async function loadReviews() {
    try {
        const { reviews } = await api('/reviews');
        const list = document.getElementById('reviewList');
        if (!reviews || !reviews.length) {
            list.innerHTML =
                `<div class="empty-state-card"><div class="empty-icon">✅</div><h3>暂无 Review</h3><p>所有任务已处理完毕</p></div>`;
            return;
        }
        list.innerHTML = reviews.map(r => `
        <div class="review-item">
            <div class="review-info">
                <div class="review-id">#${r.review_id}</div>
                <div class="review-meta">${r.type} · ${r.status} · ${r.reason || '无'}</div>
            </div>
            <div class="review-actions">
                <button class="btn btn-success btn-sm" onclick="applyReview('${r.review_id}')">✓ 批准</button>
                <button class="btn btn-danger btn-sm" onclick="rejectReview('${r.review_id}')">✕ 拒绝</button>
            </div>
        </div>`).join('');
    } catch (err) {
        console.error('Load reviews error:', err);
    }
}

async function applyReview(id) {
    try {
        await api(`/reviews/${id}/apply`, { method: 'POST' });
        showToast('Review 已批准', 'success');
        loadReviews();
    } catch (_) { showToast('操作失败', 'error'); }
}

async function rejectReview(id) {
    try {
        await api(`/reviews/${id}/reject`, { method: 'POST' });
        showToast('Review 已拒绝', 'info');
        loadReviews();
    } catch (_) { showToast('操作失败', 'error'); }
}

/* ---- 工具函数 ---- */
function escapeHtml(str) {
    if (typeof str !== 'string') str = JSON.stringify(str);
    const map = {
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    };
    return str.replace(/[&<>"']/g, c => map[c]);
}
