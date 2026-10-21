const vscode = acquireVsCodeApi();

const agentSelect = document.getElementById('agent-select');
const sessionSelect = document.getElementById('session-select');
const chatContainer = document.getElementById('chat-container');
const messageInput = document.getElementById('message-input');
const sendButton = document.getElementById('send-button');
const protocolStatus = document.getElementById('protocol-status');
const mentionMenu = document.getElementById('mention-menu');

let currentAgent = agentSelect.value;
let currentSession = '';
let currentAssistantMessageDiv = null;
let currentAssistantMessageContent = '';

// --- Mention System State ---
let allAgents = [];
let workspaceFiles = [];
const virtualMentions = [
    { id: 'problems', name: 'problems', icon: '⚠️', type: 'panel', detail: '目前工作區的錯誤與警告' },
    { id: 'terminal', name: 'terminal', icon: '💻', type: 'panel', detail: '最後一次終端機輸出' },
    { id: 'git', name: 'git', icon: '🌿', type: 'panel', detail: '目前的 Git Diff 摘要' },
    { id: 'opened', name: 'opened', icon: '📖', type: 'panel', detail: '所有開啟中的檔案清單' }
];
let mentionFilter = '';
let selectedMentionIndex = 0;
let isMentionMenuOpen = false;

function createTextElement(tagName, className, text) {
    const element = document.createElement(tagName);
    element.className = className;
    element.textContent = text == null ? '' : String(text);
    return element;
}

function normalizeMessageRole(role) {
    return role === 'user' ? 'user' : 'assistant';
}

function scrollToBottom() {
    chatContainer.scrollTop = chatContainer.scrollHeight;
}

function addMessageToChat(role, content) {
    const div = document.createElement('div');
    div.className = `message ${normalizeMessageRole(role)}`;
    div.textContent = content == null ? '' : String(content);
    chatContainer.appendChild(div);
    scrollToBottom();
    return div;
}

function addSystemNotice(text) {
    const div = document.createElement('div');
    div.className = 'system-notice';
    div.textContent = text == null ? '' : String(text);
    chatContainer.appendChild(div);
    scrollToBottom();
}

// --- Plan Management ---
let activePlanCard = null;

function createPlanUI(title, steps) {
    const card = document.createElement('div');
    card.className = 'plan-card';
    const planSteps = Array.isArray(steps) ? steps : [];
    const header = document.createElement('div');
    header.className = 'plan-header';
    header.appendChild(createTextElement('span', 'plan-title', title));

    const cancelButton = createTextElement('button', 'plan-cancel-btn', '🛑 終止');
    cancelButton.id = 'cancel-task-btn';
    cancelButton.type = 'button';
    header.appendChild(cancelButton);
    card.appendChild(header);

    const progressContainer = document.createElement('div');
    progressContainer.className = 'progress-container';
    const progressBar = document.createElement('progress');
    progressBar.className = 'progress-bar';
    progressBar.id = 'plan-progress';
    progressBar.max = Math.max(planSteps.length, 1);
    progressBar.value = 0;
    progressContainer.appendChild(progressBar);
    card.appendChild(progressContainer);

    const stepList = document.createElement('div');
    stepList.className = 'step-list';
    stepList.id = 'plan-steps';
    planSteps.forEach((step, index) => {
        const stepItem = document.createElement('div');
        stepItem.className = 'step-item';
        stepItem.id = `step-${index}`;
        stepItem.appendChild(createTextElement('span', 'step-icon', '⏳'));
        stepItem.appendChild(createTextElement('span', 'step-text', step));
        stepList.appendChild(stepItem);
    });
    card.appendChild(stepList);
    
    chatContainer.appendChild(card);
    scrollToBottom();
    
    activePlanCard = {
        element: card,
        steps: planSteps,
        total: planSteps.length,
        completed: 0
    };

    cancelButton.addEventListener('click', () => {
        vscode.postMessage({ type: 'cancel_task', session_id: currentSession });
        addSystemNotice('⚠️ 使用者已發送終止指令。');
    });
}

function updatePlanStep(index, status, details) {
    if (!activePlanCard) return;
    const stepEl = document.getElementById(`step-${index}`);
    if (!stepEl) return;
    const iconEl = stepEl.querySelector('.step-icon');
    stepEl.classList.remove('active', 'completed');
    iconEl.classList.remove('spinning');

    switch (status) {
        case 'in_progress':
            stepEl.classList.add('active');
            iconEl.textContent = '🔄';
            iconEl.classList.add('spinning');
            break;
        case 'completed':
            stepEl.classList.add('completed');
            iconEl.textContent = '✅';
            break;
        case 'failed':
            iconEl.textContent = '❌';
            break;
        case 'todo':
            iconEl.textContent = '⏳';
            break;
    }

    if (details) {
        let detailsEl = stepEl.querySelector('.step-details');
        if (!detailsEl) {
            detailsEl = document.createElement('div');
            detailsEl.className = 'step-details';
            stepEl.appendChild(detailsEl);
        }
        detailsEl.textContent = details;
    }

    const completedCount = Array.from(document.querySelectorAll('.step-item.completed')).length;
    const bar = document.getElementById('plan-progress');
    if (bar) bar.value = completedCount;
}

// --- Mention Menu Logic ---

function updateMentionMenu() {
    const filter = mentionFilter.toLowerCase();
    
    // Combine all sources
    const items = [
        ...allAgents.map(a => ({
            id: String(a.id ?? ''),
            name: String(a.name ?? a.id ?? ''),
            icon: '👤',
            type: 'agent'
        })),
        ...virtualMentions,
        ...workspaceFiles.map(f => ({
            id: String(f),
            name: String(f),
            icon: '📄',
            type: 'file'
        }))
    ].filter(item => item.name.toLowerCase().includes(filter));

    if (items.length === 0) {
        closeMentionMenu();
        return;
    }

    mentionMenu.replaceChildren();
    items.forEach((item, index) => {
        const mentionItem = document.createElement('div');
        mentionItem.className = `mention-item${index === selectedMentionIndex ? ' selected' : ''}`;
        mentionItem.dataset.index = String(index);
        mentionItem.appendChild(createTextElement('span', 'icon', item.icon));
        mentionItem.appendChild(createTextElement('span', 'name', item.name));
        mentionItem.appendChild(createTextElement('span', 'type-label', item.type));
        mentionItem.addEventListener('click', () => applyMention(item));
        mentionMenu.appendChild(mentionItem);
    });
    
    mentionMenu.classList.add('is-open');
    isMentionMenuOpen = true;

}

function applyMention(item) {
    const text = messageInput.value;
    const cursorPos = messageInput.selectionStart;
    const textBefore = text.substring(0, cursorPos);
    const textAfter = text.substring(cursorPos);
    
    const atIndex = textBefore.lastIndexOf('@');
    if (atIndex === -1) return;

    const replacement = item.type === 'agent' ? `@${item.id} ` : `@${item.id} `;
    const newText = textBefore.substring(0, atIndex) + replacement + textAfter;
    
    messageInput.value = newText;
    const newCursorPos = atIndex + replacement.length;
    messageInput.setSelectionRange(newCursorPos, newCursorPos);
    
    closeMentionMenu();
    messageInput.focus();
}

function closeMentionMenu() {
    mentionMenu.classList.remove('is-open');
    isMentionMenuOpen = false;
    selectedMentionIndex = 0;
}

// 監聽 Input 觸發 @
messageInput.addEventListener('input', (e) => {
    const text = messageInput.value;
    const cursorPos = messageInput.selectionStart;
    const textBefore = text.substring(0, cursorPos);
    
    const match = textBefore.match(/@(\w*)$/);
    if (match) {
        mentionFilter = match[1];
        updateMentionMenu();
    } else {
        closeMentionMenu();
    }

    const lineCount = messageInput.value.split(/\r?\n/).length;
    messageInput.rows = Math.min(5, Math.max(1, lineCount));
});

// 監聽鍵盤導覽
messageInput.addEventListener('keydown', (e) => {
    if (isMentionMenuOpen) {
        const items = mentionMenu.querySelectorAll('.mention-item');
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            selectedMentionIndex = (selectedMentionIndex + 1) % items.length;
            updateMentionMenu();
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            selectedMentionIndex = (selectedMentionIndex - 1 + items.length) % items.length;
            updateMentionMenu();
        } else if (e.key === 'Enter' || e.key === 'Tab') {
            e.preventDefault();
            const selected = items[selectedMentionIndex];
            if (selected) selected.click();
        } else if (e.key === 'Escape') {
            closeMentionMenu();
        }
        return;
    }

    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
    }
});

// 發送訊息
function sendMessage() {
    const text = messageInput.value.trim();
    if (!text) return;

    addMessageToChat('user', text);
    
    vscode.postMessage({
        type: 'chat',
        content: text,
        model: currentAgent,
        session_id: currentSession
    });

    messageInput.value = '';
    messageInput.rows = 1;
    currentAssistantMessageDiv = null;
    currentAssistantMessageContent = '';
    closeMentionMenu();
}

// 監聽 Agent 切換
agentSelect.addEventListener('change', (e) => {
    currentAgent = e.target.value;
    vscode.postMessage({ type: 'fetch_sessions', agent: currentAgent });
});

// 監聽 Session 切換
sessionSelect.addEventListener('change', (e) => {
    currentSession = e.target.value;
    if (currentSession) {
        vscode.postMessage({ type: 'load_session', session_id: currentSession });
    } else {
        chatContainer.replaceChildren();
        addSystemNotice('已開啟新對話。');
    }
});

sendButton.addEventListener('click', sendMessage);

// 接收 Extension 的訊息
window.addEventListener('message', event => {
    const message = event.data;

    switch (message.type) {
        case 'agents_data':
            allAgents = message.agents;
            const previousAgent = currentAgent;
            agentSelect.replaceChildren();
            message.agents.forEach(a => {
                const opt = document.createElement('option');
                opt.value = a.id;
                opt.textContent = a.name;
                agentSelect.appendChild(opt);
            });
            if (Array.from(agentSelect.options).some(opt => opt.value === previousAgent)) {
                agentSelect.value = previousAgent;
            } else if (agentSelect.options.length > 0) {
                currentAgent = agentSelect.value;
            }
            if (currentAgent) {
                vscode.postMessage({ type: 'fetch_sessions', agent: currentAgent });
            }
            break;

        case 'workspace_files':
            workspaceFiles = message.files;
            break;

        case 'sessions_data':
            sessionSelect.replaceChildren();
            const newOpt = document.createElement('option');
            newOpt.value = '';
            newOpt.textContent = '-- 建立新對話 --';
            sessionSelect.appendChild(newOpt);

            message.sessions.forEach(s => {
                const opt = document.createElement('option');
                opt.value = s.id;
                opt.textContent = `${s.is_home ? '🏠 ' : ''}${s.title || '未命名'} (${new Date(s.updatedAt).toLocaleDateString()})`;
                sessionSelect.appendChild(opt);
            });

            if (Array.from(sessionSelect.options).some(opt => opt.value === currentSession)) {
                sessionSelect.value = currentSession;
            } else {
                currentSession = '';
                chatContainer.replaceChildren();
                addSystemNotice('請開始對話或選擇歷史紀錄。');
            }
            break;

        case 'session_history':
            chatContainer.replaceChildren();
            if (message.messages) {
                let msgsArray = [];
                if (message.messages.messages && message.messages.currentId) {
                    const tree = message.messages.messages;
                    let curr = message.messages.currentId;
                    const path = [];
                    while (curr && tree[curr]) {
                        path.unshift(tree[curr]);
                        curr = tree[curr].parentId;
                    }
                    msgsArray = path;
                } else if (Array.isArray(message.messages)) {
                    msgsArray = message.messages;
                }

                msgsArray.forEach(msg => {
                    addMessageToChat(msg.role, msg.content);
                });
                addSystemNotice('已載入歷史對話。');
            }
            break;

        case 'stream':
            if (!currentAssistantMessageDiv) {
                currentAssistantMessageDiv = document.createElement('div');
                currentAssistantMessageDiv.className = 'message assistant';
                chatContainer.appendChild(currentAssistantMessageDiv);
            }
            currentAssistantMessageContent += message.content;
            currentAssistantMessageDiv.textContent = currentAssistantMessageContent;
            scrollToBottom();
            break;

        case 'stream_done':
            currentAssistantMessageDiv = null;
            currentAssistantMessageContent = '';
            break;

        case 'session_created':
            currentSession = message.session_id;
            vscode.postMessage({ type: 'fetch_sessions', agent: currentAgent });
            break;

        case 'error':
            addMessageToChat('assistant', `❌ 錯誤：${message.content}`);
            break;
            
        case 'prefill':
            messageInput.value = message.content;
            messageInput.focus();
            break;

        case 'config_updated':
            vscode.postMessage({ type: 'fetch_agents' });
            break;

        case 'plan_created':
            createPlanUI(message.title, message.steps);
            break;

        case 'plan_step_update':
            updatePlanStep(message.stepIndex, message.status, message.details);
            break;

        case 'auth_change':
            addSystemNotice(`🛡️ 使用者已將授權等級變更為 ${message.level}`);
            break;
    }
});

// 初始化
vscode.postMessage({ type: 'fetch_agents' });
vscode.postMessage({ type: 'fetch_files' });
