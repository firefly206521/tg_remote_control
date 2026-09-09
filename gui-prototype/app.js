"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const labels = { running: "运行中", queued: "排队中", idle: "空闲" };
  const query = new URLSearchParams(location.search);
  const urlToken = query.get("token");
  if (urlToken) {
    sessionStorage.setItem("tg-ai-web-token", urlToken);
    history.replaceState(null, "", location.pathname);
  }
  const token = sessionStorage.getItem("tg-ai-web-token") || "";
  let tasks = [];
  let agentAvailability = { codex: true, claude: true };
  let currentId = localStorage.getItem("tg-ai-current-task") || "";
  let detailsOpen = true;
  let polling = false;
  const histories = new Map();
  const cursors = new Map();
  const unread = new Map();
  let drafts = {};
  try { drafts = JSON.parse(localStorage.getItem("tg-ai-drafts") || "{}"); } catch { drafts = {}; }
  const scrollPositions = new Map();
  let toastTimer;

  const current = () => tasks.find(task => task.id === currentId);
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text ?? "");
    return node;
  }
  function toast(text, error = false) {
    $("toast").textContent = text;
    $("toast").style.background = error ? "#7c3f35" : "#273f2c";
    $("toast").hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { $("toast").hidden = true; }, 3500);
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Authorization": `Bearer ${token}`, "Content-Type": "application/json", ...(options.headers || {}) }
    });
    let body = {};
    try { body = await response.json(); } catch { /* use status below */ }
    if (!response.ok) throw new Error(body.error || `请求失败（${response.status}）`);
    return body;
  }
  function saveDraft() {
    if (!currentId) return;
    drafts[currentId] = $("prompt").value;
    localStorage.setItem("tg-ai-drafts", JSON.stringify(drafts));
  }
  function updateAgentOptions() {
    for (const option of $("new-agent").options) {
      const agent = option.value.toLowerCase();
      const available = agentAvailability[agent] !== false;
      option.disabled = !available;
      option.textContent = `${agent === "claude" ? "Claude" : "Codex"}${available ? "" : "（暂不可用）"}`;
    }
  }
  function renderList() {
    const search = $("search").value.trim().toLocaleLowerCase();
    const visible = tasks.filter(task => task.name.toLocaleLowerCase().includes(search));
    $("task-count").textContent = String(tasks.length).padStart(2, "0");
    $("task-list").replaceChildren();
    for (const task of visible) {
      const count = unread.get(task.id) || 0;
      const button = element("button", `task-item${task.id === currentId ? " selected" : ""}`);
      button.type = "button";
      button.title = task.name;
      button.setAttribute("aria-current", task.id === currentId ? "true" : "false");
      button.setAttribute("aria-label", `${task.name}，${labels[task.status] || task.status}${count ? `，${count} 条未读` : ""}`);
      const top = element("div", "task-top");
      top.append(element("span", `status-dot ${task.status}`), element("span", "task-name", task.name));
      if (count) top.append(element("span", "unread", count > 99 ? "99+" : count));
      const meta = element("div", "task-meta");
      meta.append(element("span", "", task.agent), element("span", "", labels[task.status] || task.status));
      button.append(top, meta);
      button.addEventListener("click", () => selectTask(task.id));
      $("task-list").append(button);
    }
    if (!visible.length) $("task-list").append(element("p", "sidebar-empty", "没有匹配的任务\n试试其他名称"));
  }
  async function selectTask(id) {
    if (currentId) {
      saveDraft();
      scrollPositions.set(currentId, $("messages").scrollTop);
    }
    currentId = id;
    localStorage.setItem("tg-ai-current-task", id);
    unread.set(id, 0);
    await fetchMessages(id, false);
    renderList();
    renderTask();
  }
  function renderTask(forceBottom = false) {
    const task = current();
    if (!task) return renderNoTasks();
    $("task-title").textContent = task.name;
    const botName = task.agent === "claude" ? "My_claude" : "My_codex";
    $("task-subtitle").textContent = task.id === window.telegramCurrentByAgent?.[task.agent]
      ? `${botName} 的 Telegram 当前任务 · 出门后可直接继续`
      : `网页独立选择 · 在 ${botName} 发送 /sw ${task.name} 后继续`;
    $("agent-value").textContent = `✳ ${task.agent}`;
    $("directory-value").textContent = task.workdir;
    $("directory-value").title = task.workdir;
    $("status-value").replaceChildren(element("span", `badge ${task.status}`, labels[task.status] || task.status));
    const messages = histories.get(task.id) || [];
    $("message-count").textContent = `(${messages.length})`;
    $("messages").replaceChildren();
    if (!messages.length) {
      const empty = element("div", "empty-conversation");
      empty.append(element("div", "empty-icon", "↗"), element("h2", "", "开始这个任务"), element("p", "", "这里暂时没有升级后的对话记录。\n发送消息后，记录会保存在本机。"));
      $("messages").append(empty);
    } else {
      $("messages").append(element("div", "date-divider", "本机保存的对话"));
      for (const message of messages) {
        const row = element("article", `message ${message.role}`);
        const body = element("div", "message-body");
        const when = new Date(message.created_at * 1000).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
        const byline = element("div", "message-byline", message.role === "user" ? "你" : message.role === "system" ? "任务动态" : task.agent);
        if (message.role === "assistant") byline.append(element("span", "message-tag", "AI 助手"));
        byline.append(element("time", "", when));
        body.append(byline, element("div", "message-text", message.text));
        row.append(element("div", "avatar", message.role === "user" ? "F" : message.role === "system" ? "↳" : "✳"), body);
        $("messages").append(row);
      }
    }
    $("prompt").value = drafts[task.id] || "";
    const elapsed = task.status === "running" ? ` · ${formatSeconds(task.elapsed)}` : "";
    $("activity").textContent = `${task.status === "running" ? "◌" : "·"} ${task.activity || labels[task.status]}${elapsed}`;
    $("stop-task").hidden = task.status === "idle";
    updateComposer();
    $("messages").scrollTop = forceBottom ? $("messages").scrollHeight : (scrollPositions.get(task.id) ?? $("messages").scrollHeight);
    updateLatest();
  }
  function renderNoTasks() {
    $("task-title").textContent = "还没有任务";
    $("task-subtitle").textContent = "点击左侧“新建任务”开始";
    $("messages").replaceChildren(element("div", "empty-conversation", "创建第一个任务后即可发送消息。"));
    $("prompt").disabled = true;
    $("send").disabled = true;
    $("stop-task").hidden = true;
  }
  function formatSeconds(value) {
    const seconds = Number(value) || 0;
    return seconds >= 60 ? `${Math.floor(seconds / 60)}分${String(seconds % 60).padStart(2, "0")}秒` : `${seconds}秒`;
  }
  function updateComposer() {
    const task = current();
    $("prompt").disabled = !task;
    $("send").disabled = !task || !$("prompt").value.trim();
    $("draft-hint").textContent = $("prompt").value ? "草稿已保存在此浏览器" : "草稿随任务保留";
  }
  function updateLatest() {
    const panel = $("messages");
    $("latest").hidden = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 60;
  }
  async function fetchMessages(id, countUnread = true) {
    const after = cursors.get(id) || 0;
    const firstLoad = !histories.has(id);
    const body = await api(`/api/tasks/${encodeURIComponent(id)}/messages?after=${after}`);
    if (!histories.has(id)) histories.set(id, []);
    if (body.messages.length) {
      histories.get(id).push(...body.messages);
      cursors.set(id, body.messages.at(-1).id);
      if (!firstLoad && countUnread && id !== currentId) unread.set(id, (unread.get(id) || 0) + body.messages.length);
    }
    return body.messages.length;
  }
  async function refresh() {
    if (polling) return;
    polling = true;
    try {
      const panel = $("messages");
      const priorScroll = panel.scrollTop;
      const wasNearBottom = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 60;
      const body = await api("/api/state");
      tasks = body.tasks;
      agentAvailability = Object.fromEntries(
        Object.entries(body.agents || {}).map(([agent, state]) => [agent, state.available !== false])
      );
      window.telegramCurrent = body.telegramCurrent;
      window.telegramCurrentByAgent = body.telegramCurrentByAgent || {};
      if (!tasks.some(task => task.id === currentId)) currentId = tasks[0]?.id || "";
      let currentReceived = false;
      for (const task of tasks) {
        const received = await fetchMessages(task.id);
        if (task.id === currentId && received) currentReceived = true;
      }
      const unavailable = Object.entries(agentAvailability)
        .filter(([, available]) => !available)
        .map(([agent]) => agent === "claude" ? "Claude" : "Codex");
      $("connection-state").textContent = unavailable.length ? `${unavailable.join("、")} 暂不可用` : "已连接";
      updateAgentOptions();
      if (currentId) scrollPositions.set(currentId, priorScroll);
      renderList();
      if (current()) renderTask(currentReceived && wasNearBottom);
    } catch (error) {
      $("connection-state").textContent = "连接失败";
      toast(error.message, true);
    } finally { polling = false; }
  }

  $("search").addEventListener("input", renderList);
  $("prompt").addEventListener("input", () => { saveDraft(); updateComposer(); });
  $("prompt").addEventListener("keydown", event => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault();
      if (!$("send").disabled) $("composer").requestSubmit();
    }
  });
  $("composer").addEventListener("submit", async event => {
    event.preventDefault();
    const task = current();
    const prompt = $("prompt").value.trim();
    if (!task || !prompt) return;
    $("send").disabled = true;
    try {
      const body = await api(`/api/tasks/${encodeURIComponent(task.id)}/submit`, { method: "POST", body: JSON.stringify({ prompt }) });
      drafts[task.id] = "";
      $("prompt").value = "";
      saveDraft();
      if (body.notice) toast(body.notice);
      await refresh();
      $("prompt").focus();
    } catch (error) { toast(error.message, true); updateComposer(); }
  });
  $("stop-task").addEventListener("click", async () => {
    const task = current(); if (!task) return;
    $("stop-task").disabled = true;
    try {
      const body = await api(`/api/tasks/${encodeURIComponent(task.id)}/stop`, { method: "POST", body: "{}" });
      toast(body.notice); await refresh();
    } catch (error) { toast(error.message, true); }
    finally { $("stop-task").disabled = false; }
  });
  $("messages").addEventListener("scroll", updateLatest);
  $("latest").addEventListener("click", () => { $("messages").scrollTop = $("messages").scrollHeight; });
  window.addEventListener("resize", updateLatest);
  $("details-toggle").addEventListener("click", () => {
    detailsOpen = !detailsOpen;
    $("task-details").hidden = !detailsOpen;
    $("details-toggle").setAttribute("aria-expanded", String(detailsOpen));
    updateLatest();
  });
  function openDialog() {
    $("create-form").reset();
    $("create-error").textContent = "";
    $("new-directory").value = current()?.workdir || "H:\\AIworkspace";
    updateAgentOptions();
    const preferred = current()?.agent;
    const preferredOption = [...$("new-agent").options].find(
      option => option.value.toLowerCase() === preferred && !option.disabled
    );
    const firstAvailable = [...$("new-agent").options].find(option => !option.disabled);
    if (preferredOption || firstAvailable) $("new-agent").value = (preferredOption || firstAvailable).value;
    $("new-dialog").showModal();
    $("new-name").focus();
  }
  $("new-task").addEventListener("click", openDialog);
  $("close-dialog").addEventListener("click", () => $("new-dialog").close());
  $("cancel-dialog").addEventListener("click", () => $("new-dialog").close());
  document.addEventListener("keydown", event => {
    if (event.key.toLowerCase() === "n" && !event.ctrlKey && !event.altKey && !event.metaKey && !event.isComposing && !$("new-dialog").open && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
      event.preventDefault(); openDialog();
    }
  });
  $("create-form").addEventListener("submit", async event => {
    event.preventDefault();
    $("create-error").textContent = "";
    try {
      const body = await api("/api/tasks", { method: "POST", body: JSON.stringify({ name: $("new-name").value, agent: $("new-agent").value, workdir: $("new-directory").value }) });
      $("new-dialog").close();
      await refresh();
      await selectTask(body.task.id);
      $("prompt").focus();
      toast("任务已创建");
    } catch (error) { $("create-error").textContent = error.message; }
  });
  if (!token) {
    $("connection-state").textContent = "缺少凭据";
    toast("请使用启动日志中带 token 的完整 GUI 地址打开。", true);
  } else {
    refresh();
    setInterval(refresh, 2000);
  }
})();
