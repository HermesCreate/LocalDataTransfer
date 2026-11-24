(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const STATE_KEYS = { "就绪": "ready", "传输中": "running", "正在停止": "stopping", "已停止": "stopped",
    "完成": "done", "部分失败": "partial", "失败": "failed", "后台未连接": "offline" };

  let hosts = [];
  let selected = [];          // in selection order
  let running = false;
  let offlineNotified = false;
  let clearedAt = 0;          // log_total at the time "清空" was clicked
  let lastLogKey = "";

  async function api(path, body) {
    const opts = body === undefined ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch (_) { throw new Error("后台返回了无法解析的内容。"); }
    if (!res.ok || data.ok === false) throw new Error(data.error || `请求失败（HTTP ${res.status}）`);
    return data;
  }

  function message(text, ok) {
    const el = $("formMsg");
    el.textContent = text || "";
    el.classList.toggle("ok", !!ok);
  }

  // ---------------- servers multi-select
  function renderSummary() {
    const s = $("serversSummary");
    if (selected.length) { s.textContent = selected.join(" / "); s.classList.remove("placeholder"); }
    else { s.textContent = hosts.length ? "选择服务器" : "~/.ssh/config 中没有可用的 Host"; s.classList.add("placeholder"); }
    $("serversCount").textContent = `已选 ${selected.length} / ${hosts.length}`;
  }

  function renderHosts() {
    const box = $("serversOptions");
    box.innerHTML = "";
    if (!hosts.length) {
      box.innerHTML = '<div class="ms-empty">没有读取到具体的 Host 别名。</div>';
    }
    hosts.forEach((h) => {
      const label = document.createElement("label");
      label.className = "ms-option";
      label.setAttribute("role", "option");
      const cb = document.createElement("input");
      cb.type = "checkbox"; cb.value = h; cb.checked = selected.includes(h); cb.disabled = running;
      cb.addEventListener("change", () => {
        if (cb.checked) { if (!selected.includes(h)) selected.push(h); }
        else selected = selected.filter((x) => x !== h);
        label.setAttribute("aria-selected", cb.checked);
        renderSummary();
      });
      label.setAttribute("aria-selected", cb.checked);
      const span = document.createElement("span"); span.textContent = h;
      label.append(cb, span);
      box.appendChild(label);
    });
    renderSummary();
  }

  async function loadHosts() {
    try {
      const data = await api("/api/hosts");
      hosts = data.hosts || [];
      selected = selected.filter((h) => hosts.includes(h));
      renderHosts();
    } catch (e) { message("读取 ~/.ssh/config 失败：" + e.message); }
  }

  function toggleMenu(open) {
    const menu = $("serversMenu"), t = $("serversToggle");
    const willOpen = open === undefined ? menu.hidden : open;
    menu.hidden = !willOpen;
    t.setAttribute("aria-expanded", String(willOpen));
  }

  // ---------------- config
  async function loadConfig() {
    try {
      const c = await api("/api/config");
      $("local").value = c.local || "";
      $("remotePath").value = c.remote_path || "";
      selected = Array.isArray(c.servers) ? c.servers.slice() : [];
      const dir = c.direction === "server_to_local" ? "server_to_local" : "local_to_server";
      document.querySelector(`input[name=direction][value=${dir}]`).checked = true;
    } catch (e) { message(e.message); }
  }

  function direction() { return document.querySelector("input[name=direction]:checked").value; }

  // ---------------- actions
  async function chooseFolder() {
    const btn = $("chooseFolder");
    btn.disabled = true;
    message("");
    try {
      const r = await api("/api/choose-folder", { current: $("local").value });
      if (!r.cancelled && r.path) { $("local").value = r.path; message("已选择文件夹并保存。", true); }
    } catch (e) { message(e.message); }
    finally { btn.disabled = running; }
  }

  async function start() {
    message("");
    const payload = { local: $("local").value, servers: selected.slice(), remote_path: $("remotePath").value, direction: direction() };
    if (!payload.servers.length) return message("请至少选择一台服务器。");
    if (!payload.remote_path.trim()) return message("远程路径不能为空。");
    if (!payload.local.trim()) return message("本地地址不能为空。");
    $("startBtn").disabled = true;
    try { await api("/api/start", payload); clearedAt = 0; toggleMenu(false); poll(); }
    catch (e) { message(e.message); $("startBtn").disabled = running; }
  }

  async function stop() {
    try { await api("/api/stop", {}); poll(); } catch (e) { message(e.message); }
  }

  // ---------------- status
  function setRunning(isRunning) {
    running = isRunning;
    $("startBtn").disabled = isRunning;
    $("stopBtn").disabled = !isRunning;
    $("chooseFolder").disabled = isRunning;
    ["local", "remotePath"].forEach((id) => { $(id).readOnly = isRunning; });
    document.querySelectorAll("input[name=direction], #serversOptions input").forEach((el) => { el.disabled = isRunning; });
  }

  function setPill(text) {
    $("statusText").textContent = text;
    $("statusPill").dataset.state = STATE_KEYS[text] || "ready";
  }

  function renderStatus(s) {
    setPill(s.status);
    setRunning(!!s.running);
    const pct = Math.max(0, Math.min(100, Number(s.percent) || 0));
    $("percent").textContent = pct + "%";
    $("barFill").style.width = pct + "%";
    $("speed").textContent = s.speed || "0 B/s";
    $("eta").textContent = s.eta || "--";
    $("curLocal").textContent = s.local || "--";
    $("curServer").textContent = s.current_server || (s.servers && s.servers.length ? s.servers.join(" / ") : "--");
    $("curRemote").textContent = s.remote_path ? (s.current_server ? `${s.current_server}:${s.remote_path}` : s.remote_path) : "--";
    const ul = $("results"); ul.innerHTML = "";
    (s.results || []).forEach((r) => {
      const li = document.createElement("li");
      const cls = { "完成": "r-done", "失败": "r-fail", "传输中": "r-run", "已停止": "r-stop" }[r.status] || "";
      const a = document.createElement("span"); a.textContent = r.server;
      const b = document.createElement("span"); b.className = cls;
      b.textContent = r.status + (r.return_code !== null && r.return_code !== undefined && r.return_code !== 0 ? `（返回码 ${r.return_code}）` : "");
      li.append(a, b); ul.appendChild(li);
    });
    // logs: full array, minus what the user cleared locally
    const total = s.log_total || 0;
    if (total < clearedAt) clearedAt = 0;
    const logs = s.logs || [];
    const visible = logs.slice(Math.max(0, logs.length - (total - clearedAt)));
    const key = total + ":" + clearedAt;
    if (key !== lastLogKey) {
      lastLogKey = key;
      const el = $("log");
      el.textContent = visible.join("\n");
      el.scrollTop = el.scrollHeight;
    }
  }

  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const s = await api("/api/status");
      if (offlineNotified) { offlineNotified = false; message("后台已重新连接。", true); }
      renderStatus(s);
      window.__lastStatus = s;
    } catch (e) {
      setPill("后台未连接");
      if (!offlineNotified) { offlineNotified = true; message("后台未连接：请用“启动传输工具.command”重新启动服务。"); }
    } finally { polling = false; }
  }

  // ---------------- wire up
  $("serversToggle").addEventListener("click", () => toggleMenu());
  document.addEventListener("click", (e) => { if (!$("servers").contains(e.target)) toggleMenu(false); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") toggleMenu(false); });
  $("refreshHosts").addEventListener("click", async () => { await loadHosts(); message("已重新读取 ~/.ssh/config。", true); });
  $("chooseFolder").addEventListener("click", chooseFolder);
  $("startBtn").addEventListener("click", start);
  $("stopBtn").addEventListener("click", stop);
  $("clearLog").addEventListener("click", () => {
    clearedAt = (window.__lastStatus && window.__lastStatus.log_total) || 0;
    lastLogKey = ""; $("log").textContent = "";
  });

  (async () => {
    try { const h = await api("/api/health"); $("testBadge").hidden = !h.test_mode; } catch (_) {}
    await loadConfig();
    await loadHosts();
    await poll();
    setInterval(poll, 500);
  })();
})();
