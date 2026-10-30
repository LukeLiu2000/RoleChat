/* RoleChat 前端逻辑（原生 JS，无依赖）。所有动态内容用 textContent 渲染，防 XSS。 */
"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  characters: [], sessions: [], memories: [],
  charId: null, sessionId: null, messages: [],
  lastRefs: [], lastEvents: [], summary: null, sending: false,
};

/* 语音配置（来自 config.voice；识别与合成都走本地服务） */
const voice = { tts_voice: "female", auto_tts: false, speed: 1.0 };
let currentAudio = null;

async function api(method, path, body) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const res = await fetch(path, opt);
  let data = {};
  try { data = await res.json(); } catch (e) { /* 非 JSON */ }
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

function toast(msg, isErr) {
  const t = $("toast");
  t.textContent = msg;
  t.className = isErr ? "err" : "";
  t.hidden = false;
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.hidden = true; }, 2600);
}

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

/* 任何脚本/请求错误都必须可见，绝不允许“点了没反应”的静默失败 */
window.addEventListener("error", (e) => toast("界面脚本错误：" + e.message, true));
window.addEventListener("unhandledrejection", (e) => {
  const r = e.reason;
  toast("操作失败：" + (r && r.message ? r.message : String(r)), true);
});

/* ---------------- 顶部状态 ---------------- */
async function refreshHealth() {
  try {
    const h = await api("GET", "/api/health");
    const p = await api("POST", "/api/probe");
    const badge = $("providerBadge");
    const label = h.provider === "mock" ? "mock 离线模拟" : `${h.provider} · ${h.model || "?"}`;
    document.querySelector(".brand .sub").textContent = "Luke Liu · 本地角色聊天 v" + h.version;
    badge.textContent = label + (p.connected ? " ●" : " ○ 未连上");
    badge.className = "badge " + (p.connected ? "on" : (h.provider === "mock" ? "on" : "off"));
    badge.title = p.message + "\n数据目录：" + h.data_dir;
  } catch (e) { /* 启动中 */ }
}

/* ---------------- 角色列表 ---------------- */
async function loadCharacters(selectId) {
  const d = await api("GET", "/api/characters");
  state.characters = d.characters;
  const box = $("charList");
  box.textContent = "";
  for (const c of state.characters) {
    const item = el("div", "item" + (c.id === state.charId ? " active" : ""));
    const grow = el("div", "grow");
    grow.appendChild(el("div", "t", c.name));
    grow.appendChild(el("div", "sub t", (c.personality || "").slice(0, 24)));
    item.appendChild(grow);
    const edit = el("button", "iconbtn", "✎");
    edit.title = "编辑角色";
    edit.onclick = (ev) => { ev.stopPropagation(); openCharModal(c); };
    item.appendChild(edit);
    item.onclick = () => selectCharacter(c.id);
    box.appendChild(item);
  }
  if (selectId) selectCharacter(selectId);
}

function selectCharacter(cid) {
  if (state.charId !== cid) { state.charId = cid; state.sessionId = null; state.messages = []; }
  $("btnNewSession").disabled = !cid;
  document.querySelectorAll("#charList .item").forEach((n, i) => {
    n.classList.toggle("active", state.characters[i] && state.characters[i].id === cid);
  });
  loadSessions();
}

/* ---------------- 会话列表 ---------------- */
async function loadSessions(selectId) {
  if (!state.charId) { $("sessionList").textContent = ""; renderChat(); return; }
  const d = await api("GET", "/api/sessions?character_id=" + state.charId);
  state.sessions = d.sessions;
  const box = $("sessionList");
  box.textContent = "";
  for (const s of state.sessions) {
    const item = el("div", "item" + (s.id === state.sessionId ? " active" : ""));
    const grow = el("div", "grow");
    grow.appendChild(el("div", "t", s.title));
    grow.appendChild(el("div", "sub t", `${s.message_count} 条消息 · ${s.updated_at.replace("T", " ")}`));
    item.appendChild(grow);
    const del = el("button", "iconbtn", "🗑");
    del.title = "删除会话";
    del.onclick = async (ev) => {
      ev.stopPropagation();
      if (!confirm("删除会话「" + s.title + "」？消息与摘要会一并删除。")) return;
      await api("DELETE", "/api/sessions/" + s.id);
      if (state.sessionId === s.id) { state.sessionId = null; state.messages = []; }
      loadSessions();
    };
    item.appendChild(del);
    item.onclick = () => selectSession(s.id);
    box.appendChild(item);
  }
  if (selectId) {
    await selectSession(selectId);
  } else {
    renderChat(); // 关键：启动后没有任何会话时也要渲染空状态（含「开始对话」按钮）
  }
}

async function selectSession(sid) {
  state.sessionId = sid;
  const d = await api("GET", "/api/sessions/" + sid);
  state.messages = d.messages;
  state.summary = d.session.summary;
  renderChat();
  renderSessionListActive();
  const lastA = [...d.messages].reverse().find((m) => m.role === "assistant" && m.used_memories);
  if (lastA) {
    state.lastRefs = (lastA.used_memories || []).map((u) => ({
      id: u.id, content: "", scope: "?", score: u.score, matched: [],
      _idOnly: true,
    }));
    // 补全内容：从记忆库查
    await loadMemories();
    for (const r of state.lastRefs) {
      const m = state.memories.find((x) => x.id === r.id);
      if (m) { r.content = m.content; r.scope = m.scope; }
    }
    renderRefs([], null);
  }
}

function renderSessionListActive() {
  document.querySelectorAll("#sessionList .item").forEach((n, i) => {
    n.classList.toggle("active", state.sessions[i] && state.sessions[i].id === state.sessionId);
  });
}

/* ---------------- 聊天区 ---------------- */
function renderChat() {
  const box = $("messages");
  box.textContent = "";
  const char = state.characters.find((c) => c.id === state.charId);
  if (!state.sessionId) {
    $("chatTitle").textContent = char ? char.name : "选择或创建一个角色";
    $("chatSub").textContent = "";
    const hint = el("div", "empty-hint");
    if (char) {
      hint.appendChild(document.createTextNode("和 " + char.name + " 聊聊吧"));
      hint.appendChild(document.createElement("br"));
      const btn = el("button", "primary", "💬 开始对话");
      btn.onclick = () => startNewSession();
      hint.appendChild(btn);
    } else {
      hint.appendChild(document.createTextNode("从左侧选择或创建一个角色"));
    }
    box.appendChild(hint);
    return;
  }
  const sess = state.sessions.find((s) => s.id === state.sessionId);
  $("chatTitle").textContent = char ? char.name + " · " + (sess ? sess.title : "") : "";
  $("chatSub").textContent = state.summary && state.summary.text ? "含早期摘要" : "";
  for (const m of state.messages) {
    if (m.role === "system_event") {
      const w = el("div", "msg system_event");
      w.appendChild(el("div", "bubble", "🧠 " + m.content));
      box.appendChild(w);
      continue;
    }
    const w = el("div", "msg " + (m.role === "user" ? "user" : "assistant"));
    const who = el("div", "who");
    who.appendChild(el("span", null, m.role === "user" ? "我" : (char ? char.name : "角色")));
    if (m.role === "assistant") {
      const chip = el("span", "chip model", "模型生成");
      chip.title = "这是模型当轮输出的文本，不代表已存入长期记忆";
      who.appendChild(chip);
      if (m.model && m.model.provider) {
        const mc = el("span", "chip", m.model.provider === "mock" ? "mock" : m.model.provider + ":" + (m.model.model || ""));
        who.appendChild(mc);
      }
      const spk = el("button", "spk", "🔊");
      spk.title = "朗读这条回复（本地语音合成）";
      spk.onclick = () => speak(m.content, spk);
      who.appendChild(spk);
    }
    w.appendChild(who);
    w.appendChild(el("div", "bubble", m.content));
    box.appendChild(w);
  }
  box.scrollTop = box.scrollHeight;
}

async function startNewSession() {
  if (!state.charId) {
    toast("请先在左侧选择或创建角色", true);
    return;
  }
  try {
    const r = await api("POST", "/api/sessions", { character_id: state.charId });
    await loadSessions(r.session.id);
    $("input").focus();
  } catch (e) {
    toast("创建会话失败：" + e.message, true);
  }
}

async function sendMessage() {
  const input = $("input");
  const text = input.value.trim();
  if (!text || state.sending) return;
  if (!state.sessionId) {
    toast("请先点击「开始对话」或左侧「＋ 新会话」创建会话", true);
    return;
  }
  stopAudio();
  state.sending = true;
  $("btnSend").disabled = true;
  try {
    const r = await api("POST", "/api/chat", { session_id: state.sessionId, message: text });
    input.value = "";
    state.messages = (await api("GET", "/api/sessions/" + state.sessionId)).messages;
    state.summary = r.summary;
    state.lastRefs = r.used_memories || [];
    state.lastEvents = r.memory_events || [];
    renderChat();
    renderRefs(r.memory_events || [], r.model);
    loadSessions();
    loadMemories();
    if (voice.auto_tts && r.reply) speak(r.reply, null);
  } catch (e) {
    toast("发送失败：" + e.message, true);
    state.messages = (await api("GET", "/api/sessions/" + state.sessionId)).messages;
    renderChat();
  } finally {
    state.sending = false;
    $("btnSend").disabled = false;
    input.focus();
  }
}

/* ---------------- 语音对话（Luke Liu · 全本地：SenseVoice 识别 / Kokoro 合成） ---------------- */
function stopAudio() {
  if (currentAudio) {
    try { currentAudio.pause(); } catch (e) { /* 已结束 */ }
    currentAudio = null;
  }
  document.querySelectorAll(".spk.playing").forEach((b) => b.classList.remove("playing"));
}

async function speak(text, btn) {
  if (!text || !text.trim()) return;
  stopAudio();
  if (btn) btn.classList.add("playing");
  try {
    const res = await fetch("/api/voice/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: text.slice(0, 1200), voice: voice.tts_voice, speed: voice.speed }),
    });
    let d = {};
    try { d = await res.clone().json(); } catch (e) { /* 音频体不是 json */ }
    if (!res.ok) throw new Error(d.error || ("HTTP " + res.status));
    const blob = await res.blob();
    currentAudio = new Audio(URL.createObjectURL(blob));
    currentAudio.onended = () => stopAudio();
    await currentAudio.play();
  } catch (e) {
    stopAudio();
    toast("语音合成失败：" + e.message, true);
  }
}

const rec = { on: false, ctx: null, node: null, stream: null, chunks: [] };

function downsampleTo16k(f32, rate) {
  if (rate === 16000) return f32;
  const n = Math.round((f32.length * 16000) / rate);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const pos = (i * rate) / 16000;
    const i0 = Math.floor(pos);
    const i1 = Math.min(i0 + 1, f32.length - 1);
    const t = pos - i0;
    out[i] = f32[i0] * (1 - t) + f32[i1] * t;
  }
  return out;
}

function encodeWav(f32) { /* 16kHz 单声道 16bit PCM */
  const buf = new ArrayBuffer(44 + f32.length * 2);
  const v = new DataView(buf);
  const ws = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
  ws(0, "RIFF"); v.setUint32(4, 36 + f32.length * 2, true); ws(8, "WAVE"); ws(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, 16000, true); v.setUint32(28, 32000, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  ws(36, "data"); v.setUint32(40, f32.length * 2, true);
  let o = 44;
  for (let i = 0; i < f32.length; i++, o += 2) {
    const s = Math.max(-1, Math.min(1, f32[i]));
    v.setInt16(o, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return buf;
}

function teardownRec() {
  try { if (rec.node) rec.node.disconnect(); } catch (e) { /* 忽略 */ }
  try { if (rec.stream) rec.stream.getTracks().forEach((t) => t.stop()); } catch (e) { /* 忽略 */ }
  try { if (rec.ctx) rec.ctx.close(); } catch (e) { /* 忽略 */ }
  rec.ctx = null; rec.node = null; rec.stream = null;
}

async function toggleMic() {
  const btn = $("btnMic");
  if (rec.on) { finishRec(); return; }
  if (!state.sessionId) {
    toast("请先点击「开始对话」创建会话，再使用语音", true);
    return;
  }
  if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia)) {
    toast("当前环境不支持麦克风录音，请用 Chrome / Edge 打开本页（允许 https://jsdom 测试环境除外）", true);
    return;
  }
  try {
    rec.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    rec.ctx = new AudioContext({ sampleRate: 16000 });
    const src = rec.ctx.createMediaStreamSource(rec.stream);
    rec.node = rec.ctx.createScriptProcessor(4096, 1, 1);
    rec.chunks = [];
    rec.node.onaudioprocess = (e) => rec.chunks.push(new Float32Array(e.inputBuffer.getChannelData(0)));
    src.connect(rec.node);
    rec.node.connect(rec.ctx.destination); // 不写 outputBuffer，静音直连只驱动回调
    rec.on = true;
    btn.classList.add("rec");
    btn.title = "录音中…再点一次结束并识别发送";
    toast("录音中：请说话，说完点 🎤 结束");
  } catch (e) {
    teardownRec();
    toast("无法访问麦克风：" + e.message, true);
  }
}

async function finishRec() {
  const btn = $("btnMic");
  rec.on = false;
  btn.classList.remove("rec");
  btn.title = "点击开始录音，再点一次结束并识别发送";
  const rate = rec.ctx ? rec.ctx.sampleRate : 16000;
  let total = 0;
  for (const c of rec.chunks) total += c.length;
  const all = new Float32Array(total);
  let o = 0;
  for (const c of rec.chunks) { all.set(c, o); o += c.length; }
  teardownRec();
  if (total < 1600) { toast("录音太短，请点开 🎤 后多说一会儿", true); return; }
  const pcm = downsampleTo16k(all, rate);
  btn.disabled = true;
  try {
    const res = await fetch("/api/voice/asr", {
      method: "POST",
      headers: { "Content-Type": "audio/wav" },
      body: encodeWav(pcm),
    });
    let d = {};
    try { d = await res.json(); } catch (e) { /* 非 JSON */ }
    if (!res.ok) throw new Error(d.error || ("HTTP " + res.status));
    const text = (d.text || "").trim();
    if (!text) { toast("没听清，请靠近麦克风再试一次", true); return; }
    $("input").value = text;
    toast("识别结果：" + text);
    sendMessage();
  } catch (e) {
    toast("语音识别失败：" + e.message, true);
  } finally {
    btn.disabled = false;
  }
}


/* ---------------- 右侧：本轮引用 ---------------- */
function renderRefs(events, model) {
  const refBox = $("refList");
  const evBox = $("eventList");
  refBox.textContent = ""; evBox.textContent = "";
  const meta = $("lastReplyMeta");
  if (model) {
    meta.textContent = `本轮模型：${model.provider}${model.model ? " · " + model.model : ""}。引用与事件如下（模型回复 ≠ 存储记忆）。`;
  }
  if (!state.lastRefs.length) {
    refBox.appendChild(el("div", "sub", "本轮没有引用长期记忆。"));
  }
  for (const m of state.lastRefs) {
    if (!m.content) continue;
    const card = el("div", "refcard");
    const head = el("div", "head");
    const scopeChip = el("span", "chip " + (m.scope === "user" ? "scope-user" : "scope-character"),
      m.scope === "user" ? "用户记忆" : "角色记忆");
    head.appendChild(scopeChip);
    head.appendChild(el("span", "chip mem", "已存储记忆"));
    card.appendChild(head);
    card.appendChild(el("div", "body", m.content));
    const foot = el("div", "foot");
    if (m.score !== undefined) {
      foot.appendChild(el("span", "matched", "相关分 " + m.score));
      const bar = el("div", "scorebar");
      const inner = el("div");
      inner.style.width = Math.min(100, Math.round(m.score * 60)) + "%";
      bar.appendChild(inner);
      foot.appendChild(bar);
    }
    if (m.matched && m.matched.length) {
      foot.appendChild(el("span", "matched", "命中词：" + m.matched.slice(0, 6).join(" ")));
    }
    card.appendChild(foot);
    refBox.appendChild(card);
  }
  if (events && events.length) {
    for (const ev of events) {
      evBox.appendChild(el("div", "ev", "· " + ev.detail));
    }
  }
}

/* ---------------- 右侧：记忆管理 ---------------- */
async function loadMemories() {
  const scope = $("memFilter").value;
  const d = await api("GET", "/api/memories" + (scope ? "?scope=" + scope : ""));
  state.memories = d.memories;
  renderMemories();
}

function renderMemories() {
  const box = $("memList");
  box.textContent = "";
  if (!state.memories.length) {
    box.appendChild(el("div", "sub", "（暂无长期记忆）"));
    return;
  }
  for (const m of state.memories) {
    const card = el("div", "memcard");
    const head = el("div", "head");
    head.appendChild(el("span", "chip " + (m.scope === "user" ? "scope-user" : "scope-character"),
      m.scope === "user" ? "用户记忆" : "角色记忆"));
    head.appendChild(el("span", "chip", m.confidence));
    card.appendChild(head);
    const body = el("div", "body", m.content);
    card.appendChild(body);
    const foot = el("div", "foot");
    foot.appendChild(el("span", "sub", `更新 ${m.updated_at.replace("T", " ")} · 来源会话 ${m.source_session || "手动"}`));
    const spacer = el("span", "spacer");
    foot.appendChild(spacer);
    const btnEdit = el("button", "iconbtn", "✎");
    btnEdit.title = "编辑记忆";
    btnEdit.onclick = () => {
      const nv = prompt("编辑记忆内容：", m.content);
      if (nv === null) return;
      if (!nv.trim()) { toast("内容不能为空", true); return; }
      api("PUT", "/api/memories/" + m.id, { content: nv }).then(loadMemories).catch((e) => toast(e.message, true));
    };
    const btnDel = el("button", "iconbtn", "🗑");
    btnDel.title = "删除记忆";
    btnDel.onclick = async () => {
      if (!confirm("删除这条记忆？\n" + m.content)) return;
      await api("DELETE", "/api/memories/" + m.id);
      loadMemories();
    };
    foot.appendChild(btnEdit); foot.appendChild(btnDel);
    card.appendChild(foot);
    box.appendChild(card);
  }
}

async function manualExtract() {
  if (!state.sessionId) { toast("先选择一个会话", true); return; }
  const r = await api("POST", "/api/memories/extract", { session_id: state.sessionId });
  const added = r.memory_events.filter((e) => e.kind === "remembered" || e.kind === "merged");
  toast(added.length ? "提取完成：" + added.map((e) => e.detail).join("；") : "本轮没有新的长期记忆");
  loadMemories();
}

/* ---------------- 角色弹窗 ---------------- */
function openCharModal(c) {
  $("charModalTitle").textContent = c ? "编辑角色" : "新建角色";
  $("charId").value = c ? c.id : "";
  $("charName").value = c ? c.name : "";
  $("charBackground").value = c ? c.background : "";
  $("charPersonality").value = c ? c.personality : "";
  $("charSpeech").value = c ? c.speech_style : "";
  $("charRules").value = c ? c.rules : "";
  $("btnCharDelete").hidden = !c;
  $("modalChar").hidden = false;
}

async function saveChar() {
  const data = {
    name: $("charName").value.trim(),
    background: $("charBackground").value.trim(),
    personality: $("charPersonality").value.trim(),
    speech_style: $("charSpeech").value.trim(),
    rules: $("charRules").value.trim(),
  };
  if (!data.name) { toast("名称必填", true); return; }
  const id = $("charId").value;
  try {
    const r = id ? await api("PUT", "/api/characters/" + id, data)
      : await api("POST", "/api/characters", data);
    $("modalChar").hidden = true;
    await loadCharacters();
    selectCharacter(r.character.id);
    toast("已保存角色：" + r.character.name);
  } catch (e) { toast(e.message, true); }
}

async function deleteChar() {
  const id = $("charId").value;
  if (!id) return;
  if (!confirm("删除该角色及其全部会话、角色记忆？")) return;
  await api("DELETE", "/api/characters/" + id);
  $("modalChar").hidden = true;
  state.charId = null; state.sessionId = null; state.messages = [];
  await loadCharacters();
  renderChat();
}

/* ---------------- 设置弹窗 ---------------- */
const PRESETS = {
  mock: { provider: "mock", base_url: "", model: "" },
  ollama: { provider: "ollama", base_url: "http://localhost:11434", model: "qwen2.5:7b" },
  lmstudio: { provider: "openai", base_url: "http://localhost:1234/v1", model: "" },
  anthropic: { provider: "anthropic", base_url: "https://api.anthropic.com", model: "claude-opus-5-5" },
};

async function openSettings() {
  const d = await api("GET", "/api/config");
  const c = d.config, m = c.model;
  $("setProvider").value = m.provider || "mock";
  $("setBase").value = m.base_url || "";
  $("setModel").value = m.model || "";
  $("setKey").value = "";
  $("setKey").placeholder = m.api_key_set ? "已保存（" + (m.api_key_hint || "") + "），留空表示不修改" : "只保存在本机 config.json";
  $("setCtx").value = m.context_chars;
  $("setMaxTok").value = m.max_tokens;
  $("setTimeout").value = m.timeout_sec;
  $("setMemK").value = c.memory.max_memories_in_prompt;
  $("setMemMin").value = c.memory.min_score;
  $("setMemEvery").value = c.memory.auto_extract_every;
  const v = c.voice || {};
  $("setTtsVoice").value = v.tts_voice || "female";
  $("setAutoTts").checked = !!v.auto_tts;
  $("setTtsSpeed").value = v.speed || 1.0;
  $("probeResult").textContent = "";
  $("modalSettings").hidden = false;
}

async function saveSettings(silent) {
  const body = {
    model: {
      provider: $("setProvider").value,
      base_url: $("setBase").value.trim(),
      model: $("setModel").value.trim(),
      context_chars: parseInt($("setCtx").value || "6000", 10),
      max_tokens: parseInt($("setMaxTok").value || "4096", 10),
      timeout_sec: parseInt($("setTimeout").value || "120", 10),
    },
    memory: {
      max_memories_in_prompt: parseInt($("setMemK").value || "5", 10),
      min_score: parseFloat($("setMemMin").value || "0.15"),
      auto_extract_every: parseInt($("setMemEvery").value || "4", 10),
    },
    voice: {
      tts_voice: $("setTtsVoice").value,
      auto_tts: $("setAutoTts").checked,
      speed: parseFloat($("setTtsSpeed").value || "1.0") || 1.0,
    },
  };
  if ($("setKey").value.trim()) body.model.api_key = $("setKey").value.trim();
  try {
    await api("POST", "/api/config", body);
    Object.assign(voice, body.voice);
    if (!silent) {
      $("modalSettings").hidden = true;
      toast("设置已保存（保存在工作区 config.json）");
    }
    refreshHealth();
  } catch (e) { toast(e.message, true); }
}

async function probeFromSettings() {
  $("probeResult").textContent = "测试中…";
  const tmp = {
    provider: $("setProvider").value,
    base_url: $("setBase").value.trim(),
    model: $("setModel").value.trim(),
  };
  // 先保存当前表单（不保存则测的是旧配置），再探测；silent=true 不关闭弹窗
  try {
    await saveSettings(true);
    const r = await api("POST", "/api/probe");
    $("probeResult").textContent = (r.connected ? "✅ " : "❌ ") + r.message +
      (r.models && r.models.length ? "：" + r.models.slice(0, 8).join(", ") : "");
  } catch (e) {
    $("probeResult").textContent = "❌ " + e.message;
  }
}

/* ---------------- 事件绑定与启动 ---------------- */
function bind() {
  $("btnSend").onclick = sendMessage;
  $("btnMic").onclick = toggleMic;
  $("input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) sendMessage();
  });
  $("btnNewChar").onclick = () => openCharModal(null);
  $("btnCharSave").onclick = saveChar;
  $("btnCharCancel").onclick = () => { $("modalChar").hidden = true; };
  $("btnCharDelete").onclick = deleteChar;
  $("btnNewSession").onclick = () => startNewSession();
  $("btnSettings").onclick = openSettings;
  $("btnCfgSave").onclick = () => saveSettings();
  $("btnCfgCancel").onclick = () => { $("modalSettings").hidden = true; };
  $("btnProbe").onclick = probeFromSettings;
  document.querySelectorAll(".presets .mini").forEach((b) => {
    b.onclick = () => {
      const p = PRESETS[b.dataset.preset];
      $("setProvider").value = p.provider;
      $("setBase").value = p.base_url;
      $("setModel").value = p.model;
    };
  });
  $("memFilter").onchange = loadMemories;
  $("btnAddMem").onclick = () => {
    const nv = prompt("手动添加长期记忆（关于真实用户的事实或偏好）：");
    if (!nv || !nv.trim()) return;
    api("POST", "/api/memories", { content: nv.trim(), scope: "user" })
      .then(() => { loadMemories(); toast("已添加"); })
      .catch((e) => toast(e.message, true));
  };
  $("btnExtract").onclick = () => manualExtract().catch((e) => toast(e.message, true));
  $("btnSummary").onclick = () => {
    $("summaryText").textContent = state.summary && state.summary.text
      ? state.summary.text
      : "（当前会话还没有生成早期摘要——消息超出上下文预算后自动压缩。）";
    $("modalSummary").hidden = false;
  };
  $("btnSummaryClose").onclick = () => { $("modalSummary").hidden = true; };
  document.querySelectorAll(".tab").forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      ["refs", "mems", "about"].forEach((k) => { $("tab-" + k).hidden = k !== t.dataset.tab; });
    };
  });
  document.querySelectorAll(".modal").forEach((m) => {
    m.addEventListener("click", (e) => { if (e.target === m) m.hidden = true; });
  });
}

async function loadVoiceCfg() {
  try {
    const d = await api("GET", "/api/config");
    const v = (d.config && d.config.voice) || {};
    voice.tts_voice = v.tts_voice || "female";
    voice.auto_tts = !!v.auto_tts;
    voice.speed = v.speed || 1.0;
  } catch (e) { /* 用默认值 */ }
}

async function boot() {
  try {
    bind();
    loadVoiceCfg();
    await loadCharacters();
    if (state.characters.length) {
      selectCharacter(state.characters[0].id);
    } else {
      toast("还没有角色，点击左侧「＋ 新建」创建一个", true);
    }
    await loadMemories();
    refreshHealth();
  } catch (e) {
    toast("初始化失败：" + e.message, true);
  }
}

boot();
