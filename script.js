const dropzone = document.getElementById("dropzone");
const fileInput = document.getElementById("fileInput");
const statusText = document.getElementById("statusText");
const startBtn = document.getElementById("startBtn");
const progressWrap = document.getElementById("progressWrap");
const progressFill = document.getElementById("progressFill");
const progressLabel = document.getElementById("progressLabel");

let chosenFile = null;

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

function pickFile(file) {
  chosenFile = file;
  removeResultLink();
  resetProgress();
  setStatus(`已选择：${file.name}（${Math.ceil(file.size / 1024)} KB）`);
}

async function pollJob(jobId) {
  for (;;) {
    const resp = await fetch(`/api/job/${jobId}`);
    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "查询任务状态失败");
    }

    setProgress(data.progress ?? 0);
    if (data.message) {
      setStatus(data.message);
    }

    if (data.status === "completed") {
      const noticeText = data.notice ? `；${data.notice}` : "";
      setStatus(`处理完成：${Math.ceil(data.bytes / 1024)} KB；引擎：${data.engine}${noticeText}`);
      showResultLink(data.download_url, data.output_name);
      return;
    }

    if (data.status === "failed") {
      throw new Error(data.error || "处理失败");
    }

    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}

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
  if (!chosenFile) {
    setStatus("请先上传文件后再处理");
    return;
  }

  removeResultLink();
  showProgress();
  setProgress(2);

  const level = document.getElementById("level").value;
  setStatus(`正在以“${level}”模式提交任务...`);

  startBtn.disabled = true;
  startBtn.textContent = "处理中...";

  try {
    const formData = new FormData();
    formData.append("file", chosenFile);
    formData.append("level", level);

    const resp = await fetch("/api/process", {
      method: "POST",
      body: formData,
    });

    const data = await resp.json();
    if (!resp.ok) {
      throw new Error(data.error || "处理失败");
    }

    setProgress(data.progress ?? 5);
    await pollJob(data.job_id);
  } catch (err) {
    setStatus(`处理失败：${err.message}`);
  } finally {
    startBtn.disabled = false;
    startBtn.textContent = "开始处理";
  }
});
