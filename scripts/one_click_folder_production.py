#!/usr/bin/env python3
"""Folder-to-production launcher with isolated scan failures and guarded submission.

Default behavior is a read-only dry run. A real run requires an explicit file
selection plus --submit --confirm-submit, or the interactive SUBMIT confirmation.
"""
from __future__ import annotations
import argparse, copy, hashlib, json, math, mimetypes, os, re, sys, time, uuid, zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

DEFAULT_ROOT = Path(r"D:\脚本")
DEFAULT_API = os.environ.get("VIDEO_FACTORY_API_URL", "http://127.0.0.1:8765").rstrip("/")
DEFAULT_STATE_FILE = Path(__file__).resolve().parents[1] / "data" / "folder_production_state.json"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
DOCX_MAX_BYTES = 64 * 1024 * 1024
IMAGE_MAX_BYTES = 10 * 1024 * 1024
QUALITY = {"resolution": "4k", "mode": "fast", "processing": "ai_super_resolution", "aspect_ratio": "16:9"}
SUPPORTED_ASPECT_RATIOS = ("16:9", "9:16", "1:1")
PROCESSING_PLANS = {
    "lanczos": ("plans", "普通缩放"),
    "ai_super_resolution": ("ai_plans", "Real-ESRGAN 本机 AI 超分"),
    "seedvr2": ("seedvr2_plans", "SeedVR2"),
    "native": ("native_plans", "原生画质"),
    "reconstruction": ("reconstruction_plans", "视频重建"),
}

class WorkflowError(RuntimeError):
    def __init__(self, message: str, *, phase: str = "", details: Any = None, outcome_unknown: bool = False):
        super().__init__(message); self.phase = phase; self.details = details; self.outcome_unknown = outcome_unknown

@dataclass(frozen=True)
class ImageAsset:
    path: Path
    sha256: str
    size: int
    mime: str

@dataclass
class Candidate:
    path: Path
    relative_path: str
    sha256: str
    size: int
    text: str
    images: list[ImageAsset] = field(default_factory=list)
    product_match: bool = False
    product_reason: str = ""
    issues: list[dict[str, str]] = field(default_factory=list)
    duplicate_images: list[str] = field(default_factory=list)

@dataclass
class ScanResult:
    candidates: list[Candidate]
    rejected: list[dict[str, str]]

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""): digest.update(chunk)
    return digest.hexdigest()

def _inside(path: Path, root: Path) -> bool:
    try: path.resolve().relative_to(root.resolve()); return True
    except (OSError, ValueError): return False

def read_docx(path: Path) -> tuple[bytes, str]:
    try:
        size = path.stat().st_size
        if size <= 0 or size > DOCX_MAX_BYTES: raise WorkflowError(f"DOCX 大小超出 1 byte 至 {DOCX_MAX_BYTES} byte：{path.name}", phase="scan")
        raw = path.read_bytes()
        with zipfile.ZipFile(__import__("io").BytesIO(raw)) as archive:
            bad_member = archive.testzip()
            if bad_member: raise WorkflowError(f"DOCX ZIP 内容损坏（{bad_member}）：{path.name}", phase="scan")
            xml = archive.read("word/document.xml")
    except WorkflowError: raise
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError) as exc:
        raise WorkflowError(f"无法读取有效 DOCX：{path.name}（{type(exc).__name__}）", phase="scan") from exc
    try: root = ET.fromstring(xml)
    except ET.ParseError as exc: raise WorkflowError(f"Word 正文 XML 损坏：{path.name}", phase="scan") from exc
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}; paragraphs = []
    for paragraph in root.findall(".//w:p", ns):
        pieces = []
        for node in paragraph.iter():
            if node.tag.endswith("}t") and node.text: pieces.append(node.text)
            elif node.tag.endswith("}tab"): pieces.append("\t")
            elif node.tag.endswith("}br"): pieces.append("\n")
        value = "".join(pieces).strip()
        if value: paragraphs.append(value)
    return raw, "\n".join(paragraphs)

def inspect_image(path: Path) -> ImageAsset:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream: header = stream.read(16)
    except OSError as exc: raise WorkflowError(f"产品图片不可读：{path.name}", phase="scan") from exc
    if not 0 < size <= IMAGE_MAX_BYTES: raise WorkflowError(f"图片大小超出 1 byte 至 {IMAGE_MAX_BYTES} byte：{path.name}", phase="scan")
    suffix = path.suffix.casefold()
    valid = (suffix == ".png" and header.startswith(b"\x89PNG\r\n\x1a\n")
        or suffix in {".jpg", ".jpeg"} and header.startswith(b"\xff\xd8\xff")
        or suffix == ".webp" and len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP")
    if not valid: raise WorkflowError(f"图片扩展名与真实格式不符：{path.name}", phase="scan")
    return ImageAsset(path, sha256_file(path), size, mimetypes.guess_type(path.name)[0] or "application/octet-stream")

def match_product(path: Path, text: str, *, brand: str | None = None, product_name: str | None = None) -> tuple[bool, str]:
    """Use explicit product mentions when present; folder assets are bound separately."""
    haystack = "\n".join((path.stem, path.parent.name, text)).casefold()
    brand_hit = bool(brand and brand.strip() and brand.strip().casefold() in haystack)
    model_hit = bool(product_name and product_name.strip() and product_name.strip().casefold() in haystack)
    if brand_hit or model_hit:
        return True, "剧本明确包含命令行指定的品牌或产品名称"
    if "owala" in haystack and "freesip" in haystack:
        return True, "剧本文字明确包含 Owala FreeSip"
    return False, "剧本未明确标注具体产品；若同目录存在图片，将按文件夹产品组绑定"


def product_identity(candidate: Candidate, *, product_name: str | None = None, brand: str | None = None) -> tuple[str, str]:
    """Resolve one stable product-library identity per source folder without inventing a brand."""
    haystack = "\n".join((candidate.path.stem, candidate.path.parent.name, candidate.text)).casefold()
    if product_name and product_name.strip():
        resolved_name = product_name.strip()
    elif "owala" in haystack and "freesip" in haystack:
        resolved_name = "Owala FreeSip"
    else:
        group = Path(candidate.relative_path).parent.as_posix().strip("./")
        resolved_name = group or candidate.path.stem
    resolved_brand = brand.strip() if brand and brand.strip() else ("Owala" if "owala" in haystack and "freesip" in haystack else "")
    return resolved_name[:200], resolved_brand[:200]

def resolve_aspect_ratio(text: str, requested: str = "auto") -> str:
    """Use an explicit CLI override or preserve the ratio stated in the screenplay."""
    if requested != "auto":
        if requested not in SUPPORTED_ASPECT_RATIOS:
            raise WorkflowError(f"不支持的画幅：{requested}", phase="quality")
        return requested
    ratios = set()
    for value in re.findall(r"(?<!\d)(16\s*[:：]\s*9|9\s*[:：]\s*16|1\s*[:：]\s*1)(?!\d)", text):
        left, right = (int(part.strip()) for part in re.split(r"[:：]", value))
        ratios.add(f"{left}:{right}")
    # Dimensions are a fallback when the screenplay gives pixels but no ratio.
    if not ratios:
        dimensions = re.findall(r"(?<!\d)(\d{3,5})\s*[xX×]\s*(\d{3,5})(?!\d)", text)
        for width, height in dimensions:
            divisor = math.gcd(int(width), int(height))
            ratio = f"{int(width) // divisor}:{int(height) // divisor}"
            if ratio in SUPPORTED_ASPECT_RATIOS:
                ratios.add(ratio)
    if len(ratios) > 1:
        raise WorkflowError("剧本包含互相冲突的输出画幅，未导入或提交：" + ", ".join(sorted(ratios)), phase="quality")
    return next(iter(ratios), QUALITY["aspect_ratio"])

def quality_for_candidate(candidate: Candidate, *, resolution: str, mode: str, processing: str,
                          requested_aspect_ratio: str = "auto") -> dict[str, str]:
    return {"resolution": resolution, "mode": mode, "processing": processing,
        "aspect_ratio": resolve_aspect_ratio(candidate.text, requested_aspect_ratio)}

def context_native_canvas(aspect_ratio: str, megapixels: float = 0.59) -> tuple[int, int]:
    """Match the H3 Context-IR 32-pixel grid at the selected portrait/landscape ratio."""
    if aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
        raise WorkflowError(f"Context-IR 不支持画幅 {aspect_ratio}", phase="quality")
    width_ratio, height_ratio = (int(part) for part in aspect_ratio.split(":"))
    ratio = width_ratio / height_ratio
    pixels = round(megapixels * 1_000_000, 2)
    native_height = math.sqrt(pixels / ratio)
    width = max(32, round(native_height * ratio / 32) * 32)
    height = max(32, round(native_height / 32) * 32)
    return width, height

def _image_files(directory: Path) -> Iterable[Path]:
    try: entries = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
    except OSError: return ()
    return (path for path in entries if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS)

def inspect_candidate(path: Path, root: Path, *, brand: str | None, product_name: str | None) -> Candidate:
    raw, text = read_docx(path); matched, reason = match_product(path, text, brand=brand, product_name=product_name)
    candidate = Candidate(path, path.relative_to(root).as_posix(), sha256_bytes(raw), len(raw), text,
                          product_match=matched, product_reason=reason)
    seen = set()
    for image_path in _image_files(path.parent):
        try:
            if not _inside(image_path, root): raise WorkflowError(f"图片解析后指向脚本根目录之外：{image_path.name}", phase="scan")
            image = inspect_image(image_path)
            if image.sha256 in seen: candidate.duplicate_images.append(image_path.name); continue
            seen.add(image.sha256); candidate.images.append(image)
        except WorkflowError as exc: candidate.issues.append({"file": image_path.name, "error": str(exc)})
    if candidate.images and not candidate.product_match:
        candidate.product_match = True
        candidate.product_reason = "同目录有效图片按文件夹约定视为共享产品参考图"
    return candidate

def scan(root: Path, *, brand: str | None = None, product_name: str | None = None) -> ScanResult:
    root = root.resolve()
    if not root.is_dir(): raise WorkflowError(f"脚本目录不存在：{root}", phase="scan")
    try:
        paths = sorted((p for p in root.rglob("*") if p.is_file() and p.suffix.casefold() == ".docx"
            and not p.name.startswith("~$") and not any(part.startswith(".") for part in p.relative_to(root).parts)
            and _inside(p, root)), key=lambda p: p.as_posix().casefold())
    except OSError as exc: raise WorkflowError(f"扫描脚本目录失败：{exc}", phase="scan") from exc
    if not paths: raise WorkflowError(f"{root} 中没有可扫描的 .docx 文件", phase="scan")
    candidates, rejected = [], []
    for path in paths:
        try: candidates.append(inspect_candidate(path, root, brand=brand, product_name=product_name))
        except WorkflowError as exc: rejected.append({"file": path.relative_to(root).as_posix(), "error": str(exc)})
        except Exception as exc: rejected.append({"file": path.relative_to(root).as_posix(), "error": f"扫描异常 {type(exc).__name__}: {exc}"})
    return ScanResult(candidates, rejected)

class Api:
    def __init__(self, base: str, timeout: float = 180): self.base = base.rstrip("/"); self.timeout = timeout
    def request(self, method: str, path: str, *, body: bytes | None = None, content_type: str | None = None, read_only: bool = False) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "VideoFactoryFolderLauncher/1.1"}
        if content_type: headers["Content-Type"] = content_type
        request = Request(self.base + path, data=body, headers=headers, method=method)
        for attempt in range(3 if read_only else 1):
            try:
                with urlopen(request, timeout=self.timeout) as response: raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
            except HTTPError as exc:
                text = exc.read(256000).decode("utf-8", errors="replace")
                try:
                    obj = json.loads(text); detail = obj.get("detail", obj.get("message", obj)) if isinstance(obj, dict) else obj
                    message = str(detail.get("message") or detail.get("error") or detail) if isinstance(detail, dict) else str(detail)
                except (json.JSONDecodeError, TypeError): message = text[:1200]
                raise WorkflowError(f"API {method} {path} 返回 HTTP {exc.code}: {message}", phase="api",
                    details={"http_status": exc.code}, outcome_unknown=not read_only and (exc.code == 408 or exc.code >= 500)) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if read_only and attempt < 2: time.sleep(.4 * (attempt + 1)); continue
                raise WorkflowError(f"API {method} {path} 连接失败；写操作不会自动重试：{exc}", phase="api", outcome_unknown=not read_only) from exc
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WorkflowError(f"API {method} {path} 返回无效 JSON", phase="api", outcome_unknown=not read_only) from exc
        raise WorkflowError(f"API {method} {path} 请求失败", phase="api")
    def get(self, path: str) -> Any: return self.request("GET", path, read_only=True)
    def post_json(self, path: str, value: Any) -> Any:
        return self.request("POST", path, body=json.dumps(value, ensure_ascii=False).encode("utf-8"), content_type="application/json")
    def post_multipart(self, path: str, fields: dict[str, str], files: list[tuple[str, str, bytes, str]]) -> Any:
        boundary = "----VideoFactory" + uuid.uuid4().hex; parts = []
        for name, value in fields.items():
            parts.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(), value.encode("utf-8"), b"\r\n"))
        for field_name, filename, data, mime in files:
            safe_filename = Path(filename.replace("\\", "/")).name.replace('"', "_")
            parts.extend((f"--{boundary}\r\n".encode(), f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_filename}"\r\n'.encode("utf-8"), f"Content-Type: {mime}\r\n\r\n".encode(), data, b"\r\n"))
        parts.append(f"--{boundary}--\r\n".encode())
        return self.request("POST", path, body=b"".join(parts), content_type=f"multipart/form-data; boundary={boundary}")


class RunLedger:
    """Local idempotency journal; never stores API credentials or media contents."""
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        try:
            self.lock_fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.name == "nt":
                import msvcrt
                if os.fstat(self.lock_fd).st_size == 0: os.write(self.lock_fd, b"\0")
                os.lseek(self.lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(self.lock_fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            fd = getattr(self, "lock_fd", None)
            if fd is not None: os.close(fd); self.lock_fd = None
            raise WorkflowError(f"批量制作已由另一个进程占用：{self.lock_path}", phase="ledger") from exc
        self.entries: dict[str, dict[str, Any]] = {}
        try:
            if path.exists():
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(loaded, dict) or loaded.get("version") != 1 or not isinstance(loaded.get("entries"), dict):
                    raise ValueError("unknown ledger structure")
                self.entries = loaded["entries"]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            self.close()
            raise WorkflowError(f"批量防重复记录不可读取；为避免重复提交已停止：{path}", phase="ledger") from exc

    def close(self) -> None:
        fd = getattr(self, "lock_fd", None)
        if fd is None: return
        try:
            os.close(fd)
        finally:
            self.lock_fd = None

    def __enter__(self) -> "RunLedger": return self
    def __exit__(self, *_: Any) -> None: self.close()

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.entries.get(key)
        return value if isinstance(value, dict) else None

    def record(self, key: str, value: dict[str, Any]) -> None:
        self.entries[key] = {**value, "updated_at": datetime.now(timezone.utc).isoformat()}
        payload = json.dumps({"version": 1, "entries": self.entries}, ensure_ascii=False, indent=2)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, self.path)


def api_fingerprint(base: str) -> str:
    stable_identity = os.environ.get("VIDEO_FACTORY_API_ID", "").strip()
    if stable_identity:
        parsed_identity = urlsplit(stable_identity)
        if parsed_identity.scheme and parsed_identity.hostname:
            host = parsed_identity.hostname.casefold()
            if parsed_identity.port:
                host += f":{parsed_identity.port}"
            return f"{parsed_identity.scheme.casefold()}://{host}{parsed_identity.path.rstrip('/')}"
    parsed = urlsplit(base)
    # Strip user-info, path parameters, query and fragment so credentials never land in the journal.
    host = parsed.hostname or "unknown-host"
    if parsed.port: host += f":{parsed.port}"
    return f"{parsed.scheme.casefold()}://{host}{parsed.path.rstrip('/')}"


def find_prior_candidate_entry(entries: dict[str, dict[str, Any]], candidate: Candidate,
                               quality: dict[str, str]) -> tuple[str, dict[str, Any]] | None:
    """Recover an older ledger key when its API was reached through a new SSH port."""
    expected_images = sorted(image.sha256 for image in candidate.images)
    matches = [(key, value) for key, value in entries.items()
        if isinstance(value, dict)
        and value.get("script") == candidate.relative_path
        and value.get("script_sha256") == candidate.sha256
        and sorted(value.get("image_sha256") or []) == expected_images
        and value.get("quality") == quality]
    if len(matches) <= 1:
        return matches[0] if matches else None
    # Prefer a durable existing project over older preflight records or an
    # uncertain upload with no project ID. If several different projects were
    # created for the same exact input, refuse automatic recovery.
    project_matches = [(key, value) for key, value in matches if value.get("project_id")]
    project_ids = {value.get("project_id") for _, value in project_matches}
    if len(project_ids) == 1:
        return max(project_matches, key=lambda row: str(row[1].get("updated_at", "")))
    if len(project_ids) > 1:
        raise WorkflowError("同一 Word 与素材在旧台账中存在多条哈希相同记录；为避免重复生成，停止恢复", phase="ledger",
            details={"script": candidate.relative_path, "projects": sorted(project_ids),
                     "ledger_keys": [key for key, _ in project_matches]})
    unresolved = [(key, value) for key, value in matches
        if value.get("status") == "unknown" and value.get("stage") in {"import_pending", "import_response"}]
    if len(unresolved) == 1:
        return unresolved[0]
    if len(unresolved) > 1:
        endpoints = {value.get("api") for _, value in unresolved}
        if len(endpoints) > 1:
            raise WorkflowError("同一 Word 在多个 API 的导入结果未知；为避免串档或重复导入，停止恢复", phase="ledger",
                details={"script": candidate.relative_path, "ledger_keys": [key for key, _ in unresolved]})
        return max(unresolved, key=lambda row: str(row[1].get("updated_at", "")))
    return max(matches, key=lambda row: str(row[1].get("updated_at", "")))


def candidate_fingerprint(candidate: Candidate, quality: dict[str, str], api: Api) -> str:
    identity = {"path": candidate.relative_path, "docx_sha256": candidate.sha256,
        "images": sorted(image.sha256 for image in candidate.images), "quality": quality,
        "endpoint": api_fingerprint(api.base)}
    return hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

def quality_supported(api: Api, quality: dict[str, str]) -> tuple[bool, dict[str, Any]]:
    capabilities = api.get("/api/quality/capabilities"); ratio = quality["aspect_ratio"]; resolution = quality["resolution"]
    plan_key, label = PROCESSING_PLANS.get(quality.get("processing", ""), ("", "所选处理方式"))
    plans = capabilities.get(plan_key, []) if isinstance(capabilities, dict) and plan_key else []
    matching = [item for item in plans if item.get("requested", {}).get("resolution") == resolution and item.get("requested", {}).get("aspect_ratio") == ratio]
    if matching:
        evidence = copy.deepcopy(matching[-1])
        mode = quality.get("mode")
        mode_mapping = capabilities.get("modes", {}).get(mode, {}) if isinstance(capabilities, dict) else {}
        mode_steps = mode_mapping.get("steps") if isinstance(mode_mapping, dict) else None
        if isinstance(evidence.get("generation"), dict) and isinstance(mode_steps, int):
            # /quality/capabilities plans are built with the API's default
            # QualitySettings (high/30 steps); the selected run mode is a
            # separate exact mapping. Report the selected mode, not that
            # unrelated default, alongside the resolution/upscaler evidence.
            evidence["generation"]["steps"] = mode_steps
            evidence["requested_mode"] = mode
            evidence["mode_steps_verified"] = mode_steps == {"preview": 12, "standard": 20, "fast": 4, "high": 30}.get(mode)
        supported = evidence.get("supported") is True
        if quality.get("processing") == "ai_super_resolution":
            supported = supported and evidence.get("ai_super_resolution") is True and bool(evidence.get("postprocess_model"))
        return supported, evidence
    return False, {"supported": False, "reason": f"服务未返回 {label} 的 {resolution} · {ratio} 验收能力", "capability_count": len(plans)}

def generation_supported(api: Api, quality: dict[str, str], *, busy_wait_seconds: float = 600) -> tuple[bool, dict[str, Any]]:
    """Require the active local Context-IR route and the requested sampler mapping."""
    deadline = time.monotonic() + max(0.0, busy_wait_seconds)
    while True:
        health = api.get("/api/providers/h3/context-health")
        if not (isinstance(health, dict) and health.get("busy") is True and health.get("busy_reason") == "qwen"):
            break
        if time.monotonic() >= deadline:
            return False, {"health": health, "reason": "本地 Qwen 持续占用资源超过 10 分钟；未导入文档或提交视频任务"}
        time.sleep(min(2.0, max(0.1, deadline - time.monotonic())))
    expected_steps = {"preview": 12, "standard": 20, "fast": 4, "high": 30}.get(quality.get("mode"))
    capabilities = api.get("/api/quality/capabilities")
    mode = capabilities.get("modes", {}).get(quality.get("mode"), {}) if isinstance(capabilities, dict) else {}
    steps = mode.get("steps") if isinstance(mode, dict) else None
    result = {"health": health, "mode": quality.get("mode"), "steps": steps,
              "expected_steps": expected_steps, "workflow_route": "local Context-IR"}
    ready = (isinstance(health, dict) and health.get("provider") == "comfyui_h3_context_ir"
        and health.get("mode") == "real" and health.get("ready") is True
        and health.get("reference_to_video_available") is True
        and expected_steps is not None and steps == expected_steps)
    if not ready:
        result["reason"] = "本机 Context-IR 视频路由未就绪，或画质档位没有匹配的采样步数映射"
    return ready, result

def product_lookup(api: Api, name: str, brand: str) -> list[dict[str, Any]]:
    response = api.get("/api/product-library?q=" + quote(name)); items = response.get("items", []) if isinstance(response, dict) else []
    return [item for item in items if str(item.get("name", "")).casefold() == name.casefold()
            or (brand.casefold() in str(item.get("brand", "")).casefold() and "freesip" in str(item.get("name", "")).casefold())]

def dry_run_plan(candidate: Candidate, api: Api | None, quality: dict[str, str], product_name: str | None, brand: str | None) -> dict[str, Any]:
    resolved_name, resolved_brand = product_identity(candidate, product_name=product_name, brand=brand)
    result = {"script": candidate.relative_path, "script_sha256": candidate.sha256, "script_bytes": candidate.size,
        "product_match": candidate.product_match, "product_match_reason": candidate.product_reason,
        "product_group": Path(candidate.relative_path).parent.as_posix(), "product_name": resolved_name, "brand": resolved_brand,
        "product_images": [{"name": x.path.name, "sha256": x.sha256, "bytes": x.size, "mime": x.mime} for x in candidate.images],
        "duplicate_image_files_skipped": candidate.duplicate_images, "isolated_image_issues": candidate.issues,
        "quality": quality, "would_submit_model": False}
    if candidate.product_match and not candidate.images: result["blockers"] = ["脚本匹配产品但目录没有有效产品图；禁止空绑定"]
    if len(candidate.images) > 8: result.setdefault("blockers", []).append("同一产品组图片超过 8 张；产品参考 API 上限为 8，程序不会静默丢弃图片")
    if api is None: result["api"] = {"reachable": False, "reason": "未执行 API 只读预检"}; return result
    try: result["api"] = {"reachable": True, "health": api.get("/api/health")}
    except WorkflowError as exc: result["api"] = {"reachable": False, "reason": str(exc)}; return result
    try:
        ready, evidence = generation_supported(api, quality)
        result["generation_preflight"] = {"ready": ready, "evidence": evidence}
        if not ready:
            result.setdefault("blockers", []).append("本机 Context-IR/H3 工作流未就绪，未创建项目或提交模型")
    except WorkflowError as exc:
        result["generation_preflight"] = {"ready": False, "error": str(exc)}
        result.setdefault("blockers", []).append("无法核验本机 Context-IR/H3 工作流，未创建项目或提交模型")
    try:
        supported, evidence = quality_supported(api, quality); result["quality_preflight"] = {"supported": supported, "evidence": evidence}
        if not supported: result.setdefault("blockers", []).append("本机超分处理方式、画幅和分辨率尚无匹配验收能力；真实提交前阻断，不创建项目或任务")
    except WorkflowError as exc:
        result["quality_preflight"] = {"supported": False, "error": str(exc)}; result.setdefault("blockers", []).append("无法读取画质能力，真实提交前阻断")
    if candidate.product_match and candidate.images:
        try:
            matches = product_lookup(api, resolved_name, resolved_brand)
            result["product_gallery"] = {"matches": [{"id": x.get("id"), "name": x.get("name"), "brand": x.get("brand"), "known_image_hashes": [i.get("sha256") for i in x.get("images", [])]} for x in matches]}
            if len(matches) > 1: result.setdefault("blockers", []).append("产品图库存在多个候选，不能自动猜选")
        except WorkflowError as exc:
            result["product_gallery"] = {"error": str(exc)}; result.setdefault("blockers", []).append("无法核验图库，真实提交前阻断")
    return result

def _result(response: dict[str, Any]) -> dict[str, Any]:
    value = response.get("result"); return value if isinstance(value, dict) else response

def import_word(api: Api, candidate: Candidate, quality: dict[str, str]) -> dict[str, Any]:
    fields = {"auto_start": "false", "allow_cloud_costs": "false", "allow_paid_fallback": "false", "quality_json": json.dumps(quality, ensure_ascii=False)}
    data = candidate.path.read_bytes()
    return api.post_multipart("/api/studio/import", fields, [("file", candidate.path.name, data, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])

def ensure_product(api: Api, candidate: Candidate, *, product_name: str, brand: str) -> tuple[dict[str, Any], list[str]]:
    matches = product_lookup(api, product_name, brand)
    if len(matches) > 1: raise WorkflowError("产品图库有多个相似产品；请先手动指定唯一产品", phase="product")
    product = matches[0] if matches else api.post_json("/api/product-library", {"name": product_name, "brand": brand, "category": "Drinkware",
        "notes": "由一键脚本同步入口导入；图片按 SHA-256 去重。", "selling_points": [],
        "evidence_notes": "商品信息来自用户提供图片与剧本文字，不推断未提供的规格。", "target_audience": ""})
    known = {item.get("sha256") for item in product.get("images", [])}; missing = [image for image in candidate.images if image.sha256 not in known]
    if missing:
        uploaded = api.post_multipart(f"/api/product-library/{quote(product['id'], safe='')}/images",
            {"purpose": "main", "notes": "Word 同目录图片；SHA-256=" + ",".join(x.sha256 for x in missing)},
            [("files", image.path.name, image.path.read_bytes(), image.mime) for image in missing])
        errors = uploaded.get("errors", []) if isinstance(uploaded, dict) else []
        if errors: raise WorkflowError("部分产品图上传失败：" + json.dumps(errors, ensure_ascii=False), phase="product", details={"product_id": product["id"]})
        product = uploaded.get("product", product)
    wanted = {x.sha256 for x in candidate.images}; image_ids = [item["id"] for item in product.get("images", []) if item.get("sha256") in wanted]
    if not image_ids:
        product = api.get(f"/api/product-library/{quote(product['id'], safe='')}")
        image_ids = [item["id"] for item in product.get("images", []) if item.get("sha256") in wanted]
    if len(image_ids) != len(candidate.images):
        raise WorkflowError("上传后图库 SHA-256 与图片 ID 不匹配，停止生成", phase="product", details={"expected": sorted(wanted), "found": [i.get("sha256") for i in product.get("images", [])]})
    return product, image_ids

def identity_reference_image_ids(product: dict[str, Any], image_ids: list[str]) -> list[str]:
    """Choose one stable product-only identity anchor for video generation.

    Keep every synchronized image in the product library, but don't send a
    hand-held/detail photo alongside the cover as an identity reference. H3's
    visual guard can otherwise interpret the incidental hand as a second
    operator or fail to uniquely resolve the product. Preserve gallery order
    for the deterministic fallback when no selected cover is available.
    """
    selected = list(dict.fromkeys(item for item in image_ids if isinstance(item, str) and item))
    if not selected:
        return []
    cover = product.get("cover_image_id")
    return [cover] if isinstance(cover, str) and cover in selected else selected[:1]

def _project_has_run(overview: dict[str, Any]) -> tuple[bool, dict[str, Any] | None]:
    run = overview.get("run") if isinstance(overview, dict) else None; return bool(run and run.get("id")), run

def poll_existing_run(api: Api, project_id: str, run_id: str, *, wait: bool = False,
                     poll_seconds: float = 5, wait_seconds: float = 7200) -> dict[str, Any]:
    """Resume observation of a known run without importing or submitting it again."""
    deadline = time.monotonic() + wait_seconds
    while True:
        view = api.get(f"/api/studio/projects/{quote(project_id, safe='')}")
        current = view.get("run") or {}
        if current.get("id") != run_id:
            raise WorkflowError("项目 run ID 已变化；停止恢复轮询", phase="poll", details=current)
        auto = (current.get("result_json") or {}).get("automatic_final") or {}
        auto_status = auto.get("status")
        run_status = current.get("status")
        terminal = run_status in {"completed", "completed_with_errors", "cancelled", "waiting_review"}
        if terminal and auto_status in {"generated", "failed", "blocked", "cancelled", "completed"}:
            videos = [x.get("video") for x in view.get("shots", []) if x.get("video")]
            renders = view.get("renders", [])
            exceptions = view.get("exceptions", [])
            failed = auto_status in {"failed", "blocked", "cancelled"} or run_status in {"completed_with_errors", "cancelled", "waiting_review"} or bool(exceptions)
            status = ("cancelled" if auto_status == "cancelled" or run_status == "cancelled"
                      else "blocked" if auto_status == "blocked" or run_status == "waiting_review"
                      else "failed" if failed else "finished")
            return {"status": status, "project_id": project_id, "run_id": run_id,
                "run_status": run_status, "automatic_final": auto, "videos": videos,
                "renders": renders, "exceptions": exceptions,
                "resumed_without_resubmitting": True}
        if not wait:
            return {"status": "still_running", "project_id": project_id, "run_id": run_id,
                "run_status": run_status, "automatic_final": auto,
                "resumed_without_resubmitting": True}
        if time.monotonic() >= deadline:
            return {"status": "still_running_or_wait_timeout", "project_id": project_id,
                "run_id": run_id, "run_status": run_status, "automatic_final": auto,
                "resumed_without_resubmitting": True}
        time.sleep(poll_seconds)


def retry_product_reference_failures(api: Api, project_id: str, expected_run_id: str, *,
                                     attempted_node_ids: Iterable[str] = (), wait: bool = False,
                                     poll_seconds: float = 5, wait_seconds: float = 7200) -> dict[str, Any] | None:
    """Correct video-stage product identity references, then retry one failed shot at a time.

    The backend validates the frozen input and preserves keyframe references.
    Serial retries avoid overlapping product edits or duplicate jobs.
    """
    project_path = f"/api/studio/projects/{quote(project_id, safe='')}"
    attempted = list(dict.fromkeys(str(node_id) for node_id in attempted_node_ids if node_id))
    considered_set = set(attempted)
    active = {"dispatching", "running", "retrying", "queued", "submitted", "cancelling", "reconciling", "unknown"}
    terminal = {"succeeded", "exception", "failed", "skipped", "cancelled"}
    deadline = time.monotonic() + max(0.0, wait_seconds)

    def current_run() -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        overview = api.get(project_path)
        run = overview.get("run") if isinstance(overview, dict) else None
        if not isinstance(run, dict) or run.get("id") != expected_run_id:
            raise WorkflowError("项目当前 run ID 与恢复台账不一致；停止重试", phase="retry",
                details={"expected_run_id": expected_run_id,
                    "observed_run_id": run.get("id") if isinstance(run, dict) else None})
        nodes = run.get("nodes_json")
        if not isinstance(nodes, list):
            raise WorkflowError("项目 run 缺少节点状态；未提交重试", phase="retry")
        return overview, run, [node for node in nodes if isinstance(node, dict)]

    def result_for(run: dict[str, Any], status: str = "started", note: str | None = None) -> dict[str, Any]:
        result = {"status": status, "project_id": project_id, "run_id": expected_run_id,
            "run_status": run.get("status"), "retried_product_reference_nodes": attempted,
            "product_reference_retry_run_id": expected_run_id,
            "message": "视频阶段仅使用已验证的商品封面锚点；关键画面商品选图和其他制作输入保持不变。"}
        if note:
            result["retry_note"] = note
        return result

    def wait_until_idle(watched_node_id: str | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
        while True:
            _, run, nodes = current_run()
            by_id = {node.get("id"): node for node in nodes}
            watched = by_id.get(watched_node_id) if watched_node_id else None
            watched_done = not watched_node_id or (watched and watched.get("status") in terminal)
            if watched_done and not any(node.get("status") in active for node in nodes):
                return run, nodes
            if not wait or time.monotonic() >= deadline:
                return None
            time.sleep(max(0.0, poll_seconds))

    _, run, nodes = current_run()
    if run.get("status") not in {"running", "exceptions", "completed_with_errors", "paused"}:
        return None
    # An unknown result must be reconciled first; automatic replay could incur a
    # duplicate provider charge.
    if any(node.get("status") == "unknown" for node in nodes):
        return None
    if any(node.get("status") in active for node in nodes):
        idle = wait_until_idle()
        if idle is None:
            return result_for(run, "still_running_or_wait_timeout",
                "批次仍有活动节点；未重复提交，恢复时先核对当前节点状态。") if attempted else None
        run, nodes = idle

    while True:
        if run.get("status") not in {"running", "exceptions", "completed_with_errors", "paused"}:
            return result_for(run) if attempted else None
        failed = [node for node in nodes if node.get("kind") == "video"
            and node.get("status") in {"exception", "failed"}
            and ("商品参考身份不一致或无法确定" in str(node.get("error") or "")
                 or node.get("code") == "product_reference_identity_mismatch")
            and node.get("id") not in considered_set]
        if not failed:
            return result_for(run) if attempted else None
        node = failed[0]
        node_id = node.get("id")
        shot_id = node.get("target_id")
        if not isinstance(node_id, str) or not node_id or not isinstance(shot_id, str) or not shot_id:
            raise WorkflowError("商品参考失败节点缺少稳定节点或镜头 ID；未提交重试", phase="retry")

        selection = api.get(f"/api/product-library/projects/{quote(project_id, safe='')}/shots/{quote(shot_id, safe='')}/references")
        refs = selection.get("products") if isinstance(selection, dict) else None
        if not isinstance(refs, list) or not refs or not all(isinstance(ref, dict) for ref in refs):
            raise WorkflowError("失败镜头没有可核对的商品参考选择；未重试", phase="retry", details=selection)
        payload_products = []
        changed = False
        for ref in refs:
            product_id = ref.get("product_id")
            if not isinstance(product_id, str) or not product_id:
                raise WorkflowError("商品参考缺少稳定商品 ID；未重试", phase="retry", details=ref)
            product = api.get(f"/api/product-library/{quote(product_id, safe='')}")
            cover_id = product.get("cover_image_id") if isinstance(product, dict) else None
            available = {item.get("id") for item in (ref.get("images") or []) if isinstance(item, dict)}
            available.update(item.get("id") for item in (product.get("images") or []) if isinstance(item, dict))
            if not isinstance(cover_id, str) or cover_id not in available:
                raise WorkflowError("图库没有可验证的商品封面锚点；未更改商品或重试", phase="retry",
                    details={"product_id": product_id, "cover_image_id": cover_id,
                        "available_image_ids": sorted(value for value in available if isinstance(value, str))})
            old_video_ids = ref.get("video_image_ids") or []
            new_video_ids = old_video_ids if old_video_ids == [cover_id] else [cover_id]
            changed = changed or new_video_ids != old_video_ids
            payload_products.append({
                "product_id": product_id,
                "expected_revision": ref.get("revision"),
                "video_image_ids": list(new_video_ids),
                "keyframe_image_ids": list(ref.get("keyframe_image_ids") or []),
            })
        if not changed:
            # The server-side selection is an idempotency fence if the client
            # stopped after the request was accepted but before the journal saved.
            considered_set.add(node_id)
            _, run, nodes = current_run()
            continue
        payload = {"expected_run_id": expected_run_id, "node_id": node_id, "products": payload_products}
        response = api.post_json(f"{project_path}/retry-product-references", payload)
        updated_run = response.get("run") if isinstance(response, dict) else None
        if not isinstance(updated_run, dict) or updated_run.get("id") != expected_run_id:
            raise WorkflowError("商品参考恢复响应未确认原 run；先核对服务状态，未再次提交", phase="retry",
                details=response, outcome_unknown=True)
        updated_nodes = updated_run.get("nodes_json") or []
        updated_node = next((item for item in updated_nodes if isinstance(item, dict) and item.get("id") == node_id), None)
        if not updated_node or updated_node.get("status") in {"exception", "failed"}:
            raise WorkflowError("专用商品参考接口未确认失败镜头已重新排队；为避免重复费用停止自动重试",
                phase="retry", details={"node_id": node_id, "response_node": updated_node}, outcome_unknown=True)
        attempted.append(node_id)
        considered_set.add(node_id)
        run = updated_run
        if not wait:
            return result_for(run)
        idle = wait_until_idle(node_id)
        if idle is None:
            return result_for(run, "still_running_or_wait_timeout",
                f"已重排 {node_id}；节点或批次仍在运行，恢复时先核对状态。")
        run, nodes = idle


def reconcile_import_by_sha(api: Api, script_sha256: str) -> dict[str, Any] | None:
    """Read the durable import ledger before retrying an import with unknown response."""
    rows = api.get("/api/word-imports")
    if not isinstance(rows, list):
        raise WorkflowError("Word 导入台账响应无效；未知导入结果不允许重试", phase="reconcile")
    matches = [row for row in rows if isinstance(row, dict) and row.get("sha256") == script_sha256]
    if not matches:
        return {"found": False, "safe_to_replay": True, "reason": "远端没有该 SHA-256 的导入记录"}
    matches.sort(key=lambda row: str(row.get("created_at", "")))
    for row in reversed(matches):
        result = row.get("result_json") or row.get("result") or {}
        project_id = row.get("project_id") or (result.get("project_id") if isinstance(result, dict) else None)
        if row.get("status") in {"completed", "imported"} and project_id:
            return {"found": True, "safe_to_replay": True, "import_id": row.get("id"),
                    "revision": row.get("revision"), "status": row.get("status"), "project_id": project_id,
                    "reason": "找到完成的同哈希导入；复用其项目，不重新导入"}
        if row.get("status") == "pending_review" and row.get("id"):
            return {"found": True, "safe_to_replay": True, "import_id": row["id"],
                    "revision": row.get("revision"), "status": row.get("status"),
                    "records": [{"id": row.get("id"), "status": row.get("status")}],
                    "reason": "找到尚未建项的同哈希导入；将原记录重解析并验证后再确认"}
    unresolved = [row for row in matches if row.get("status") not in {"uploaded", "failed"}]
    if unresolved:
        return {"found": True, "safe_to_replay": False,
                "records": [{"id": row.get("id"), "status": row.get("status")} for row in unresolved],
                "reason": "存在无法判定是否已建立项目的导入状态"}
    return {"found": True, "safe_to_replay": True,
            "records": [{"id": row.get("id"), "status": row.get("status")} for row in matches],
            "reason": "同哈希记录尚未创建项目；可用修复后的解析器安全重放"}


def resume_existing_import(api: Api, import_id: str, script_sha256: str) -> dict[str, Any]:
    """Reconcile a timed-out upload by exact hash, then repair/confirm its import row."""
    path = f"/api/word-imports/{quote(import_id, safe='')}"
    existing = api.get(path)
    if not isinstance(existing, dict) or existing.get("sha256") != script_sha256:
        raise WorkflowError("待恢复导入的 SHA-256 与本地 Word 不一致；停止以防串档", phase="reconcile", details={
            "import_id": import_id, "expected_sha256": script_sha256,
            "actual_sha256": existing.get("sha256") if isinstance(existing, dict) else None})
    status = existing.get("status")
    result = existing.get("result_json") or {}
    if status == "completed":
        project_id = result.get("project_id") if isinstance(result, dict) else None
        shot_ids = result.get("shot_ids", []) if isinstance(result, dict) else []
        if project_id and shot_ids:
            return {"status": "imported", "import_id": import_id, "project_id": project_id,
                    "shot_ids": shot_ids, "result": result, "reused": True}
        raise WorkflowError("同哈希导入已完成，但缺少项目或镜头 ID；不重复导入", phase="reconcile", details=existing)
    if status not in {"pending_review", "uploaded", "failed", "parsing"}:
        raise WorkflowError(f"同哈希导入状态 {status!r} 无法安全恢复；未重新上传", phase="reconcile", details=existing)

    revision = existing.get("revision")
    retry_path = path + (f"/retry?expected_revision={int(revision)}" if revision is not None else "/retry")
    refreshed = api.request("POST", retry_path)
    if not isinstance(refreshed, dict) or refreshed.get("status") != "pending_review":
        raise WorkflowError("同哈希 Word 已重解析，但没有形成可确认的预览", phase="import", details=refreshed)
    issues = refreshed.get("issues") or []
    if issues:
        raise WorkflowError("修复后的 Word 预览仍有阻断问题；保留原导入供检查，不自动建项",
            phase="import", details={"import_id": import_id, "issues": issues, "revision": refreshed.get("revision")})
    confirmed = api.post_json(path + "/confirm", {"revision": refreshed.get("revision"),
        "duplicate_strategy": "new_project", "reuse_approved": True})
    if not isinstance(confirmed, dict):
        raise WorkflowError("恢复导入确认响应结构无效", phase="import", details=confirmed)
    project_id = confirmed.get("project_id")
    shot_ids = confirmed.get("shot_ids") or []
    if not project_id or not shot_ids:
        raise WorkflowError("恢复导入确认后缺少 project_id 或 shot_ids", phase="import", details=confirmed)
    return {"status": "imported", "import_id": import_id, "project_id": project_id,
            "shot_ids": shot_ids, "result": confirmed, "reused": True,
            "reparsed_revision": refreshed.get("revision")}

def submit_candidate(api: Api, candidate: Candidate, *, quality: dict[str, str], product_name: str | None, brand: str | None,
                     wait: bool = False, poll_seconds: float = 5, wait_seconds: float = 7200,
                     progress: Callable[[str, dict[str, Any]], None] | None = None,
                     reconciled_import: dict[str, Any] | None = None,
                     retry_failed_nodes: bool = False,
                     product_reference_retry_nodes: Iterable[str] = ()) -> dict[str, Any]:
    resolved_name, resolved_brand = product_identity(candidate, product_name=product_name, brand=brand)
    if len(candidate.images) > 8: raise WorkflowError("同一产品组图片超过 8 张；未导入 Word 或提交模型", phase="preflight",
        details={"image_count": len(candidate.images), "maximum": 8})
    ready, generation = generation_supported(api, quality)
    if not ready:
        raise WorkflowError("本机 Context-IR/H3 工作流未就绪，未导入 Word 或提交模型", phase="preflight", details=generation)
    supported, capability = quality_supported(api, quality)
    if not supported: raise WorkflowError("所选本机画质处理规格未验证，未导入 Word 或提交模型", phase="preflight", details=capability)
    if candidate.product_match and not candidate.images: raise WorkflowError("脚本匹配产品但无有效同目录产品图，未导入或提交", phase="preflight")
    matches = product_lookup(api, resolved_name, resolved_brand) if candidate.product_match else []
    if len(matches) > 1: raise WorkflowError("产品图库存在多个候选，未导入或提交", phase="preflight")
    if reconciled_import and reconciled_import.get("import_id"):
        if progress: progress("import_pending", {"import_id": reconciled_import["import_id"],
            "reconciled_status": reconciled_import.get("status")})
        imported = resume_existing_import(api, str(reconciled_import["import_id"]), candidate.sha256)
    else:
        if progress: progress("import_pending", {})
        imported = import_word(api, candidate, quality)
    if not isinstance(imported, dict): raise WorkflowError("Word 导入响应结构无效", phase="import")
    result = _result(imported)
    observed_project_id = result.get("project_id") or imported.get("project_id")
    if progress: progress("import_response", {"project_id": observed_project_id})
    if imported.get("status") not in {"imported", "completed"} or imported.get("issues"):
        raise WorkflowError("Word 未形成可自动制作项目：" + json.dumps(imported.get("issues", imported.get("preview", {})), ensure_ascii=False)[:1600], phase="import", details=imported)
    project_id = result.get("project_id") or imported.get("project_id"); shot_ids = result.get("shot_ids") or imported.get("shot_ids") or []
    if not project_id or not shot_ids: raise WorkflowError("导入响应缺少 project_id 或 shot_ids", phase="import", details=imported)
    if len(shot_ids) > 20: raise WorkflowError(f"该 Word 有 {len(shot_ids)} 镜，自动成片单项目限 20 镜，未启动制作", phase="preflight", details={"project_id": project_id, "shot_count": len(shot_ids)})
    product = None; image_ids = []; product_plan = None
    if candidate.product_match:
        product, image_ids = ensure_product(api, candidate, product_name=resolved_name, brand=resolved_brand)
        identity_ids = identity_reference_image_ids(product, image_ids)
        if not identity_ids:
            raise WorkflowError("产品图库没有可用身份锚点；保留已同步图片，未提交制作", phase="product",
                details={"product_id": product.get("id"), "image_ids": image_ids})
        product_plan = {"product_id": product["id"], "image_ids": identity_ids,
            "selection_reason": "同目录图片均已按 SHA-256 同步至图库；仅使用图库封面图作为商品身份锚点。"}
        if progress: progress("product_synced", {"project_id": project_id, "product_id": product.get("id"), "image_ids": image_ids})
    overview = api.get(f"/api/studio/projects/{quote(project_id, safe='')}"); has_run, old_run = _project_has_run(overview)
    if has_run:
        run_id = str(old_run["id"])
        if retry_failed_nodes:
            retried = retry_product_reference_failures(api, project_id, run_id,
                attempted_node_ids=product_reference_retry_nodes, wait=wait,
                poll_seconds=poll_seconds, wait_seconds=wait_seconds)
            if retried:
                outcome = {**retried, "product_id": product.get("id") if product else None,
                    "product_image_ids": image_ids, "shot_count": len(shot_ids), "quality": quality}
                if wait and retried.get("status") != "still_running_or_wait_timeout":
                    outcome.update(poll_existing_run(api, project_id, run_id, wait=True,
                        poll_seconds=poll_seconds, wait_seconds=wait_seconds))
                return outcome
        if wait:
            return poll_existing_run(api, project_id, run_id, wait=True,
                poll_seconds=poll_seconds, wait_seconds=wait_seconds)
        outcome = {"status": "existing_run_preserved", "project_id": project_id, "run": old_run, "message": "检测到已有制作记录；为避免重复派发，本次未再次启动"}
        if progress: progress("existing_run_preserved", {"project_id": project_id, "run": old_run})
        return outcome
    width, height = context_native_canvas(quality["aspect_ratio"])
    payload = {"quality": quality, "width": width, "height": height, "automatic_draft_assembly": False,
        "automatic_final_assembly": True, "allow_cloud_costs": False, "allow_paid_fallback": False}
    if product_plan: payload["product_reference_plan"] = product_plan
    if progress: progress("start_pending", {"project_id": project_id})
    started = api.post_json(f"/api/studio/projects/{quote(project_id, safe='')}/start", payload); run = started.get("run") if isinstance(started, dict) else None
    if not run or not run.get("id"): raise WorkflowError("启动响应缺少 run ID；先查询项目状态再重试", phase="start", details=started, outcome_unknown=True)
    outcome = {"status": "started", "project_id": project_id, "run_id": run["id"], "product_id": product.get("id") if product else None,
        "product_image_ids": image_ids, "shot_count": len(shot_ids), "quality": quality, "run_status": run.get("status")}
    if progress: progress("started", {"project_id": project_id, "run_id": run["id"], "run_status": run.get("status")})
    if wait:
        outcome.update(poll_existing_run(api, project_id, run["id"], wait=True,
            poll_seconds=poll_seconds, wait_seconds=wait_seconds))
        if progress: progress(outcome["status"], {"project_id": project_id, "run_id": run["id"],
            "run_status": outcome.get("run_status"), "video_count": len(outcome.get("videos", []))})
    return outcome


def _definite_start_rejection(entry: dict[str, Any]) -> bool:
    """A recorded 4xx start response proves no run was accepted by the API."""
    details = entry.get("details")
    status = details.get("http_status") if isinstance(details, dict) else None
    try: status = int(status)
    except (TypeError, ValueError): return False
    return entry.get("stage") in {"start_pending", "start_pending_retry"} and 400 <= status < 500 and not entry.get("outcome_unknown", False)


def _failure_status(exc: WorkflowError, stage: str) -> str:
    if exc.outcome_unknown: return "unknown"
    return "preflight_failed" if stage == "preflight" else "failed"


def _is_unresolved_import(entry: dict[str, Any] | None) -> bool:
    """Recognize both caught timeouts and crash-left import-pending ledger rows."""
    return bool(isinstance(entry, dict)
        and entry.get("stage") in {"import_pending", "import_response"}
        and entry.get("status") in {"unknown", "import_pending", "import_response"}
        and not entry.get("project_id") and not entry.get("run_id"))


def retry_rejected_project_start(api: Api, project_id: str, quality: dict[str, str], previous: dict[str, Any], *,
                                 wait: bool = False, poll_seconds: float = 5, wait_seconds: float = 7200,
                                 retry_failed_nodes: bool = False) -> dict[str, Any]:
    """Retry a definitively rejected start using the already-imported project.

    The project is read first so an interrupted or inconsistent ledger cannot
    accidentally dispatch a duplicate run. The screenplay is never re-imported.
    """
    overview = api.get(f"/api/studio/projects/{quote(project_id, safe='')}")
    has_run, existing = _project_has_run(overview)
    if has_run:
        if retry_failed_nodes:
            retried = retry_product_reference_failures(api, project_id, str(existing["id"]),
                attempted_node_ids=previous.get("retried_product_reference_nodes") or [], wait=wait,
                poll_seconds=poll_seconds, wait_seconds=wait_seconds)
            if retried:
                if wait and retried.get("status") != "still_running_or_wait_timeout":
                    retried.update(poll_existing_run(api, project_id, str(existing["id"]), wait=True,
                        poll_seconds=poll_seconds, wait_seconds=wait_seconds))
                return {**retried, "project_id": project_id, "run_id": existing["id"],
                    "retried_rejected_start": True}
        result = poll_existing_run(api, project_id, str(existing["id"]), wait=wait,
            poll_seconds=poll_seconds, wait_seconds=wait_seconds)
        return {**result, "project_id": project_id, "run_id": existing["id"], "resumed_without_resubmitting": True}
    width, height = context_native_canvas(quality["aspect_ratio"])
    payload: dict[str, Any] = {"quality": quality, "width": width, "height": height,
        "automatic_draft_assembly": False, "automatic_final_assembly": True,
        "allow_cloud_costs": False, "allow_paid_fallback": False}
    product_id = previous.get("product_id")
    image_ids = previous.get("image_ids") or previous.get("product_image_ids") or []
    if product_id and image_ids:
        product = api.get(f"/api/product-library/{quote(str(product_id), safe='')}")
        identity_ids = identity_reference_image_ids(product, list(image_ids))
        if not identity_ids:
            raise WorkflowError("无法从已同步商品图中确认身份锚点；未重新启动", phase="product",
                details={"product_id": product_id, "image_ids": image_ids})
        payload["product_reference_plan"] = {"product_id": product_id, "image_ids": identity_ids,
            "selection_reason": "恢复已导入项目时沿用图库封面图作为商品身份锚点。"}
    started = api.post_json(f"/api/studio/projects/{quote(project_id, safe='')}/start", payload)
    run = started.get("run") if isinstance(started, dict) else None
    if not run or not run.get("id"):
        raise WorkflowError("重试启动响应缺少 run ID；项目已存在，先查询状态再决定是否继续",
            phase="start", details=started, outcome_unknown=True)
    outcome = {"status": "started", "project_id": project_id, "run_id": run["id"],
        "product_id": product_id, "product_image_ids": image_ids,
        "shot_count": len(overview.get("shots", [])) if isinstance(overview, dict) else None,
        "quality": quality, "run_status": run.get("status"), "retried_rejected_start": True}
    if wait:
        outcome.update(poll_existing_run(api, project_id, run["id"], wait=True,
            poll_seconds=poll_seconds, wait_seconds=wait_seconds))
    return outcome

def resolve_selection(candidates: list[Candidate], selectors: list[str], *, limit: int) -> list[Candidate]:
    if not selectors: raise WorkflowError("真实提交必须精确选文件；请使用 --file 或 --interactive", phase="selection")
    selected = []
    for selector in selectors:
        normalized = selector.replace("\\", "/").casefold()
        matches = [x for x in candidates if x.relative_path.casefold() == normalized or x.path.as_posix().casefold() == normalized]
        if not matches and "/" not in normalized: matches = [x for x in candidates if Path(x.relative_path).name.casefold() == normalized]
        if len(matches) != 1: raise WorkflowError(f"选择 {selector!r} 匹配 {len(matches)} 份，请指定唯一相对路径", phase="selection")
        if matches[0] not in selected: selected.append(matches[0])
    if len(selected) > limit: raise WorkflowError(f"选择 {len(selected)} 份，超过 --limit={limit}", phase="selection")
    return selected


def select_all(candidates: list[Candidate], *, limit: int) -> list[Candidate]:
    if not candidates: raise WorkflowError("没有可处理的 Word 文件", phase="selection")
    if len(candidates) > limit:
        raise WorkflowError(f"扫描到 {len(candidates)} 份 Word，超过 --limit={limit}；提高限制后再启动", phase="selection")
    return list(candidates)

def interactive_selection(candidates: list[Candidate], limit: int) -> list[Candidate]:
    if not sys.stdin.isatty(): raise WorkflowError("当前非交互终端，请用 --file 精确选择", phase="selection")
    for n, item in enumerate(candidates, 1): print(f"{n:>3}. {item.relative_path}{' [匹配产品]' if item.product_match else ''}")
    raw = input(f"输入要处理的编号（最多 {limit} 个，逗号分隔）：").strip()
    try: indexes = [int(x.strip()) for x in raw.split(",") if x.strip()]
    except ValueError as exc: raise WorkflowError("编号须为逗号分隔的数字", phase="selection") from exc
    if not indexes or len(indexes) > limit or len(set(indexes)) != len(indexes) or any(x < 1 or x > len(candidates) for x in indexes):
        raise WorkflowError(f"请选择 1 至 {limit} 个范围内且不重复的编号", phase="selection")
    return [candidates[x - 1] for x in indexes]

def print_json(value: Any) -> None: print(json.dumps(value, ensure_ascii=False, indent=2, default=str))

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="脚本目录导入与自动制作（默认只读 dry-run）")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT); parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--file", action="append", default=[], help="精确相对路径或唯一文件名；可重复指定")
    parser.add_argument("--all", dest="all_files", action="store_true", help="将扫描到的全部 Word 按目录产品组顺序处理")
    parser.add_argument("--interactive", action="store_true"); parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--brand", default=None, help="可选：明确指定产品品牌；默认不臆测")
    parser.add_argument("--product-name", default=None, help="可选：明确指定产品名称；默认按目录产品组或文档明确内容")
    parser.add_argument("--resolution", choices=("720p", "1080p", "2k", "4k"), default=QUALITY["resolution"])
    parser.add_argument("--mode", choices=("high", "standard", "fast", "preview"), default=QUALITY["mode"])
    parser.add_argument("--processing", choices=("lanczos", "native", "ai_super_resolution", "seedvr2", "reconstruction"), default=QUALITY["processing"])
    parser.add_argument("--aspect-ratio", choices=("auto", "16:9", "9:16", "1:1"), default="auto",
        help="默认从每份 Word 解析；未标注时回退到 16:9")
    parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--submit", action="store_true")
    parser.add_argument("--confirm-submit", action="store_true"); parser.add_argument("--wait", action="store_true")
    parser.add_argument("--continue-on-error", action="store_true", help="批量任务中隔离失败项并继续其余文件")
    parser.add_argument("--stop-on-error", action="store_true", help="第一项失败后停止批量任务")
    parser.add_argument("--state-file", type=Path, default=DEFAULT_STATE_FILE, help="防重复提交状态文件（默认位于项目 data/）")
    parser.add_argument("--retry-failed", action="store_true",
        help="重跑安全的 preflight_failed 项，并仅重试空闲 run 中已确认的商品参考编译失败节点")
    parser.add_argument("--timeout", type=float, default=180); parser.add_argument("--poll-seconds", type=float, default=5); parser.add_argument("--wait-seconds", type=float, default=7200)
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.limit < 1 or min(args.timeout, args.poll_seconds, args.wait_seconds) <= 0: print("limit/timeout/poll/wait 必须为正数", file=sys.stderr); return 2
    if args.all_files and (args.file or args.interactive): print("--all 不能与 --file 或 --interactive 同时使用", file=sys.stderr); return 2
    if args.stop_on_error and args.continue_on_error: print("--stop-on-error 与 --continue-on-error 不能同时使用", file=sys.stderr); return 2
    if args.submit and args.dry_run: print("--submit 与 --dry-run 不能同时使用", file=sys.stderr); return 2
    if args.submit and not args.confirm_submit: print("拒绝提交：真实运行还需要 --confirm-submit", file=sys.stderr); return 2
    if args.confirm_submit and not args.submit and not args.interactive: print("--confirm-submit 只能与 --submit 或 --interactive 搭配", file=sys.stderr); return 2
    try:
        found = scan(args.root, brand=args.brand, product_name=args.product_name)
        print(f"扫描完成：有效 Word {len(found.candidates)} 份；隔离 Word {len(found.rejected)} 份")
        for item in found.rejected: print(f"隔离：{item['file']} — {item['error']}")
        if args.all_files: selected = select_all(found.candidates, limit=args.limit)
        elif args.interactive: selected = interactive_selection(found.candidates, args.limit)
        elif args.file: selected = resolve_selection(found.candidates, args.file, limit=args.limit)
        elif args.submit: raise WorkflowError("真实提交必须使用 --all、--file 或 --interactive 明确选择", phase="selection")
        else: selected = found.candidates[:1]; print("未选文件：只展示排序最前候选；真实批量提交请使用 --all。")
        qualities = [quality_for_candidate(item, resolution=args.resolution, mode=args.mode,
            processing=args.processing, requested_aspect_ratio=args.aspect_ratio) for item in selected]
        api = Api(args.api, args.timeout)
        plans = [dry_run_plan(item, api, quality, args.product_name, args.brand)
            for item, quality in zip(selected, qualities)]
        print_json({"selected_count": len(selected), "limit": args.limit, "mode": "all" if args.all_files else "selected", "plans": plans, "rejected_word_files": found.rejected,
            "note": "默认只读；所选本机处理方式没有对应画幅与分辨率的 capability supported=true 时，不会开始导入或生成。"})
        should_submit = args.submit
        if args.interactive and not args.dry_run and not args.submit:
            should_submit = input("复核计划后输入 START 开始所选真实任务；其他输入退出：").strip() == "START"
        if not should_submit: print("Dry-run 完成：没有写数据库、上传产品图或提交模型任务。"); return 0
        outcomes: list[dict[str, Any]] = []; continue_on_error = args.continue_on_error or args.all_files
        with RunLedger(args.state_file) as ledger:
            for candidate, plan, quality in zip(selected, plans, qualities):
                key = candidate_fingerprint(candidate, quality, api); previous = ledger.get(key) or {}
                reconciled_import = None
                if previous is None:
                    prior = find_prior_candidate_entry(ledger.entries, candidate, quality)
                    if prior:
                        prior_key, prior_value = prior
                        previous = {**prior_value, "prior_api_fingerprint": prior_value.get("api"),
                            "api": api_fingerprint(api.base), "recovered_from_ledger_key": prior_key}
                        ledger.record(key, previous)
                if _is_unresolved_import(previous):
                    try:
                        reconciled = reconcile_import_by_sha(api, candidate.sha256)
                    except WorkflowError as exc:
                        reconciled = {"safe_to_replay": False, "reason": str(exc)}
                    if reconciled.get("safe_to_replay") is True:
                        reconciled_import = reconciled if reconciled.get("import_id") else None
                        retry_record = {**previous, "status": "preflight_failed", "stage": "reconciled_import_safe_retry",
                            "reconciliation": reconciled, "safe_retry_evidence": "exact SHA-256 import ledger checked; no generation run was started"}
                        ledger.record(key, retry_record)
                        previous = retry_record
                if previous:
                    safe_retry = args.retry_failed and previous.get("status") == "preflight_failed"
                    if not safe_retry:
                        previous_status = previous.get("status")
                        project_id = previous.get("project_id")
                        run_id = previous.get("run_id")
                        if project_id and _definite_start_rejection(previous):
                            try:
                                outcome = retry_rejected_project_start(api, project_id, quality, previous,
                                    wait=args.wait, poll_seconds=args.poll_seconds, wait_seconds=args.wait_seconds,
                                    retry_failed_nodes=args.retry_failed)
                                outcome["script"] = candidate.relative_path
                                ledger.record(key, {**previous, **current, **outcome,
                                    "status": outcome["status"], "stage": outcome["status"]})
                                print_json(outcome); outcomes.append(outcome)
                            except WorkflowError as exc:
                                status = _failure_status(exc, "start_pending")
                                item = {"status": status, "script": candidate.relative_path, "project_id": project_id,
                                    "phase": exc.phase, "error": str(exc), "details": exc.details,
                                    "outcome_unknown": exc.outcome_unknown}
                                ledger.record(key, {**previous, **item, "stage": "start_pending_retry"})
                                print_json(item); outcomes.append(item)
                            continue
                        resumable = previous_status in {"started", "still_running", "still_running_or_wait_timeout"}
                        uncertain_with_project = previous_status in {"unknown", "start_pending"} and project_id
                        if uncertain_with_project and not run_id:
                            try:
                                overview = api.get(f"/api/studio/projects/{quote(project_id, safe='')}")
                                has_run, observed_run = _project_has_run(overview)
                                if has_run:
                                    run_id = observed_run.get("id")
                                    resumable = True
                            except WorkflowError:
                                pass
                        if resumable and project_id and run_id:
                            try:
                                item = retry_product_reference_failures(api, project_id, run_id,
                                    attempted_node_ids=previous.get("retried_product_reference_nodes") or [],
                                    wait=args.wait, poll_seconds=args.poll_seconds,
                                    wait_seconds=args.wait_seconds) if args.retry_failed else None
                                if item and args.wait and item.get("status") != "still_running_or_wait_timeout":
                                    item.update(poll_existing_run(api, project_id, run_id, wait=True,
                                        poll_seconds=args.poll_seconds, wait_seconds=args.wait_seconds))
                                if item is None:
                                    item = poll_existing_run(api, project_id, run_id, wait=args.wait,
                                        poll_seconds=args.poll_seconds, wait_seconds=args.wait_seconds)
                                item["script"] = candidate.relative_path
                                ledger.record(key, {**previous, **item, "status": item["status"], "stage": item["status"]})
                            except WorkflowError as exc:
                                item = {"status": "manual_check_required", "script": candidate.relative_path,
                                    "previous_status": previous_status, "project_id": project_id, "run_id": run_id,
                                    "error": str(exc), "phase": exc.phase, "details": exc.details}
                                ledger.record(key, {**previous, **item, "stage": "resume_check"})
                        else:
                            preserved_status = "duplicate_prevented" if previous_status in {"finished", "existing_run_preserved", "duplicate_prevented"} else "manual_check_required"
                            item = {"status": preserved_status, "script": candidate.relative_path,
                                "previous_status": previous_status, "project_id": project_id, "run_id": run_id,
                                "message": "已有未知或失败状态；为避免重复派发，未重新导入或提交。" if preserved_status == "manual_check_required" else None}
                        print_json(item); outcomes.append(item); continue
                if not plan.get("api", {}).get("reachable"):
                    item = {"status": "blocked", "script": candidate.relative_path, "reason": plan.get("api", {}).get("reason", "服务不可达")}
                    ledger.record(key, {**item, "status": "preflight_failed", "stage": "preflight"}); print_json(item); outcomes.append(item)
                    if not continue_on_error: break
                    continue
                if plan.get("blockers"):
                    item = {"status": "blocked", "script": candidate.relative_path, "blockers": plan["blockers"]}
                    ledger.record(key, {**item, "status": "preflight_failed", "stage": "preflight"}); print_json(item); outcomes.append(item)
                    if not continue_on_error: break
                    continue
                current: dict[str, Any] = {"stage": "preflight"}
                def progress(stage: str, details: dict[str, Any], *, _key: str = key,
                             _candidate: Candidate = candidate, _quality: dict[str, str] = quality) -> None:
                    current.update(details); current["stage"] = stage; current["status"] = stage
                    current["script"] = _candidate.relative_path
                    current["script_sha256"] = _candidate.sha256
                    current["image_sha256"] = sorted(asset.sha256 for asset in _candidate.images)
                    current["quality"] = _quality; current["api"] = api_fingerprint(api.base)
                    ledger.record(_key, current)
                try:
                    outcome = submit_candidate(api, candidate, quality=quality, product_name=args.product_name, brand=args.brand,
                        wait=args.wait, poll_seconds=args.poll_seconds, wait_seconds=args.wait_seconds, progress=progress,
                        reconciled_import=reconciled_import, retry_failed_nodes=args.retry_failed,
                        product_reference_retry_nodes=previous.get("retried_product_reference_nodes") or [])
                    outcomes.append(outcome)
                    if current.get("stage") == "preflight":
                        progress(outcome.get("status", "finished"), {"project_id": outcome.get("project_id"), "run_id": outcome.get("run_id")})
                    ledger.record(key, {**previous, **current, **outcome,
                        "status": outcome.get("status", "finished"), "stage": outcome.get("status", "finished"),
                        "script": candidate.relative_path, "script_sha256": candidate.sha256,
                        "image_sha256": sorted(asset.sha256 for asset in candidate.images),
                        "quality": quality, "api": api_fingerprint(api.base)})
                    print_json(outcome)
                except WorkflowError as exc:
                    stage = current.get("stage", "preflight")
                    status = _failure_status(exc, stage)
                    ledger.record(key, {**current, "status": status, "stage": stage, "script": candidate.relative_path,
                        "error": str(exc), "phase": exc.phase, "details": exc.details, "outcome_unknown": exc.outcome_unknown})
                    item = {"status": status, "script": candidate.relative_path, "phase": exc.phase, "error": str(exc),
                        "details": exc.details, "outcome_unknown": exc.outcome_unknown}
                    outcomes.append(item); print_json(item)
                    if not continue_on_error: break
        return 0 if outcomes and all(x.get("status") in {"started", "finished", "existing_run_preserved", "duplicate_prevented"} for x in outcomes) else 1
    except WorkflowError as exc:
        print(f"失败阶段：{exc.phase or 'unknown'}\n{exc}", file=sys.stderr)
        if exc.details is not None: print_json({"details": exc.details, "outcome_unknown": exc.outcome_unknown})
        if exc.outcome_unknown: print("写操作状态未知；先查询项目/图库再决定是否继续，不要盲目重复提交。", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("用户中断；已提交任务状态需在应用中核对，不会自动重提。", file=sys.stderr); return 130

if __name__ == "__main__": raise SystemExit(main())
