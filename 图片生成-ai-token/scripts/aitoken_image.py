#!/usr/bin/env python3
"""AI-TOKEN image client. Local journal is authoritative for submission safety."""
from __future__ import annotations

import argparse
import base64
import csv
import getpass
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://ai-token.vip"
MODELS = BASE + "/v1/models"
MARKET = BASE + "/api/model-marketplace"
SELECTOR = BASE + "/api/content/ai-models/selector?section=generate"
EDIT_SELECTOR = BASE + "/api/content/ai-models/selector?section=edit"
GENERATE = BASE + "/v1/images/generations"
EDIT = BASE + "/v1/images/edits"
RESULTS = BASE + "/v1/images/results"
RESUME = BASE + "/v1/images/tasks/"
DOCS = BASE + "/dashboard/api-docs"
DOCS_CATALOG = BASE + "/api/api-docs"
DOCS_MODELS = DOCS_CATALOG + "/models/"
MAX_ATTEMPTS = 4
MAX_BYTES = 128 * 1024 * 1024
MAX_REFERENCE_BYTES = 20 * 1024 * 1024


class ClientError(Exception):
    pass


class ApiError(ClientError):
    def __init__(self, kind, status=0):
        self.kind, self.status = kind, status
        super().__init__(f"API {kind}; HTTP {status or 'unavailable'}")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise ClientError("Private storage must not be a symbolic link")
    if os.name == "nt":
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, check=True)
        sid = next(csv.reader(io.StringIO(result.stdout)))[1]
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r",
                        f"*{sid}:(OI)(CI)F"], capture_output=True, check=True)
    else:
        path.chmod(0o700)


def write_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(encode(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def process_lock(path):
    with open(path, "a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ClientError("Another skill process is active; inspect its result, do not resubmit") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ApiError("unknown", code)


def public_image_url(url):
    part = urllib.parse.urlsplit(url)
    if (part.scheme != "https" or not part.hostname or part.username or
            part.password or part.port not in (None, 443)):
        raise ClientError("Image URL must be public HTTPS")
    addresses = socket.getaddrinfo(part.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ClientError("Image URL must not target a private/local address")


class ImageRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Http:
    def __init__(self, key=""):
        self.key = key

    def json(self, url, payload=None, idempotency=None):
        endpoint = url.split("?", 1)[0]
        allowed = url in {MODELS, MARKET, SELECTOR, EDIT_SELECTOR, GENERATE, DOCS_CATALOG}
        allowed = allowed or endpoint == RESULTS or bool(re.fullmatch(re.escape(RESUME) + r"[1-9][0-9]*/resume", url))
        allowed = allowed or bool(re.fullmatch(re.escape(DOCS_MODELS) + r"[A-Za-z0-9_-]+\?revision=[A-Za-z0-9_-]+", url))
        if not allowed:
            raise ClientError("Unrecognized API endpoint")
        if payload is not None and endpoint not in {GENERATE} and not url.startswith(RESUME):
            raise ClientError("This endpoint is read-only")
        data = None if payload is None else encode(payload).encode("utf-8")
        return self._send(url, data, "application/json" if data is not None else None, idempotency)

    def edit(self, fields, references, idempotency):
        boundary = "aitoken-" + uuid.uuid4().hex
        chunks = []
        for name, value in fields.items():
            chunks.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n').encode("utf-8"))
        for index, (reference, raw) in enumerate(read_references(references), 1):
            chunks.extend([
                (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="reference-{index}{reference["suffix"]}"\r\n'
                 f'Content-Type: {reference["mime"]}\r\n\r\n').encode("utf-8"), raw, b"\r\n"])
        chunks.append(f"--{boundary}--\r\n".encode("ascii"))
        return self._send(EDIT, b"".join(chunks), "multipart/form-data; boundary=" + boundary, idempotency)

    def _send(self, url, data, content_type, idempotency):
        headers = {"Accept": "application/json", "User-Agent": "AI-TOKEN-Image-Skill/1"}
        endpoint = url.split("?", 1)[0]
        if endpoint in {MODELS, GENERATE, EDIT, RESULTS} or url.startswith(RESUME):
            if not self.key:
                raise ClientError("API Key is missing; run configure")
            headers["Authorization"] = "Bearer " + self.key
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        if content_type:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req, timeout=700 if data else 30) as response:
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ApiError("unknown")
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                body = json.loads(exc.read(32768).decode("utf-8"))
            except (ValueError, OSError):
                body = {}
            finally:
                exc.close()
            error = body.get("error", {}) if isinstance(body, dict) else {}
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            message = str(error.get("message", "")) if isinstance(error, dict) else ""
            if ("idempotency" in message.lower() or "processing" in message.lower()
                    or isinstance(body, dict) and (body.get("tasks") or body.get("requires_admin"))):
                raise ApiError("unknown", status) from None
            if status in {401, 402, 403}:
                raise ApiError("blocked", status) from None
            if status == 404 and endpoint == RESULTS:
                raise ApiError("not_found", status) from None
            if status in {400, 422} and (code in {"image_generation_failed", "image_edit_failed", "invalid_image_upload", "invalid_request_error"}
                                         or isinstance(body.get("detail") if isinstance(body, dict) else None, list)):
                raise ApiError("failed", status) from None
            raise ApiError("unknown", status) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ApiError("unknown") from None

    def image(self, url):
        public_image_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "AI-TOKEN-Image-Skill/1"})
        # Intentionally no Authorization header, including on redirects.
        with urllib.request.build_opener(ImageRedirect()).open(req, timeout=60) as response:
            data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ClientError("Image exceeds download size limit")
        return data


def credit(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError
        return format(number.normalize(), "f")
    except (InvalidOperation, ValueError):
        raise ClientError("Missing or invalid credit price") from None


def combinations(option):
    rows = option.get("parameter_rows") or []
    if rows:
        return [{"tier": str(r["tier"]).upper(), "ratio": str(r["ratio"]),
                 "quality": str(r.get("quality") or ""), "credits": credit(r.get("site_price"))}
                for r in rows if isinstance(r, dict) and "tier" in r and "ratio" in r]
    return []


def catalog(api, marketplace, selector):
    if not isinstance(api, dict) or not isinstance(api.get("data"), list):
        raise ClientError("Unexpected /models schema")
    entries = []
    for m in api["data"]:
        if not isinstance(m, dict) or "image" not in (m.get("capabilities") or []):
            continue
        model = {k: m.get(k) for k in ("id", "channel_name", "display_name")}
        model["office_image_contract"] = m.get("office_image_contract") or {}
        model["supports_edit"] = model["office_image_contract"].get("supports_reference") is True
        model.update(choices=[], problems=[])
        markets = [r for r in marketplace.get("models", [])
                   if r.get("channel_name") == m.get("channel_name")
                   and r.get("display_name") == m.get("display_name")
                   and r.get("is_available") is True]
        if len(markets) == 1:
            market = markets[0]
            matches = [s for s in selector.get("models", [])
                       if s.get("name") == m.get("display_name")
                       and s.get("provider") == market.get("provider")
                       and s.get("api_integration_id") == market.get("api_integration_id")
                       and s.get("model_id") == market.get("model_id")
                       and s.get("is_enabled") is True]
            if len(matches) == 1:
                s = matches[0]
                opt = s.get("grsai_drawing_options") or {}
                model["quality_options"] = opt.get("quality_options") or []
                model["quality_default"] = opt.get("quality_default") or ""
                model["choices"] = combinations(opt)
                if not model["choices"] and s.get("pricing_source") == "site":
                    qualities = model["quality_options"] or [""]
                    prices = s.get("pricing") or {}
                    for tier in opt.get("image_size_options") or []:
                        ratios = (opt.get("aspect_ratio_options_by_size") or {}).get(tier) or opt.get("aspect_ratio_options") or []
                        for ratio in ratios:
                            for quality in qualities:
                                keys = [f"{tier}|{ratio}|{quality}", f"{tier}|{quality}"] if quality else []
                                keys += [str(tier)]
                                values = [prices[k] for k in keys if k in prices]
                                if values:
                                    model["choices"].append(dict(tier=str(tier).upper(), ratio=str(ratio),
                                                                  quality=quality, credits=credit(values[0])))
            else:
                model["problems"].append("生成参数缺失或匹配不唯一")
        else:
            model["problems"].append("模型市场匹配缺失或不唯一")
        if not model["choices"]:
            model["problems"].append("缺少可核对的参数组合价格，不能生成")
        rates = sorted({(c["tier"], c["quality"], c["credits"]) for c in model["choices"]})
        price_text = "；".join(f"{t}{('/' + q) if q else ''}：{v} 积分" for t, q, v in rates)
        model["number"] = len(entries) + 1
        model["label"] = f"{model['number']}. {model['channel_name']} | {model['display_name']} — {price_text or '积分未知（暂不可用）'}"
        entries.append(model)
    ids = [m["id"] for m in entries]
    if any(not value for value in ids) or len(ids) != len(set(ids)):
        raise ClientError("Ambiguous public model IDs")
    return entries


def selected_model(models, model_id):
    rows = [m for m in models if m["id"] == model_id]
    if len(rows) != 1:
        raise ClientError("Model no longer available to this API Key")
    return rows[0]


def option_menu(model, tier=None, ratio=None):
    rows = model["choices"]
    if tier:
        rows = [r for r in rows if r["tier"] == tier.upper()]
    if ratio:
        rows = [r for r in rows if r["ratio"] == ratio]
    qs = sorted({r["quality"] for r in rows})
    return {"model_id": model["id"], "tiers": sorted({r["tier"] for r in rows}),
            "ratios": sorted({r["ratio"] for r in rows}), "quality_options": qs,
            "ask_quality": len(qs) > 1, "auto_quality": qs[0] if len(qs) == 1 else None,
            "choices": rows, "problems": model["problems"],
            "supports_edit": model.get("supports_edit", False),
            "max_reference_images": reference_limit(model),
            "gateway_limitations": []}


def choose(model, tier, ratio, quality=None):
    contract = model.get("office_image_contract") or {}
    if contract.get("version") != 2 or contract.get("explicit_parameters") is not True:
        raise ClientError("此模型未提供新版图片参数契约，不能猜测或回退参数")
    choices = [c for c in model["choices"] if c["tier"] == tier.upper() and c["ratio"] == ratio]
    qs = sorted({c["quality"] for c in choices})
    if quality is None:
        if len(qs) != 1:
            raise ClientError("Select an available picture quality first")
        quality = qs[0]
    rows = [c for c in choices if c["quality"] == quality]
    if len(rows) != 1:
        raise ClientError("Unsupported or ambiguous parameter combination")
    choice = rows[0]
    return dict(choice)


def payload(plan, model_id):
    params = plan["choice"]
    return {"model": model_id, "prompt": plan["prompt"], "n": 1, "response_format": "url",
            "image_size": params["tier"], "aspect_ratio": params["ratio"],
            "image_quality": params["quality"], "result_delivery": "storage"}


def reference_files(paths):
    if not 1 <= len(paths) <= 4:
        raise ClientError("图片编辑需要 1 至 4 张本地参考图片")
    from PIL import Image
    formats = {"PNG": (".png", "image/png"), "JPEG": (".jpg", "image/jpeg"),
               "WEBP": (".webp", "image/webp"), "GIF": (".gif", "image/gif"),
               "BMP": (".bmp", "image/bmp"), "TIFF": (".tiff", "image/tiff"), "AVIF": (".avif", "image/avif")}
    references = []
    for value in paths:
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise ClientError("参考图必须是用户指定的本地图片绝对路径")
        path = path.resolve()
        with path.open("rb") as stream:
            raw = stream.read(MAX_REFERENCE_BYTES + 1)
        if len(raw) > MAX_REFERENCE_BYTES:
            raise ClientError("单张参考图不得超过 20 MiB；平台限制可能更低")
        try:
            with Image.open(io.BytesIO(raw)) as image:
                suffix, mime = formats[image.format]
                image.verify()
        except (OSError, ValueError, KeyError):
            raise ClientError("参考图不是可识别的受支持图片") from None
        references.append(dict(path=str(path), sha256=hashlib.sha256(raw).hexdigest(),
                               bytes=len(raw), suffix=suffix, mime=mime))
    return references


def reference_limit(model):
    value = model.get("office_image_contract", {}).get("max_reference_images", 4)
    return min(4, value) if type(value) is int and value > 0 else 0


def read_references(references):
    files = []
    for reference in references:
        with Path(reference["path"]).open("rb") as stream:
            raw = stream.read(MAX_REFERENCE_BYTES + 1)
        if len(raw) != reference["bytes"] or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
            raise ClientError("参考图在准备后发生变化，请重新确认素材；本次未提交")
        files.append((reference, raw))
    return files


class Store:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.local = self.root / ".local"
        private_dir(self.local)
        (self.local / "prompts").mkdir(exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.local / "tasks.sqlite3")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.commit()

    def close(self):
        self.db.close()

    def read(self, task_id):
        row = self.db.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, job):
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                            (job["task_id"], encode(job)))

    def key(self):
        path = self.local / "config.json"
        if not path.exists():
            raise ClientError("请先输入 API Key：运行 configure")
        return json.loads(path.read_text(encoding="utf-8"))["api_key"]

    def configure(self, key):
        key = key.strip()
        if not key or any(c.isspace() for c in key):
            raise ClientError("API Key is empty or contains whitespace")
        Http(key).json(MODELS)
        write_json(self.local / "config.json", {"api_key": key, "base_url": BASE + "/v1", "docs_url": DOCS})
        return {"configured": True, "config_path": str(self.local / "config.json")}


class Client:
    def __init__(self, store, http):
        self.store, self.http = store, http

    def models(self, operation="generate"):
        if operation not in {"generate", "edit"}:
            raise ClientError("请选择 generate 或 edit 操作")
        selector = EDIT_SELECTOR if operation == "edit" else SELECTOR
        rows = catalog(self.http.json(MODELS), self.http.json(MARKET), self.http.json(selector))
        for model in rows:
            model["operation"] = operation
            if operation == "edit" and not model["supports_edit"]:
                model["problems"].append("该模型未开放参考图编辑")
        write_json(self.store.local / "models.json", {"checked_at": time.time(), "operation": operation, "models": rows})
        return rows

    def prepare(self, task_id, prompt, output_dir, model_id, tier, ratio, quality=None,
                operation="generate", images=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise ClientError("Task ID must contain 1-100 letters, digits, hyphens or underscores")
        if not prompt.strip() or not 1 <= len(prompt) <= 20000:
            raise ClientError("Prompt must contain 1-20000 characters")
        if operation not in {"generate", "edit"}:
            raise ClientError("请选择 generate 或 edit 操作")
        if operation == "generate" and images:
            raise ClientError("使用参考图时必须选择 edit，不能静默改为文生图")
        references = reference_files(images or []) if operation == "edit" else []
        directory = Path(output_dir).expanduser()
        if not directory.is_absolute():
            raise ClientError("Specify the user's absolute output directory")
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        request = dict(prompt=prompt, output_dir=str(directory), model_id=model_id,
                       tier=tier.upper(), ratio=ratio, quality=quality, operation=operation, references=references)
        existing = self.store.read(task_id)
        if existing:
            old_request = {"operation": "generate", "references": [], **existing["request"]}
            if old_request != request:
                raise ClientError("Task ID already has a different immutable request")
            return summary(existing)
        # Verify writable output before any paid submission.
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"ok")
        model = selected_model(self.models(operation), model_id)
        if references and (not model["supports_edit"] or len(references) > reference_limit(model)):
            raise ClientError("此模型不支持所选参考图数量，不能提交编辑任务")
        choice = choose(model, tier, ratio, quality)
        job = dict(task_id=task_id, request=request, plan=dict(prompt=prompt, output_dir=str(directory),
                   model_id=model_id, choice=choice, operation=operation, references=references),
                   api_contract=2, state="ready", attempts=0, failures=0,
                   history=[], result=None, saved_path=None)
        self.store.save(job)
        return summary(job)

    def get(self, task_id):
        job = self.store.read(task_id)
        if job is None:
            raise ClientError("Task not found; inspect tasks before creating a new ID")
        return job

    def run(self, task_id):
        job = self.get(task_id)
        if job["state"] == "dispatching":
            job["state"] = "unknown"
            self.store.save(job)
        if job.get("result"):
            return self.download(task_id)
        if job.get("generation_succeeded"):
            return summary(job)
        if job["state"] not in {"ready", "retryable"}:
            return summary(job)
        if job.get("api_contract") != 2:
            job["state"] = "selection_changed"
            self.store.save(job)
            return summary(job)
        operation = job["plan"].get("operation", "generate")
        references = job["plan"].get("references", [])
        if references:
            read_references(references)
        while job["attempts"] < MAX_ATTEMPTS:
            try:
                models = self.models(operation)
                plan = job["plan"]
                candidates = []
                for model in models:
                    c = plan["choice"]
                    if references and (not model.get("supports_edit") or len(references) > reference_limit(model)):
                        continue
                    try:
                        candidate = choose(model, c["tier"], c["ratio"], c["quality"])
                    except ClientError:
                        continue
                    if candidate["credits"] == c["credits"]:
                        candidates.append(model["id"])
                original = plan["model_id"]
                if job["attempts"] == 0:
                    if original not in candidates:
                        raise ClientError("Model access, price or parameters changed; reselect before generation")
                    selected = original
                else:
                    used = {h["model_id"] for h in job["history"]}
                    unused = [m for m in candidates if m not in used]
                    if unused:
                        selected = unused[0]
                    elif original in candidates:
                        selected = original
                    elif candidates:
                        selected = candidates[0]
                    else:
                        raise ClientError("No same-price model supports all selected parameters")
            except ClientError:
                job["state"] = "selection_changed"
                self.store.save(job)
                return summary(job)
            number = job["attempts"] + 1
            idem = "aitoken-img-" + digest(task_id + ":" + str(number))
            job["attempts"] = number
            job["state"] = "dispatching"
            job.pop("request_id", None)
            endpoint = EDIT if operation == "edit" else GENERATE
            job["history"].append(dict(attempt=number, model_id=selected, idempotency_key=idem,
                                       endpoint=endpoint, state="dispatching"))
            self.store.save(job)  # Durable before network dispatch.
            try:
                fields = payload(plan, selected)
                result = (self.http.edit(fields, references, idem) if operation == "edit"
                          else self.http.json(GENERATE, fields, idem))
            except ApiError as exc:
                job["history"][-1].update(state=exc.kind, http_status=exc.status)
                if exc.kind == "failed":
                    job["failures"] += 1
                    job["history"][-1]["failure_counted"] = True
                    job["state"] = "exhausted" if job["attempts"] >= MAX_ATTEMPTS else "retryable"
                    self.store.save(job)
                    if job["state"] == "retryable":
                        continue
                else:
                    job["state"] = exc.kind
                    self.store.save(job)
                return summary(job)
            except (ClientError, OSError):
                # Local input validation failed before the edit transport was opened.
                job["state"] = "inputs_changed"
                job["history"][-1]["state"] = "inputs_changed"
                self.store.save(job)
                return summary(job)
            return self.receive(job, result)
        job["state"] = "exhausted"
        self.store.save(job)
        return summary(job)

    def receive(self, job, response, queried=False):
        response = response if isinstance(response, dict) else {}
        request_id = response.get("request_id")
        if isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id):
            job["request_id"] = request_id
        tasks = response.get("tasks")
        job["server_tasks"] = []
        if isinstance(tasks, list):
            statuses = {"pending", "queued", "running", "processing", "succeeded", "completed", "failed", "error",
                        "cancelled", "query_paused", "result_fetch_failed", "result_save_failed", "result_unknown"}
            job["server_tasks"] = [dict(task_id=t["task_id"], status=t.get("status") if t.get("status") in statuses else "unknown",
                                       can_resume=t.get("can_resume") is True)
                                   for t in tasks[:8] if isinstance(t, dict) and type(t.get("task_id")) is int and t["task_id"] > 0]
            if any(t.get("status") in ("succeeded", "completed", "result_fetch_failed", "result_save_failed")
                   for t in tasks if isinstance(t, dict)):
                job["generation_succeeded"] = True
        job["requires_admin"] = response.get("requires_admin") is True
        job.pop("query_error", None)
        items = response.get("data")
        item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
        result = {k: item[k] for k in ("url", "b64_json") if isinstance(item.get(k), str) and item[k]}
        if result:
            job.update(result=result, generation_succeeded=True, state="succeeded")
        else:
            status = response.get("status")
            if status in {"succeeded", "completed", "result_fetch_failed", "result_save_failed"}:
                job["generation_succeeded"] = True
                job["state"] = "delivered" if response.get("manual_delivery_confirmed") is True else "result_fetch_failed"
            elif status == "query_paused":
                job["state"] = "query_paused"
            elif status in {"pending", "queued", "running", "processing", "incomplete"}:
                job["state"] = "pending"
            elif (queried and status == "failed" and not job.get("generation_succeeded") and not job.get("result")
                  and isinstance(tasks, list)
                  and all(isinstance(t, dict) and t.get("status") in ("failed", "error", "cancelled") for t in tasks)
                  and zero_amount(response.get("billed_amount")) and zero_amount(response.get("reserved_amount"))):
                if not job["history"][-1].get("failure_counted"):
                    job["failures"] += 1
                    job["history"][-1]["failure_counted"] = True
                job["state"] = "exhausted" if job["attempts"] >= MAX_ATTEMPTS else "retryable"
            else:
                job["state"] = "result_fetch_failed" if job.get("generation_succeeded") else "unknown"
        if job.get("requires_admin") and not result:
            job["state"] = "unknown"
        job["history"][-1]["state"] = "failed" if job["state"] in {"retryable", "exhausted"} else job["state"]
        self.store.save(job)  # Record success before any download or image validation.
        return self.download(job["task_id"]) if job.get("result") else summary(job)

    def query(self, task_id):
        job = self.get(task_id)
        if not job["history"] or job["state"] in {"saved", "delivered"}:
            return summary(job)
        if job["state"] == "dispatching":
            job["state"] = "unknown"
            self.store.save(job)
        attempt = job["history"][-1]
        endpoint = attempt.get("endpoint", GENERATE)
        if endpoint not in {GENERATE, EDIT}:
            raise ClientError("未知的历史图片接口，不能查询或重新提交")
        url = RESULTS + "?" + urllib.parse.urlencode({"idempotency_key": attempt["idempotency_key"],
                                                      "endpoint": urllib.parse.urlsplit(endpoint).path})
        try:
            response = self.http.json(url)
        except ApiError as exc:
            # A 404, changed credential or unavailable query never authorizes another generation.
            job["query_error"] = exc.kind if exc.kind in {"not_found", "blocked"} else "unavailable"
            self.store.save(job)
            return summary(job)
        return self.receive(job, response, queried=True)

    def resume(self, task_id):
        self.query(task_id)
        job = self.get(task_id)
        if job.get("result") or job.get("requires_admin") or job.get("query_error"):
            return summary(job)
        resumable = [t for t in job.get("server_tasks", []) if t["can_resume"]
                     and t["status"] in {"query_paused", "result_fetch_failed", "result_save_failed"}]
        for task in resumable:
            job.setdefault("resume_history", []).append(dict(task_id=task["task_id"], state="dispatching"))
            self.store.save(job)
            try:
                self.http.json(RESUME + str(task["task_id"]) + "/resume", {})
            except ApiError:
                job["resume_history"][-1]["state"] = "unknown"
                self.store.save(job)
                return summary(job)
            job["resume_history"][-1]["state"] = "requested"
            self.store.save(job)
        return self.query(task_id) if resumable else summary(job)

    def download(self, task_id):
        job = self.get(task_id)
        if not job.get("result"):
            raise ClientError("No recorded success to download; generation has not been retried")
        if job.get("saved_path"):
            saved = Path(job["saved_path"])
            if saved.is_file() and hashlib.sha256(saved.read_bytes()).hexdigest() == job.get("image_sha256"):
                return summary(job)
        try:
            result = job["result"]
            data = (base64.b64decode(result["b64_json"], validate=True) if result.get("b64_json")
                    else self.http.image(result["url"]))
            from PIL import Image
            if len(data) > MAX_BYTES:
                raise ClientError("Image exceeds size limit")
            with Image.open(io.BytesIO(data)) as im:
                suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(im.format)
                if suffix is None:
                    raise ClientError("Unsupported image format")
                dimensions = list(im.size)
                im.verify()
            directory = Path(job["plan"]["output_dir"])
            target = directory / ("aitoken_" + digest(task_id)[:24] + suffix)
            sha = hashlib.sha256(data).hexdigest()
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                    raise ClientError("Output file already exists with different content")
            else:
                fd, temp_name = tempfile.mkstemp(dir=directory, suffix=".part")
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    # Publish atomically without overwriting any user file.
                    os.link(temp_name, target)
                finally:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
            job.update(state="saved", saved_path=str(target.resolve()), image_sha256=sha, dimensions=dimensions)
        except Exception:
            job["state"] = "download_pending"
            # Signed URLs, raw HTTP errors and user prompts must not reach stdout.
        self.store.save(job)
        return summary(job)


def summary(job):
    result = {k: job.get(k) for k in ("task_id", "request_id", "state", "attempts", "failures", "saved_path", "dimensions")}
    result["generation_succeeded"] = bool(job.get("result") or job.get("generation_succeeded"))
    result["model_id"] = job["history"][-1]["model_id"] if job["history"] else job["plan"]["model_id"]
    result["parameters"] = job["plan"]["choice"]
    result["output_dir"] = job["plan"]["output_dir"]
    result["max_attempts"] = MAX_ATTEMPTS
    result["operation"] = job["plan"].get("operation", "generate")
    result["reference_count"] = len(job["plan"].get("references", []))
    result["can_query"] = bool(job["history"]) and job["state"] not in {"saved", "delivered"}
    result["server_tasks"] = job.get("server_tasks", [])
    result["can_resume"] = (not job.get("requires_admin") and not job.get("query_error") and not job.get("result")
                            and any(t["can_resume"] for t in result["server_tasks"]))
    result["requires_admin"] = job.get("requires_admin", False)
    if job.get("query_error"):
        result["query_error"] = job["query_error"]
    messages = {
        "unknown": "服务端结果未知；用 query 查询原请求，禁止重新生成。仍未知时联系管理员。",
        "pending": "原请求仍在处理；用 query 查询，不要再次提交生成。",
        "query_paused": "原任务查询已暂停；can_resume 为 true 时用 resume 恢复原任务。",
        "result_fetch_failed": "可能已成功并计费；查询或恢复原任务以获取结果，禁止重新生成。",
        "delivered": "平台已确认人工交付；请核对原结果，不要再次生成。",
        "dispatching": "请求已记录为发送中；不可重复提交。",
        "inputs_changed": "本地参考图校验失败，未完成提交；请核对原素材与任务记录。",
        "retryable": "原请求已确认失败；可用 run 在剩余预算内按同价、相同参数重试。",
        "exhausted": "已明确失败 4 次。是否需要重新选择其他线路模型开启新任务？",
        "download_pending": "生图已成功，只能补下载；禁止再次生图。",
        "selection_changed": "可用模型、价格或参数发生变化，需要重新选择；尚未再次提交。",
        "blocked": "认证、账户余额或额度阻止调用，请处理后再安排任务。",
    }
    if job["state"] in messages:
        result["message"] = messages[job["state"]]
    return result


def zero_amount(value):
    try:
        return value is not None and credit(value) == "0"
    except ClientError:
        return False


def check_docs(store):
    contract = json.loads((store.root / "config" / "api-contract.json").read_text(encoding="utf-8"))
    http = Http()
    catalog = http.json(DOCS_CATALOG)
    revision = catalog.get("revision")
    document_id = contract["reviewed_image_document_id"]
    if (catalog.get("published") is not True or not isinstance(revision, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", revision)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", document_id)):
        raise ClientError("公开接口文档未发布或格式已变化，请查阅官方文档")
    document = http.json(DOCS_MODELS + document_id + "?revision=" + revision)
    observed = digest(encode({"catalog": catalog, "document": document}))
    value = dict(documentation_url=DOCS, checked_at=time.time(), observed_revision=revision, observed_sha256=observed,
                 reviewed_revision=contract["documentation_revision"], reviewed_sha256=contract["documentation_sha256"],
                 needs_review=revision != contract["documentation_revision"] or observed != contract["documentation_sha256"])
    write_json(store.local / "docs-check.json", value)
    return value


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="图片生成（Ai-token）：图片生成、参考图编辑与原任务结果恢复")
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("configure")
    config.add_argument("--key-stdin", action="store_true")
    for cmd in ("config-status", "check-docs", "models", "tasks"):
        command = sub.add_parser(cmd)
        if cmd == "models":
            command.add_argument("--operation", choices=("generate", "edit"), default="generate")
    options = sub.add_parser("options")
    options.add_argument("--model-id", required=True)
    options.add_argument("--tier")
    options.add_argument("--ratio")
    options.add_argument("--operation", choices=("generate", "edit"), default="generate")
    prepare = sub.add_parser("prepare")
    for flag in ("task-id", "prompt-file", "output-dir", "model-id", "tier", "ratio"):
        prepare.add_argument("--" + flag, required=True)
    prepare.add_argument("--quality")
    prepare.add_argument("--operation", choices=("generate", "edit"), default="generate")
    prepare.add_argument("--image", action="append", help="编辑参考图的本地绝对路径，可重复指定 1 至 4 次")
    for cmd in ("run", "download", "status", "query", "resume"):
        sub.add_parser(cmd).add_argument("--task-id", required=True)
    args = parser.parse_args()
    store = None
    try:
        store = Store()
        with process_lock(store.local / "process.lock"):
            if args.command == "configure":
                value = sys.stdin.readline() if args.key_stdin else getpass.getpass("请输入 AI-TOKEN API Key（不回显）: ")
                result = store.configure(value)
            elif args.command == "config-status":
                result = {"configured": (store.local / "config.json").is_file(), "config_path": str(store.local / "config.json")}
            elif args.command == "check-docs":
                result = check_docs(store)
            elif args.command == "tasks":
                result = [summary(json.loads(row[0])) for row in store.db.execute("SELECT body FROM tasks ORDER BY rowid DESC")]
            elif args.command == "status":
                result = summary(Client(store, Http()).get(args.task_id))
            else:
                # A recorded success can be re-downloaded after credential deletion/rotation.
                client = Client(store, Http() if args.command == "download" else Http(store.key()))
                if args.command == "models":
                    result = client.models(args.operation)
                elif args.command == "options":
                    result = option_menu(selected_model(client.models(args.operation), args.model_id), args.tier, args.ratio)
                elif args.command == "prepare":
                    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
                    result = client.prepare(args.task_id, prompt, args.output_dir, args.model_id, args.tier, args.ratio,
                                            args.quality, args.operation, args.image)
                elif args.command == "run":
                    result = client.run(args.task_id)
                elif args.command == "query":
                    result = client.query(args.task_id)
                elif args.command == "resume":
                    result = client.resume(args.task_id)
                else:
                    result = client.download(args.task_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except ClientError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    except Exception:
        print(json.dumps({"error": "本地配置、权限、网络或数据检查失败；勿重复提交，先查看任务状态。"}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    sys.exit(main())
