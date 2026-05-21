import io
import json
import os
import re
import shutil
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_AI_CHARS = 12000
AI_CHUNK_SIZE = 10000
AI_CHUNK_OVERLAP = 400

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

JOBS = {}
JOBS_LOCK = threading.Lock()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip().lstrip("\ufeff")
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv(BASE_DIR / ".env")
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")


def safe_name(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^\w\-.\u4e00-\u9fff]", "_", name)
    return name[:120] or f"file_{uuid.uuid4().hex[:8]}.txt"


def get_app_version() -> dict:
    commit = os.getenv("RENDER_GIT_COMMIT", "").strip()
    if not commit:
        try:
            commit = subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=BASE_DIR,
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip()
        except Exception:
            commit = "unknown"

    return {
        "commit": commit[:7] if commit and commit != "unknown" else commit,
        "model": OPENAI_MODEL,
    }


def parse_multipart_form_data(body: bytes, content_type: str) -> tuple[dict, dict]:
    raw_message = (
        f"Content-Type: {content_type}\r\n"
        "MIME-Version: 1.0\r\n\r\n"
    ).encode("utf-8") + body

    message = BytesParser(policy=default).parsebytes(raw_message)
    if not message.is_multipart():
        raise ValueError("请求体不是 multipart/form-data")

    fields = {}
    files = {}

    for part in message.iter_parts():
        disposition = part.get("Content-Disposition", "")
        if "form-data" not in disposition:
            continue

        name = part.get_param("name", header="Content-Disposition")
        if not name:
            continue

        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""

        if filename is None:
            fields[name] = payload.decode("utf-8", errors="ignore")
        else:
            files[name] = {
                "filename": filename,
                "data": payload,
            }

    return fields, files


def create_job() -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "progress": 0,
            "message": "任务已创建",
            "engine": "",
            "notice": "",
            "download_url": "",
            "output_name": "",
            "bytes": 0,
            "error": "",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    return job_id


def update_job(job_id: str, **kwargs) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(kwargs)
        job["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def optimize_text_rule(text: str, level: str) -> str:
    replacements = {
        "标准": [
            ("本文", "本研究"),
            ("我们", "本文"),
            ("非常", "较为"),
        ],
        "增强": [
            ("本文", "本研究"),
            ("我们", "本文"),
            ("非常", "较为"),
            ("所以", "因此"),
            ("比如", "例如"),
        ],
        "深度": [
            ("本文", "本研究"),
            ("我们", "本文"),
            ("非常", "较为"),
            ("所以", "因此"),
            ("比如", "例如"),
            ("很多", "大量"),
            ("这个", "该"),
        ],
    }

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    polished = "\n".join(lines)

    for old, new in replacements.get(level, replacements["标准"]):
        polished = polished.replace(old, new)

    return polished + "\n"


def decode_text_bytes(file_data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return file_data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return file_data.decode("utf-8", errors="ignore")


def extract_docx_text(file_data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(file_data)) as zf:
        xml_bytes = zf.read("word/document.xml")

    root = ET.fromstring(xml_bytes)
    pieces = []
    for node in root.iter():
        tag = node.tag
        if tag.endswith("}t") and node.text:
            pieces.append(node.text)
        elif tag.endswith("}p"):
            pieces.append("\n")

    text = "".join(pieces)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def file_to_text(file_data: bytes, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        return decode_text_bytes(file_data).strip()
    if suffix == ".docx":
        return extract_docx_text(file_data)
    if suffix == ".doc":
        raise ValueError("`.doc` 为旧二进制格式，当前仅支持 `.docx/.txt/.md`")

    return decode_text_bytes(file_data).strip()


def extract_output_text(response_json: dict) -> str:
    top_text = response_json.get("output_text")
    if isinstance(top_text, str) and top_text.strip():
        return top_text.strip()

    chunks = []
    for item in response_json.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            ctype = content.get("type")
            if ctype in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])

    merged = "\n".join(chunks).strip()
    if merged:
        return merged

    raise RuntimeError("AI 返回内容为空")


def optimize_text_with_openai(text: str, level: str) -> tuple[str, str]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 OPENAI_API_KEY")

    if not text.strip():
        raise RuntimeError("文本为空")

    level_rules = {
        "标准": "以轻度润色为主，尽量保留原句。",
        "增强": "进行中度重写，优化流畅度与衔接。",
        "深度": "进行深度重写，提升学术表达与逻辑紧凑度。",
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "developer",
                "content": (
                    "你是学术写作优化助手。"
                    "请仅输出优化后的正文，不要加标题、注释、Markdown、前后说明。"
                    "保留段落结构和原意，避免虚构数据。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"优化强度：{level}。\n"
                    f"要求：{level_rules.get(level, level_rules['标准'])}\n"
                    "请优化下面文本：\n"
                    f"{text}"
                ),
            },
        ],
    }

    req = urllib.request.Request(
        OPENAI_API_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as http_err:
        err_raw = http_err.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(err_raw)
            message = err_json.get("error", {}).get("message") or err_raw
        except json.JSONDecodeError:
            message = err_raw or str(http_err)
        raise RuntimeError(f"OpenAI 接口错误：{message}") from http_err
    except Exception as exc:
        raise RuntimeError(f"OpenAI 请求失败：{exc}") from exc

    response_json = json.loads(raw.decode("utf-8"))
    output_text = extract_output_text(response_json)
    return output_text, OPENAI_MODEL


def split_text_into_chunks(text: str, size: int = AI_CHUNK_SIZE, overlap: int = AI_CHUNK_OVERLAP) -> list[str]:
    normalized = text.replace("\r\n", "\n").strip()
    if len(normalized) <= size:
        return [normalized]

    chunks = []
    start = 0
    length = len(normalized)

    while start < length:
        end = min(start + size, length)

        if end < length:
            cut = normalized.rfind("\n", start + int(size * 0.6), end)
            if cut == -1:
                cut = normalized.rfind("。", start + int(size * 0.6), end)
            if cut != -1:
                end = cut + 1

        part = normalized[start:end].strip()
        if part:
            chunks.append(part)

        if end >= length:
            break

        start = max(0, end - overlap)

    return chunks


def optimize_text_with_openai_chunked(
    text: str,
    level: str,
    chunks: list[str] | None = None,
    progress_callback=None,
) -> tuple[str, str]:
    if chunks is None:
        chunks = split_text_into_chunks(text)

    output_parts = []
    model_name = OPENAI_MODEL
    total = len(chunks)

    for idx, chunk in enumerate(chunks, start=1):
        if progress_callback is not None:
            progress_callback(idx, total)

        if len(chunk) > MAX_AI_CHARS:
            raise RuntimeError(
                f"分段后仍超限（第 {idx} 段 {len(chunk)} 字符 > {MAX_AI_CHARS}），请调小 AI_CHUNK_SIZE"
            )

        optimized, model_name = optimize_text_with_openai(chunk, level)
        output_parts.append(optimized.strip())

    merged = "\n\n".join(output_parts).strip()
    return merged, model_name


def process_document_job(job_id: str, source_text: str, level: str, saved_output: Path) -> None:
    update_job(job_id, status="processing", progress=10, message="正在调用处理引擎...")

    engine = "rule-fallback"
    notice = ""

    try:
        if len(source_text) <= MAX_AI_CHARS:
            update_job(job_id, progress=35, message="AI 正在处理正文...")
            optimized_body, used_model = optimize_text_with_openai(source_text, level)
            engine = f"openai:{used_model}"
            update_job(job_id, progress=88, message="AI 处理完成，正在生成结果...")
        else:
            chunks = split_text_into_chunks(source_text)
            chunk_count = len(chunks)

            def on_chunk(idx: int, total: int) -> None:
                ratio = idx / max(total, 1)
                pct = 25 + int(ratio * 60)
                update_job(job_id, progress=min(pct, 90), message=f"AI 分段处理中：第 {idx}/{total} 段")

            optimized_body, used_model = optimize_text_with_openai_chunked(
                source_text,
                level,
                chunks=chunks,
                progress_callback=on_chunk,
            )
            engine = f"openai:{used_model}:chunked({chunk_count})"
            notice = f"文本较长，已自动分段调用 AI（共 {chunk_count} 段）"
            update_job(job_id, progress=92, message="分段合并完成，正在写入结果...")
    except Exception as ai_exc:
        optimized_body = optimize_text_rule(source_text, level)
        notice = f"AI 不可用，已回退规则引擎：{ai_exc}"
        update_job(job_id, progress=85, message="AI 不可用，正在回退规则引擎...")

    try:
        result_text = (
            "【文净引擎处理结果】\n"
            f"处理时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"优化强度：{level}\n"
            f"处理引擎：{engine}\n"
            "----------------------------------------\n"
            f"{optimized_body.strip()}\n"
        )

        with saved_output.open("w", encoding="utf-8") as f:
            f.write(result_text)

        update_job(
            job_id,
            status="completed",
            progress=100,
            message="处理完成",
            engine=engine,
            notice=notice,
            download_url=f"/api/download/{saved_output.name}",
            output_name=saved_output.name,
            bytes=saved_output.stat().st_size,
        )
    except Exception as write_exc:
        update_job(
            job_id,
            status="failed",
            progress=100,
            message="结果写入失败",
            error=str(write_exc),
            notice=notice,
            engine=engine,
        )


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "WenjingEngine/1.4"

    def do_POST(self):
        if self.path == "/api/process":
            self.handle_process()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_GET(self):
        if self.path == "/healthz":
            self.respond_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path == "/api/version":
            self.respond_json(HTTPStatus.OK, get_app_version())
            return
        if self.path.startswith("/api/download/"):
            self.handle_download()
            return
        if self.path.startswith("/api/job/"):
            self.handle_job_status()
            return
        super().do_GET()

    def handle_process(self):
        try:
            content_type = self.headers.get("content-type", "")
            if "multipart/form-data" not in content_type.lower():
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "请求格式必须是 multipart/form-data"})
                return

            content_length = int(self.headers.get("content-length", "0"))
            if content_length > MAX_FILE_BYTES + (1024 * 512):
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "文件过大，最大 25MB"})
                return

            body = self.rfile.read(content_length)
            fields, files = parse_multipart_form_data(body, content_type)

            file_item = files.get("file")
            if file_item is None or not file_item.get("filename"):
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "请上传文件"})
                return

            level = fields.get("level", "标准")
            filename = safe_name(file_item["filename"])
            file_data = file_item["data"]

            if not file_data:
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "文件内容为空"})
                return
            if len(file_data) > MAX_FILE_BYTES:
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "文件过大，最大 25MB"})
                return

            upload_id = uuid.uuid4().hex[:12]
            saved_input = UPLOAD_DIR / f"{upload_id}_{filename}"
            saved_output = OUTPUT_DIR / f"{upload_id}_optimized.txt"

            with saved_input.open("wb") as f:
                f.write(file_data)

            try:
                source_text = file_to_text(file_data, filename)
            except Exception as parse_exc:
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": f"文档解析失败：{parse_exc}"})
                return

            if not source_text:
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "未提取到可处理文本内容"})
                return

            job_id = create_job()
            update_job(job_id, progress=5, message="任务已入队，等待处理...")

            worker = threading.Thread(
                target=process_document_job,
                args=(job_id, source_text, level, saved_output),
                daemon=True,
            )
            worker.start()

            self.respond_json(
                HTTPStatus.ACCEPTED,
                {
                    "message": "任务已提交",
                    "job_id": job_id,
                    "status": "queued",
                    "progress": 5,
                },
            )
        except Exception as exc:
            self.respond_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"服务端异常: {exc}"})

    def handle_job_status(self):
        job_id = unquote(self.path.replace("/api/job/", "", 1)).strip()
        if not job_id:
            self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "缺少 job_id"})
            return

        job = get_job(job_id)
        if not job:
            self.respond_json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
            return

        self.respond_json(HTTPStatus.OK, job)

    def handle_download(self):
        token = unquote(self.path.replace("/api/download/", "", 1)).strip()
        if not token:
            self.send_error(HTTPStatus.BAD_REQUEST, "Bad Request")
            return

        file_path = OUTPUT_DIR / Path(token).name
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File Not Found")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{file_path.name}"')
        self.send_header("Content-Length", str(file_path.stat().st_size))
        self.end_headers()

        with file_path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def respond_json(self, status: int, payload: dict):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format, *args):
        return


def run(host: str | None = None, port: int | None = None):
    host = host or os.getenv("HOST", "0.0.0.0")
    port = int(port or os.getenv("PORT", "8000"))
    os.chdir(BASE_DIR)
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Server running at http://{host}:{port}")
    print("Press Ctrl+C to stop")
    server.serve_forever()


if __name__ == "__main__":
    run()
