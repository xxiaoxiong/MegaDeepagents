// ============================================
// Multi-Agent Team Dashboard — Vue 3 single-file app
//
// 加载顺序：先由 index.html 同步加载 Vue 3 global build，本文件作为 module 运行。
// 数据源：
//   - REST: GET /api/team-tasks          列表（无此端点时降级为创建后写入 store）
//   - REST: GET /api/team-tasks/{id}/state  状态快照
//   - REST: GET /api/team-tasks/{id}/rounds  轮次历史
//   - SSE:  GET /api/team-tasks/{id}/events  实时事件流
//   - REST: POST /api/team-tasks           创建任务
// ============================================

const { createApp, ref, reactive, computed, onMounted, onUnmounted, watch } = Vue;

// ---------- 中心 store 与 fetch ----------
const state = reactive({
  rooms: [],          // [{task_id, room_id, goal, status, team_name}]
  currentRoomId: null,
  teamSpecs: [],      // [{name, description}]
  connection: 'disconnected',  // connected | disconnected | connecting
  selectedRoomDetail: null, // team state object
  liveRounds: {},     // {room_id: {[round]: {speaker, actions, messages}}
});

async function fetchRooms() {
  try {
    const resp = await fetch('/team-tasks?limit=50');
    if (resp.ok) {
      const data = await resp.json();
      state.rooms = Array.isArray(data) ? data : data.items || [];
    }
  } catch (e) {
    console.warn('fetchRooms failed', e);
  }
}

async function fetchTeamSpecs() {
  try {
    const resp = await fetch('/teams');
    if (resp.ok) state.teamSpecs = await resp.json();
  } catch (e) {
    state.teamSpecs = [
      { name: 'software_dev_team', description: '软件开发团队' },
    ];
  }
}

async function fetchRoomDetail(roomId) {
  try {
    const resp = await fetch(`/team-tasks/${roomId}/state`);
    if (resp.ok) state.selectedRoomDetail = await resp.json();
  } catch (e) {
    state.selectedRoomDetail = null;
  }
}

async function fetchRoomRounds(roomId) {
  try {
    const resp = await fetch(`/team-tasks/${roomId}/rounds`);
    if (resp.ok) {
      // 把 list 注入 liveRounds[roomId]
      const rounds = await resp.json();
      const map = {};
      for (const r of rounds) {
        map[r.round_number] = {
          round: r.round_number,
          speaker: r.selected_speaker,
          actions: r.action_summary || '',
          messages: r.message_ids || [],
          termination: r.termination_reason,
          parallel: false,
        };
      }
      state.liveRounds[roomId] = map;
    }
  } catch (e) {
    console.warn('fetchRoomRounds failed', e);
  }
}

// ---------- SSE ----------
let currentEventSource = null;
function openEventStream(roomId) {
  if (currentEventSource) {
    currentEventSource.close();
    currentEventSource = null;
  }
  if (!roomId) return;

  state.connection = 'connecting';

  const es = new EventSource(`/team-tasks/${roomId}/events`);
  currentEventSource = es;

  es.addEventListener('speaker_selected', (e) => {
    const payload = JSON.parse(e.data || '{}');
    ensureRound(roomId, payload.round, payload.agent);
    state.liveRounds[roomId][payload.round].parallelAgents ??= [payload.agent];
  });

  es.addEventListener('parallel_dispatch', (e) => {
    const payload = JSON.parse(e.data || '{}');
    ensureRound(roomId, payload.round, payload.primary);
    state.liveRounds[roomId][payload.round].parallelAgents = payload.agents || [];
    state.liveRounds[roomId][payload.round].parallel = true;
  });

  es.addEventListener('actions_emitted', (e) => {
    const payload = JSON.parse(e.data || '{}');
    const r = state.liveRounds[roomId]?.[payload.round];
    if (r) {
      r.actions = (payload.action_types || [])
        .map((t, i) => t + (payload.action_count ? '' : ''))
        .join(', ');
      r.actionCount = payload.action_count || 0;
    }
  });

  es.addEventListener('message_published', (e) => {
    const payload = JSON.parse(e.data || '{}');
    const r = state.liveRounds[roomId]?.[payload.round];
    if (r) {
      r.messages ??= [];
      r.messages.push({
        from: payload.from_agent,
        to: payload.to_agent,
        type: payload.message_type,
        content: payload.content_preview,
        parallel: !!payload.parallel,
      });
    }
  });

  es.addEventListener('termination', (e) => {
    const payload = JSON.parse(e.data || '{}');
    const r = state.liveRounds[roomId]?.[payload.round];
    if (r) r.termination = payload.reason;
  });

  es.addEventListener('task_terminated', (e) => {
    const payload = JSON.parse(e.data || '{}');
    const target = state.rooms.find((r) => r.room_id === roomId);
    if (target) target.status = payload.status || 'completed';
  });

  es.addEventListener('open', () => {
    state.connection = 'connected';
  });

  es.addEventListener('error', () => {
    state.connection = 'disconnected';
  });
}

function ensureRound(roomId, round, primaryAgent) {
  state.liveRounds[roomId] ??= {};
  state.liveRounds[roomId][round] ??= {
    round,
    speaker: primaryAgent,
    parallelAgents: [primaryAgent].filter(Boolean),
    actions: '',
    messages: [],
    parallel: false,
  };
}

// ---------- 创建新任务 ----------
async function createTask(goal, teamName, maxRounds, reviewRequired) {
  try {
    const resp = await fetch('/team-tasks', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        goal,
        team: teamName || 'software_dev_team',
        max_rounds: maxRounds || 20,
        review_required: reviewRequired !== false,
      }),
    });
    if (resp.ok) {
      const data = await resp.json();
      // 直接选中新房间
      state.currentRoomId = data.room_id || data.task_id;
      // 给 rooms 列表补一条占位（直到 fetch 刷新到正式记录）
      state.rooms.unshift({
        task_id: data.task_id,
        room_id: data.room_id,
        goal,
        status: 'running',
        team_name: teamName || 'software_dev_team',
        current_round: 0,
        max_rounds: maxRounds || 20,
      });
      setTimeout(() => fetchRoomDetail(state.currentRoomId), 500);
    }
  } catch (e) {
    console.error('createTask failed', e);
    alert('创建任务失败：' + e.message);
  }
}

// ---------- Vue 组件 ----------
const vm = createApp({
  setup() {
    const newGoal = ref('');
    const newTeam = ref('software_dev_team');
    const newMaxRounds = ref(20);
    const searchKey = ref('');

    const currentRoom = computed(() =>
      state.rooms.find((r) => r.room_id === state.currentRoomId) || null
    );

    const currentRounds = computed(() => {
      const map = state.liveRounds[state.currentRoomId] || {};
      return Object.values(map).sort((a, b) => a.round - b.round);
    });

    const agents = computed(() => state.selectedRoomDetail?.agents || []);

    const filteredRooms = computed(() => {
      const q = searchKey.value.toLowerCase().trim();
      if (!q) return state.rooms;
      return state.rooms.filter((r) => (r.goal || '').toLowerCase().includes(q));
    });

    function selectRoom(room) {
      state.currentRoomId = room.room_id || room.task_id;
    }

    function avatarColor(name) {
      const palette = ['#3b82f6', '#22c55e', '#eab308', '#a855f7', '#06b6d4', '#ef4444'];
      let hash = 0;
      for (const ch of (name || '?')) {
        hash = ((hash << 5) - hash) + ch.charCodeAt(0);
        hash |= 0;
      }
      return palette[Math.abs(hash) % palette.length];
    }

    function avatarChar(name) {
      return (name || '?')[0].toUpperCase();
    }

    function submitTask() {
      if (!newGoal.value.trim()) return;
      createTask(newGoal.value.trim(), newTeam.value, newMaxRounds.value, true);
      newGoal.value = '';
    }

    watch(() => state.currentRoomId, (newId) => {
      if (newId) {
        fetchRoomDetail(newId);
        fetchRoomRounds(newId);
        openEventStream(newId);
      }
    });

    let pollTimer = null;
    onMounted(async () => {
      await fetchTeamSpecs();
      await fetchRooms();
      pollTimer = setInterval(fetchRooms, 3000);
    });
    onUnmounted(() => {
      if (pollTimer) clearInterval(pollTimer);
      if (currentEventSource) currentEventSource.close();
    });

    return {
      state,
      currentRoom,
      currentRounds,
      agents,
      filteredRooms,
      newGoal,
      newTeam,
      newMaxRounds,
      searchKey,
      selectRoom,
      avatarColor,
      avatarChar,
      submitTask,
    };
  },

  template: `
    <header class="app-header">
      <h1>🤖 Multi-Agent 团队控制台</h1>
      <div class="header-actions">
        <span class="status-badge" :class="state.connection">
          {{ state.connection === 'connected' ? '● 已连接' : state.connection === 'connecting' ? '○ 连接中' : '○ 断开' }}
        </span>
      </div>
    </header>

    <div class="app-main">
      <aside class="sidebar">
        <div class="sidebar-header">
          <h2>任务列表</h2>
          <input v-model="searchKey" placeholder="搜索..." />
        </div>
        <div class="room-list">
          <div
            v-for="r in filteredRooms"
            :key="r.room_id || r.task_id"
            class="room-item"
            :class="{active: (r.room_id || r.task_id) === state.currentRoomId}"
            @click="selectRoom(r)"
          >
            <span class="room-goal">{{ r.goal || '(未设置目标)' }}</span>
            <span class="room-meta">
              <span :class="'status-badge ' + (r.status || 'pending')">{{ r.status || 'pending' }}</span>
              <span v-if="r.current_round != null">R{{ r.current_round }}/{{ r.max_rounds || '?' }}</span>
            </span>
          </div>
          <div v-if="!state.rooms.length" class="empty-state">
            <div class="empty-icon">📋</div>
            <h3>暂无任务</h3>
            <p>从右边创建一个</p>
          </div>
        </div>

        <div class="create-form">
          <h3>新建任务</h3>
          <textarea v-model="newGoal" rows="3" placeholder="任务目标..."></textarea>
          <select v-model="newTeam">
            <option v-for="t in state.teamSpecs" :key="t.name" :value="t.name">{{ t.name }}</option>
          </select>
          <label>最大轮数：{{ newMaxRounds }}</label>
          <input type="range" min="1" max="50" v-model.number="newMaxRounds" />
          <button @click="submitTask">启动</button>
        </div>
      </aside>

      <main class="panel">
        <div v-if="!currentRoom" class="empty-state">
          <div class="empty-icon">👈</div>
          <h3>选择一个任务</h3>
          <p>或创建新任务</p>
        </div>

        <template v-else>
          <div class="room-info">
            <div class="room-info-item">
              <label>目标</label>
              <span>{{ currentRoom.goal }}</span>
            </div>
            <div class="room-info-item">
              <label>团队</label>
              <span>{{ currentRoom.team_name }}</span>
            </div>
            <div class="room-info-item">
              <label>状态</label>
              <span :class="'status-badge ' + (currentRoom.status || 'pending')">{{ currentRoom.status || 'pending' }}</span>
            </div>
            <div class="room-info-item" v-if="state.selectedRoomDetail">
              <label>阶段</label>
              <span>{{ state.selectedRoomDetail.phase }}</span>
            </div>
            <div class="room-info-item" v-if="state.selectedRoomDetail">
              <label>轮数</label>
              <span>{{ state.selectedRoomDetail.current_round }} / {{ state.selectedRoomDetail.max_rounds }}</span>
            </div>
          </div>

          <div class="agent-grid" v-if="agents.length || (state.selectedRoomDetail && state.selectedRoomDetail.agents)">
            <div
              v-for="agent in (state.selectedRoomDetail?.agents || []).map(name => ({name}))"
              :key="agent.name"
              class="agent-card"
              :class="{'active-speaker': currentRounds.length && currentRounds[currentRounds.length-1].parallelAgents?.includes(agent.name)}"
            >
              <div class="agent-avatar" :style="{background: avatarColor(agent.name)}">{{ avatarChar(agent.name) }}</div>
              <div class="agent-name">{{ agent.name }}</div>
            </div>
          </div>

          <div class="round-timeline">
            <div class="timeline-header">
              <h3>轮次时间线</h3>
              <span style="font-size:12px;color:var(--text-muted)">{{ currentRounds.length }} 轮</span>
            </div>
            <div class="timeline-body">
              <div
                v-for="r in currentRounds"
                :key="r.round"
                class="round-card"
                :class="{terminated: r.termination}"
              >
                <div class="round-header">
                  <span>
                    <span class="round-speaker">R{{ r.round }} · {{ r.speaker || '?' }}</span>
                    <span v-if="r.parallel" class="parallel-badge">B2并行 {{ r.parallelAgents?.length }} agents</span>
                  </span>
                  <span v-if="r.termination">⚠ 终止：{{ r.termination }}</span>
                </div>
                <div class="round-actions" v-if="r.actions">⚙ {{ r.actions }}</div>
                <div class="round-messages" v-if="r.messages && r.messages.length">
                  <div v-for="(m, i) in r.messages" :key="i" class="round-message">
                    <span class="msg-meta">
                      <span :style="{color: avatarColor(m.from)}">{{ m.from }}</span>→<span :style="{color: avatarColor(Array.isArray(m.to) ? m.to[0] : m.to)}">{{ m.to || '?' }}</span>
                      <span v-if="m.parallel" class="parallel-badge">par</span>
                    </span>
                    <span class="msg-preview">[{{ m.type }}] {{ m.content }}</span>
                  </div>
                </div>
              </div>
              <div v-if="!currentRounds.length" class="empty-state">
                <p>等待第一轮事件…</p>
              </div>
            </div>
          </div>
        </template>
      </main>
    </div>
  `,
});

vm.mount('#app');
