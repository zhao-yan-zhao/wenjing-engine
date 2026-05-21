const state = {
  token: localStorage.getItem("auth_token") || "",
  user: null,
  chosenFile: null,
};

const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const reportInput = document.getElementById("reportInput");
const statusText = document.getElementById("statusText");
const startBtn = document.getElementById("startBtn");
const progressWrap = document.getElementById("progressWrap");
const progressFill = document.getElementById("progressFill");
const progressLabel = document.getElementById("progressLabel");
const metricBox = document.getElementById("metricBox");
const reportBoard = document.getElementById("reportBoard");

const authModal = document.getElementById("authModal");
const openAuthBtn = document.getElementById("openAuthBtn");
const closeAuthBtn = document.getElementById("closeAuthBtn");
const loginBtn = document.getElementById("loginBtn");
const registerBtn = document.getElementById("registerBtn");
const logoutBtn = document.getElementById("logoutBtn");
const authStatus = document.getElementById("authStatus");
const authUsername = document.getElementById("authUsername");
const authPassword = document.getElementById("authPassword");

const dashboard = document.getElementById("dashboard");
const adminPanel = document.getElementById("adminPanel");
const userInfoText = document.getElementById("userInfoText");
const myJobs = document.getElementById("myJobs");

function apiHeaders(json = false) {
  const h = {};
  if (json) h["Content-Type"] = "application/json";
  if (state.token) h.Authorization = `Bearer ${state.token}`;
  return h;
}

async function api(path, options = {}) {
  const resp = await fetch(path, options);
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.error || "请求失败");
  }
  return data;
}

function setStatus(message) {
  statusText.textContent = message;
}

function setProgress(value) {
  const clamped = Math.max(0, Math.min(100, Number(value) || 0));
  progressFill.style.width = `${clamped}%`;
  progressLabel.textContent = `${clamped}%`;
  const track = progressFill.parentElement;
  if (track) track.setAttribute("aria-valuenow", String(clamped));
}

function resetProgress() {
  progressWrap.hidden = true;
  setProgress(0);
}

function showProgress() {
  progressWrap.hidden = false;
}

function removeResultLink() {
  const existing = document.getElementById("resultLink");
  if (existing) existing.remove();
}

function showResultLink(downloadUrl, outputName) {
  removeResultLink();
  const link = document.createElement("a");
  link.id = "resultLink";
  link.href = downloadUrl;
  link.textContent = `下载处理结果：${outputName}`;
  link.style.display = "inline-block";
  link.style.marginTop = "8px";
  link.style.color = "#0a6f5a";
  link.style.fontWeight = "700";
  statusText.insertAdjacentElement("afterend", link);
}

function showMetric(job) {
  metricBox.classList.remove("hidden");
  const before = job.aigc_before?.score ?? "-";
  const after = job.aigc_after?.score ?? "-";
  const drop = job.aigc_drop ?? "-";
  const targets = job.rewrite_targets ?? 0;
  const chars = job.estimated_ai_chars ?? 0;
  metricBox.textContent = `AIGC疑似率(估计)：${before} -> ${after}（下降 ${drop}）；定向改写段落：${targets}；预计送模字符：${chars}`;
}

function hideMetric() {
  metricBox.classList.add("hidden");
  metricBox.textContent = "";
}

function setList(targetId, items) {
  const node = document.getElementById(targetId);
  if (!node) return;
  node.innerHTML = (items || [])
    .map((item) => `<li>${item}</li>`)
    .join("") || "<li>暂无</li>";
}

function renderVisualReport(job) {
  const report = job.visual_report;
  if (!report) {
    reportBoard.classList.add("hidden");
    return;
  }

  reportBoard.classList.remove("hidden");
  document.getElementById("reportBefore").textContent = report.before?.score ?? "-";
  document.getElementById("reportAfter").textContent = report.after?.score ?? "-";
  document.getElementById("reportDrop").textContent = report.drop ?? "-";
  document.getElementById("reportTargets").textContent = report.rewrite_targets ?? "-";
  setList("reportBeforeSignals", report.before?.signals || []);
  setList("reportAfterSignals", report.after?.signals || []);
}

function pickFile(file) {
  state.chosenFile = file;
  removeResultLink();
  hideMetric();
  reportBoard.classList.add("hidden");
  resetProgress();
  setStatus(`已选择：${file.name}（${Math.ceil(file.size / 1024)} KB）`);
}

async function pollJob(jobId) {
  for (;;) {
    const data = await api(`/api/job/${jobId}`, {
      headers: apiHeaders(false),
    });

    setProgress(data.progress ?? 0);
    if (data.message) setStatus(data.message);

    if (data.status === "completed") {
      const noticeText = data.notice ? `；${data.notice}` : "";
      setStatus(`处理完成：${Math.ceil(data.bytes / 1024)} KB；引擎：${data.engine}${noticeText}`);
      showResultLink(data.download_url, data.output_name);
      showMetric(data);
      renderVisualReport(data);
      await loadMyJobs();
      if (state.user?.role === "admin") {
        await loadAdminPanel();
      }
      return;
    }

    if (data.status === "failed") {
      throw new Error(data.error || "处理失败");
    }

    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}

async function loadAppVersion() {
  const versionNode = document.getElementById("appVersion");
  if (!versionNode) return;

  try {
    const data = await api("/api/version");
    const shortCommit = data.commit && data.commit !== "unknown" ? data.commit : "unknown";
    versionNode.textContent = `v-${shortCommit} · ${data.provider}:${data.model}`;
  } catch {
    versionNode.textContent = "v-unknown";
  }
}

function openAuthModal() {
  authModal.classList.remove("hidden");
}

function closeAuthModal() {
  authModal.classList.add("hidden");
}

async function login() {
  authStatus.textContent = "登录中...";
  try {
    const data = await api("/api/login", {
      method: "POST",
      headers: apiHeaders(true),
      body: JSON.stringify({
        username: authUsername.value.trim(),
        password: authPassword.value,
      }),
    });

    state.token = data.token;
    localStorage.setItem("auth_token", state.token);
    authStatus.textContent = "登录成功";
    await refreshUser();
    closeAuthModal();
  } catch (err) {
    authStatus.textContent = `登录失败：${err.message}`;
  }
}

async function registerUser() {
  authStatus.textContent = "注册中...";
  try {
    const data = await api("/api/register", {
      method: "POST",
      headers: apiHeaders(true),
      body: JSON.stringify({
        username: authUsername.value.trim(),
        password: authPassword.value,
      }),
    });
    authStatus.textContent = `${data.message}，请继续登录`;
  } catch (err) {
    authStatus.textContent = `注册失败：${err.message}`;
  }
}

async function logout() {
  try {
    await api("/api/logout", { method: "POST", headers: apiHeaders(false) });
  } catch {
    // ignore
  }

  state.token = "";
  state.user = null;
  localStorage.removeItem("auth_token");
  renderAuthState();
  setStatus("已退出登录");
}

function renderAuthState() {
  if (state.user) {
    openAuthBtn.classList.add("hidden");
    logoutBtn.classList.remove("hidden");
    dashboard.classList.remove("hidden");
    userInfoText.textContent = `当前用户：${state.user.username}（${state.user.role}）`;
    startBtn.disabled = false;

    if (state.user.role === "admin") {
      adminPanel.classList.remove("hidden");
    } else {
      adminPanel.classList.add("hidden");
    }
  } else {
    openAuthBtn.classList.remove("hidden");
    logoutBtn.classList.add("hidden");
    dashboard.classList.add("hidden");
    adminPanel.classList.add("hidden");
    startBtn.disabled = true;
    setStatus("请先登录后上传文件");
  }
}

async function refreshUser() {
  if (!state.token) {
    state.user = null;
    renderAuthState();
    return;
  }

  try {
    state.user = await api("/api/me", { headers: apiHeaders(false) });
    renderAuthState();
    await loadMyJobs();
    if (state.user.role === "admin") {
      await loadAdminPanel();
    }
  } catch {
    state.token = "";
    state.user = null;
    localStorage.removeItem("auth_token");
    renderAuthState();
  }
}

function renderJobItems(items, container) {
  if (!items.length) {
    container.innerHTML = "<p class='hint'>暂无记录</p>";
    return;
  }

  container.innerHTML = items
    .slice(0, 8)
    .map((j) => {
      const risk = j.aigc_before?.score;
      const after = j.aigc_after?.score;
      const drop = j.aigc_drop;
      return `<div class='metric'><strong>${j.job_id}</strong> | ${j.status} | ${j.level} | ${j.engine || '-'}<br/>AIGC估计：${risk ?? '-'} -> ${after ?? '-'}（下降 ${drop ?? '-'}）</div>`;
    })
    .join("");
}

async function loadMyJobs() {
  if (!state.user) return;
  try {
    const data = await api("/api/my/jobs", { headers: apiHeaders(false) });
    renderJobItems(data.items || [], myJobs);
  } catch {
    myJobs.innerHTML = "<p class='hint'>加载失败</p>";
  }
}

async function loadAdminPanel() {
  const statsNode = document.getElementById("adminStats");
  const usersNode = document.getElementById("adminUsers");
  const jobsNode = document.getElementById("adminJobs");

  try {
    const [stats, users, jobs] = await Promise.all([
      api("/api/admin/stats", { headers: apiHeaders(false) }),
      api("/api/admin/users", { headers: apiHeaders(false) }),
      api("/api/admin/jobs", { headers: apiHeaders(false) }),
    ]);

    statsNode.innerHTML = `用户：${stats.users}<br/>任务总数：${stats.jobs_total}<br/>完成：${stats.jobs_completed}<br/>平均降分：${stats.avg_aigc_drop}`;

    usersNode.innerHTML = (users.items || [])
      .slice(0, 10)
      .map((u) => `<div class='hint'>${u.username} (${u.role})</div>`)
      .join("") || "<p class='hint'>暂无用户</p>";

    renderJobItems(jobs.items || [], jobsNode);
  } catch (err) {
    statsNode.textContent = `加载失败：${err.message}`;
    usersNode.textContent = "加载失败";
    jobsNode.textContent = "加载失败";
  }
}

async function checkAigc() {
  if (!state.user) {
    setStatus("请先登录后使用检测功能");
    openAuthModal();
    return;
  }

  const text = document.getElementById("checkText").value.trim();
  const resultNode = document.getElementById("checkResult");

  if (!text) {
    resultNode.innerHTML = "<p class='hint'>请先输入文本</p>";
    return;
  }

  resultNode.innerHTML = "<p class='hint'>检测中...</p>";
  try {
    const data = await api("/api/aigc/check", {
      method: "POST",
      headers: apiHeaders(true),
      body: JSON.stringify({ text }),
    });

    const flags = (data.flagged_sentences || [])
      .map((f) => `<li>(${f.score}) ${f.text}</li>`)
      .join("");

    resultNode.innerHTML = `
      <div class='metric'>
        <strong>疑似分值：</strong>${data.score}（${data.label}）<br/>
        <strong>信号：</strong>${(data.signals || []).join("；")}<br/>
        <strong>高风险句：</strong>
        <ul>${flags || "<li>暂无明显高风险句</li>"}</ul>
      </div>
    `;
  } catch (err) {
    resultNode.innerHTML = `<p class='hint'>检测失败：${err.message}</p>`;
  }
}

// Upload interactions
dropzone.addEventListener("click", () => fileInput.click());

fileInput.addEventListener("change", (event) => {
  const [file] = event.target.files;
  if (file) pickFile(file);
});

["dragenter", "dragover"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (e) => {
    e.preventDefault();
    dropzone.style.borderColor = "#0e8a6f";
    dropzone.style.background = "#e5f7f1";
  });
});

["dragleave", "drop"].forEach((eventName) => {
  dropzone.addEventListener(eventName, (e) => {
    e.preventDefault();
    dropzone.style.borderColor = "#9dd4c7";
    dropzone.style.background = "#f1fbf8";
  });
});

dropzone.addEventListener("drop", (e) => {
  const [file] = e.dataTransfer.files;
  if (file) pickFile(file);
});

startBtn.addEventListener("click", async () => {
  if (!state.user) {
    setStatus("请先登录后上传文件");
    openAuthModal();
    return;
  }

  if (!state.chosenFile) {
    setStatus("请先选择文件");
    return;
  }

  removeResultLink();
  hideMetric();
  reportBoard.classList.add("hidden");
  showProgress();
  setProgress(2);

  const level = document.getElementById("level").value;
  setStatus(`正在以“${level}”模式提交任务...`);

  startBtn.disabled = true;
  startBtn.textContent = "处理中...";

  try {
    const formData = new FormData();
    formData.append("file", state.chosenFile);
    formData.append("level", level);
    if (reportInput.files[0]) {
      formData.append("report", reportInput.files[0]);
    }

    const resp = await fetch("/api/process", {
      method: "POST",
      headers: apiHeaders(false),
      body: formData,
    });

    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "处理失败");
    }

    setProgress(data.progress ?? 6);
    await pollJob(data.job_id);
  } catch (err) {
    setStatus(`处理失败：${err.message}`);
  } finally {
    startBtn.disabled = false;
    startBtn.textContent = "开始处理";
  }
});

// Auth & misc
document.getElementById("jumpUploaderBtn").addEventListener("click", () => {
  document.getElementById("uploaderCard").scrollIntoView({ behavior: "smooth" });
});
document.getElementById("jumpDetectBtn").addEventListener("click", () => {
  document.getElementById("detect").scrollIntoView({ behavior: "smooth" });
});

document.getElementById("checkBtn").addEventListener("click", checkAigc);
openAuthBtn.addEventListener("click", openAuthModal);
closeAuthBtn.addEventListener("click", closeAuthModal);
loginBtn.addEventListener("click", login);
registerBtn.addEventListener("click", registerUser);
logoutBtn.addEventListener("click", logout);

authModal.addEventListener("click", (e) => {
  if (e.target === authModal) closeAuthModal();
});

loadAppVersion();
refreshUser();
