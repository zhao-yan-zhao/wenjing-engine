import io
import json
import os
import re
import secrets
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
from hashlib import pbkdf2_hmac
from http import HTTPStatus
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
DATA_DIR = BASE_DIR / "data"
USERS_FILE = DATA_DIR / "users.json"

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_AI_CHARS = 12000
AI_CHUNK_SIZE = 10000
AI_CHUNK_OVERLAP = 400

UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
DATA_DIR.mkdir(exist_ok=True)

JOBS = {}
JOBS_LOCK = threading.Lock()
SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
USERS_LOCK = threading.Lock()


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


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

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").strip().lower()
OPENAI_API_URL = os.getenv("OPENAI_API_URL", "https://api.openai.com/v1/responses")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.5")
DEEPSEEK_API_URL = os.getenv("DEEPSEEK_API_URL", "https://api.deepseek.com/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123456")


def read_json_file(path: Path, default_value):
    if not path.exists():
        return default_value
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_value


def write_json_file(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def hash_password(password: str, salt_hex: str | None = None) -> str:
    if salt_hex is None:
        salt_hex = secrets.token_hex(16)
    salt = bytes.fromhex(salt_hex)
    key = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120000)
    return f"{salt_hex}${key.hex()}"


def verify_password(password: str, hashed: str) -> bool:
    if "$" not in hashed:
        return False
    salt_hex, key_hex = hashed.split("$", 1)
    calc = hash_password(password, salt_hex)
    return calc.split("$", 1)[1] == key_hex


def ensure_default_admin() -> None:
    with USERS_LOCK:
        users = read_json_file(USERS_FILE, [])
        if any(u.get("username") == ADMIN_USERNAME for u in users):
            return

        users.append(
            {
                "username": ADMIN_USERNAME,
                "password_hash": hash_password(ADMIN_PASSWORD),
                "role": "admin",
                "created_at": now_str(),
            }
        )
        write_json_file(USERS_FILE, users)


def get_user(username: str):
    with USERS_LOCK:
        users = read_json_file(USERS_FILE, [])
        for user in users:
            if user.get("username") == username:
                return user
    return None


def create_user(username: str, password: str) -> tuple[bool, str]:
    if not re.match(r"^[a-zA-Z0-9_]{3,24}$", username):
        return False, "用户名需为 3-24 位字母数字下划线"
    if len(password) < 6:
        return False, "密码至少 6 位"

    with USERS_LOCK:
        users = read_json_file(USERS_FILE, [])
        if any(u.get("username") == username for u in users):
            return False, "用户名已存在"

        users.append(
            {
                "username": username,
                "password_hash": hash_password(password),
                "role": "user",
                "created_at": now_str(),
            }
        )
        write_json_file(USERS_FILE, users)
    return True, "注册成功"


def list_users_safe():
    with USERS_LOCK:
        users = read_json_file(USERS_FILE, [])
    result = []
    for user in users:
        result.append(
            {
                "username": user.get("username", ""),
                "role": user.get("role", "user"),
                "created_at": user.get("created_at", ""),
            }
        )
    return result


def create_session(username: str, role: str) -> str:
    token = secrets.token_urlsafe(32)
    with SESSIONS_LOCK:
        SESSIONS[token] = {
            "username": username,
            "role": role,
            "created_at": now_str(),
        }
    return token


def get_auth_token(headers) -> str | None:
    auth = headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    token = headers.get("X-Auth-Token", "").strip()
    if token:
        return token
    return None


def get_session_user(headers):
    token = get_auth_token(headers)
    if not token:
        return None

    with SESSIONS_LOCK:
        session = SESSIONS.get(token)
        if not session:
            return None
        return {
            "token": token,
            "username": session.get("username", ""),
            "role": session.get("role", "user"),
        }


def destroy_session(token: str) -> None:
    with SESSIONS_LOCK:
        if token in SESSIONS:
            del SESSIONS[token]


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

    model_name = DEEPSEEK_MODEL if LLM_PROVIDER == "deepseek" else OPENAI_MODEL
    return {
        "commit": commit[:7] if commit and commit != "unknown" else commit,
        "provider": LLM_PROVIDER,
        "model": model_name,
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


def parse_json_body(handler) -> dict:
    content_length = int(handler.headers.get("content-length", "0"))
    if content_length <= 0:
        return {}
    raw = handler.rfile.read(content_length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def create_job(username: str, level: str, filename: str) -> str:
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
            "aigc_before": None,
            "aigc_after": None,
            "aigc_drop": None,
            "rewrite_targets": 0,
            "submitted_by": username,
            "level": level,
            "source_filename": filename,
            "created_at": now_str(),
            "updated_at": now_str(),
        }
    return job_id


def update_job(job_id: str, **kwargs) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job.update(kwargs)
        job["updated_at"] = now_str()


def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return dict(job) if job else None


def list_jobs_for_user(username: str):
    with JOBS_LOCK:
        jobs = [dict(v) for v in JOBS.values() if v.get("submitted_by") == username]
    jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jobs


def list_all_jobs():
    with JOBS_LOCK:
        jobs = [dict(v) for v in JOBS.values()]
    jobs.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return jobs


def analyze_text_risk(text: str) -> dict:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    if not normalized:
        return {
            "score": 0,
            "label": "无内容",
            "signals": ["文本为空"],
            "flagged_sentences": [],
        }

    sentences = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", normalized) if s.strip()]
    if not sentences:
        sentences = [normalized]

    total = len(sentences)
    unique = len(set(sentences))
    repeated_ratio = 1 - (unique / total)

    lengths = [len(s) for s in sentences]
    avg_len = sum(lengths) / len(lengths)
    variance = sum((x - avg_len) ** 2 for x in lengths) / len(lengths)
    std_len = variance ** 0.5

    template_phrases = [
        "综上所述",
        "不难发现",
        "值得注意的是",
        "由此可见",
        "因此可以看出",
        "首先",
        "其次",
        "最后",
        "总而言之",
    ]
    template_hits = sum(normalized.count(p) for p in template_phrases)

    connectors = ["因此", "然而", "此外", "同时", "进一步", "总之", "总体来看"]
    connector_hits = sum(normalized.count(c) for c in connectors)

    score = 18
    score += min(32, repeated_ratio * 100 * 0.9)
    score += min(20, max(0, 8 - std_len) * 2.5)
    score += min(18, template_hits * 2)
    score += min(12, connector_hits * 0.8)
    score = int(max(5, min(95, score)))

    if score >= 75:
        label = "高疑似"
    elif score >= 50:
        label = "中疑似"
    else:
        label = "低疑似"

    sentence_scores = []
    repeated_sentences = set([x for x in sentences if sentences.count(x) > 1])
    for s in sentences:
        s_score = 20
        s_score += 15 if len(s) > 45 else 0
        s_score += 20 if any(tp in s for tp in template_phrases) else 0
        s_score += 15 if s in repeated_sentences else 0
        s_score += 10 if s.count("，") >= 4 else 0
        sentence_scores.append((s, min(95, s_score)))

    sentence_scores.sort(key=lambda x: x[1], reverse=True)
    flagged = [{"text": s, "score": sc} for s, sc in sentence_scores[:6] if sc >= 45]

    signals = [
        f"句子重复率约 {repeated_ratio * 100:.1f}%",
        f"句长波动标准差 {std_len:.1f}",
        f"模板化短语命中 {template_hits} 次",
    ]

    return {
        "score": score,
        "label": label,
        "signals": signals,
        "flagged_sentences": flagged,
    }


def split_paragraphs(text: str) -> list[str]:
    raw = (text or "").replace("\r\n", "\n")
    parts = re.split(r"\n\s*\n", raw)
    return [p.strip() for p in parts if p.strip()]


def choose_target_paragraphs(paragraphs: list[str], level: str) -> list[int]:
    if not paragraphs:
        return []

    threshold_map = {"标准": 62, "增强": 54, "深度": 46}
    max_targets_map = {"标准": 8, "增强": 16, "深度": 28}

    threshold = threshold_map.get(level, 62)
    max_targets = max_targets_map.get(level, 8)

    scored = []
    for idx, para in enumerate(paragraphs):
        risk = analyze_text_risk(para).get("score", 0)
        scored.append((idx, risk))

    candidates = [x for x in scored if x[1] >= threshold]
    if not candidates:
        top = sorted(scored, key=lambda x: x[1], reverse=True)[:1]
        return [x[0] for x in top]

    candidates.sort(key=lambda x: x[1], reverse=True)
    picked = [idx for idx, _ in candidates[:max_targets]]
    picked.sort()
    return picked


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

    level_rules = {
        "标准": "轻度润色，保留原句结构。",
        "增强": "中度重写，优化衔接与细节表达。",
        "深度": "深度重写，强化学术表达与逻辑推导。",
    }

    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "developer",
                "content": (
                    "你是学术写作优化助手。"
                    "请仅输出优化后的正文，不要加标题、注释、Markdown、前后说明。"
                    "保留事实、数据、引用关系，不要虚构内容。"
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
    return extract_output_text(response_json), OPENAI_MODEL


def optimize_text_with_deepseek(text: str, level: str) -> tuple[str, str]:
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("未配置 DEEPSEEK_API_KEY")

    level_rules = {
        "标准": "轻度润色，保留原句结构。",
        "增强": "中度重写，优化衔接与细节表达。",
        "深度": "深度重写，强化学术表达与逻辑推导。",
    }

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是学术写作优化助手。"
                    "请仅输出优化后的正文，不要加标题、注释、Markdown、前后说明。"
                    "保留事实、数据、引用关系，不要虚构内容。"
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
        "stream": False,
    }

    req = urllib.request.Request(
        DEEPSEEK_API_URL,
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
        raise RuntimeError(f"DeepSeek 接口错误：{message}") from http_err
    except Exception as exc:
        raise RuntimeError(f"DeepSeek 请求失败：{exc}") from exc

    response_json = json.loads(raw.decode("utf-8"))
    choices = response_json.get("choices", [])
    if not choices:
        raise RuntimeError("DeepSeek 返回内容为空")
    content = choices[0].get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("DeepSeek 返回内容为空")

    return content.strip(), DEEPSEEK_MODEL


def optimize_text_with_provider(text: str, level: str) -> tuple[str, str]:
    if not text.strip():
        raise RuntimeError("文本为空")

    if LLM_PROVIDER == "deepseek":
        out, model = optimize_text_with_deepseek(text, level)
        return out, f"deepseek:{model}"

    if LLM_PROVIDER == "openai":
        out, model = optimize_text_with_openai(text, level)
        return out, f"openai:{model}"

    raise RuntimeError(f"不支持的 LLM_PROVIDER: {LLM_PROVIDER}")


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


def optimize_text_with_provider_chunked(text: str, level: str, progress_callback=None) -> tuple[str, str]:
    chunks = split_text_into_chunks(text)
    result_parts = []
    engine_name = ""

    for idx, chunk in enumerate(chunks, start=1):
        if progress_callback:
            progress_callback(idx, len(chunks))

        if len(chunk) > MAX_AI_CHARS:
            raise RuntimeError(f"分段后仍超限：第 {idx} 段 {len(chunk)} 字符")

        optimized, engine_name = optimize_text_with_provider(chunk, level)
        result_parts.append(optimized.strip())

    return "\n\n".join(result_parts).strip(), engine_name


def process_document_job(job_id: str, source_text: str, level: str, saved_output: Path) -> None:
    before_risk = analyze_text_risk(source_text)
    update_job(
        job_id,
        status="processing",
        progress=10,
        message="正在分析 AIGC 风险...",
        aigc_before=before_risk,
    )

    paragraphs = split_paragraphs(source_text)
    target_indexes = choose_target_paragraphs(paragraphs, level)
    update_job(
        job_id,
        progress=16,
        message=f"已锁定 {len(target_indexes)} 个高风险段落，准备降重...",
        rewrite_targets=len(target_indexes),
    )

    engine = "rule-fallback"
    notice = ""
    rewritten = list(paragraphs)

    try:
        if not target_indexes:
            target_indexes = [0] if paragraphs else []

        for i, para_idx in enumerate(target_indexes, start=1):
            para_text = rewritten[para_idx]
            if len(para_text) <= MAX_AI_CHARS:
                new_text, engine = optimize_text_with_provider(para_text, level)
            else:
                new_text, engine = optimize_text_with_provider_chunked(para_text, level, None)

            rewritten[para_idx] = new_text.strip()
            ratio = i / max(len(target_indexes), 1)
            pct = 20 + int(ratio * 65)
            update_job(job_id, progress=min(pct, 90), message=f"降重处理中：第 {i}/{len(target_indexes)} 段")

        optimized_body = "\n\n".join(rewritten).strip()
        if len(target_indexes) > 1:
            notice = f"采用定向降重：共改写 {len(target_indexes)} 个风险段落"
    except Exception as ai_exc:
        if rewritten:
            for idx in target_indexes:
                rewritten[idx] = optimize_text_rule(rewritten[idx], level).strip()
            optimized_body = "\n\n".join(rewritten).strip()
        else:
            optimized_body = optimize_text_rule(source_text, level).strip()

        engine = "rule-fallback"
        notice = f"AI 不可用，已回退规则引擎：{ai_exc}"
        update_job(job_id, progress=86, message="AI 不可用，正在回退规则引擎...")

    after_risk = analyze_text_risk(optimized_body)
    drop_value = max(0, before_risk.get("score", 0) - after_risk.get("score", 0))

    try:
        result_text = (
            "【文净引擎处理结果】\n"
            f"处理时间：{now_str()}\n"
            f"优化强度：{level}\n"
            f"处理引擎：{engine}\n"
            f"AIGC疑似率(估计)：{before_risk.get('score', 0)} -> {after_risk.get('score', 0)}（下降 {drop_value}）\n"
            "----------------------------------------\n"
            f"{optimized_body}\n"
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
            aigc_after=after_risk,
            aigc_drop=drop_value,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            progress=100,
            message="结果写入失败",
            error=str(exc),
            engine=engine,
            notice=notice,
        )


class AppHandler(SimpleHTTPRequestHandler):
    server_version = "WenjingEngine/2.0"

    def do_POST(self):
        if self.path == "/api/register":
            self.handle_register()
            return
        if self.path == "/api/login":
            self.handle_login()
            return
        if self.path == "/api/logout":
            self.handle_logout()
            return
        if self.path == "/api/process":
            self.handle_process()
            return
        if self.path == "/api/aigc/check":
            self.handle_aigc_check()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def do_GET(self):
        if self.path == "/healthz":
            self.respond_json(HTTPStatus.OK, {"ok": True})
            return
        if self.path == "/api/version":
            self.respond_json(HTTPStatus.OK, get_app_version())
            return
        if self.path == "/api/me":
            self.handle_me()
            return
        if self.path == "/api/my/jobs":
            self.handle_my_jobs()
            return
        if self.path == "/api/admin/users":
            self.handle_admin_users()
            return
        if self.path == "/api/admin/jobs":
            self.handle_admin_jobs()
            return
        if self.path == "/api/admin/stats":
            self.handle_admin_stats()
            return
        if self.path.startswith("/api/job/"):
            self.handle_job_status()
            return
        if self.path.startswith("/api/download/"):
            self.handle_download()
            return
        super().do_GET()

    def current_user(self):
        return get_session_user(self.headers)

    def require_user(self):
        user = self.current_user()
        if not user:
            self.respond_json(HTTPStatus.UNAUTHORIZED, {"error": "请先登录"})
            return None
        return user

    def require_admin(self):
        user = self.require_user()
        if not user:
            return None
        if user.get("role") != "admin":
            self.respond_json(HTTPStatus.FORBIDDEN, {"error": "需要管理员权限"})
            return None
        return user

    def handle_register(self):
        try:
            payload = parse_json_body(self)
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))

            ok, msg = create_user(username, password)
            if not ok:
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": msg})
                return

            self.respond_json(HTTPStatus.OK, {"message": msg})
        except Exception as exc:
            self.respond_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"注册失败: {exc}"})

    def handle_login(self):
        try:
            payload = parse_json_body(self)
            username = str(payload.get("username", "")).strip()
            password = str(payload.get("password", ""))

            user = get_user(username)
            if not user or not verify_password(password, user.get("password_hash", "")):
                self.respond_json(HTTPStatus.UNAUTHORIZED, {"error": "用户名或密码错误"})
                return

            token = create_session(username, user.get("role", "user"))
            self.respond_json(
                HTTPStatus.OK,
                {
                    "message": "登录成功",
                    "token": token,
                    "user": {
                        "username": username,
                        "role": user.get("role", "user"),
                    },
                },
            )
        except Exception as exc:
            self.respond_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"登录失败: {exc}"})

    def handle_logout(self):
        user = self.require_user()
        if not user:
            return
        destroy_session(user.get("token", ""))
        self.respond_json(HTTPStatus.OK, {"message": "已退出登录"})

    def handle_me(self):
        user = self.require_user()
        if not user:
            return
        self.respond_json(
            HTTPStatus.OK,
            {
                "username": user.get("username"),
                "role": user.get("role"),
            },
        )

    def handle_aigc_check(self):
        user = self.require_user()
        if not user:
            return

        try:
            payload = parse_json_body(self)
            text = str(payload.get("text", "")).strip()
            if not text:
                self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "text 不能为空"})
                return

            self.respond_json(HTTPStatus.OK, analyze_text_risk(text))
        except Exception as exc:
            self.respond_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"检测失败: {exc}"})

    def handle_process(self):
        user = self.require_user()
        if not user:
            return

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

            job_id = create_job(user.get("username", ""), level, filename)
            update_job(job_id, progress=6, message="任务已入队，等待处理...")

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
                    "progress": 6,
                },
            )
        except Exception as exc:
            self.respond_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"服务端异常: {exc}"})

    def handle_job_status(self):
        user = self.require_user()
        if not user:
            return

        job_id = unquote(self.path.replace("/api/job/", "", 1)).strip()
        if not job_id:
            self.respond_json(HTTPStatus.BAD_REQUEST, {"error": "缺少 job_id"})
            return

        job = get_job(job_id)
        if not job:
            self.respond_json(HTTPStatus.NOT_FOUND, {"error": "任务不存在"})
            return

        if user.get("role") != "admin" and job.get("submitted_by") != user.get("username"):
            self.respond_json(HTTPStatus.FORBIDDEN, {"error": "无权访问该任务"})
            return

        self.respond_json(HTTPStatus.OK, job)

    def handle_my_jobs(self):
        user = self.require_user()
        if not user:
            return
        jobs = list_jobs_for_user(user.get("username", ""))[:20]
        self.respond_json(HTTPStatus.OK, {"items": jobs})

    def handle_admin_users(self):
        if not self.require_admin():
            return
        self.respond_json(HTTPStatus.OK, {"items": list_users_safe()})

    def handle_admin_jobs(self):
        if not self.require_admin():
            return
        self.respond_json(HTTPStatus.OK, {"items": list_all_jobs()[:100]})

    def handle_admin_stats(self):
        if not self.require_admin():
            return

        users = list_users_safe()
        jobs = list_all_jobs()
        completed = [j for j in jobs if j.get("status") == "completed"]
        avg_drop = 0
        if completed:
            avg_drop = round(sum(j.get("aigc_drop") or 0 for j in completed) / len(completed), 2)

        self.respond_json(
            HTTPStatus.OK,
            {
                "users": len(users),
                "jobs_total": len(jobs),
                "jobs_completed": len(completed),
                "avg_aigc_drop": avg_drop,
            },
        )

    def handle_download(self):
        user = self.require_user()
        if not user:
            return

        token = unquote(self.path.replace("/api/download/", "", 1)).strip()
        if not token:
            self.send_error(HTTPStatus.BAD_REQUEST, "Bad Request")
            return

        file_path = OUTPUT_DIR / Path(token).name
        if not file_path.exists() or not file_path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND, "File Not Found")
            return

        related_job = None
        with JOBS_LOCK:
            for v in JOBS.values():
                if v.get("output_name") == file_path.name:
                    related_job = dict(v)
                    break

        if related_job and user.get("role") != "admin":
            if related_job.get("submitted_by") != user.get("username"):
                self.respond_json(HTTPStatus.FORBIDDEN, {"error": "无权下载该文件"})
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
    ensure_default_admin()

    host = host or os.getenv("HOST", "0.0.0.0")
    port = int(port or os.getenv("PORT", "8000"))
    os.chdir(BASE_DIR)
    server = ThreadingHTTPServer((host, port), AppHandler)
    print(f"Server running at http://{host}:{port}")
    print(f"Provider: {LLM_PROVIDER}")
    print("Press Ctrl+C to stop")
    server.serve_forever()


if __name__ == "__main__":
    run()
