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
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://www.ai-token.vip"
MODELS = BASE + "/v1/models"
MARKET = BASE + "/api/model-marketplace"
SELECTOR = BASE + "/api/content/ai-models/selector?section=generate"
GENERATE = BASE + "/v1/images/generations"
DOCS = BASE + "/dashboard/api-docs"
MAX_ATTEMPTS = 4
MAX_BYTES = 128 * 1024 * 1024


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
        if url not in {MODELS, MARKET, SELECTOR, GENERATE}:
            raise ClientError("Unrecognized API endpoint")
        headers = {"Accept": "application/json", "User-Agent": "AI-TOKEN-Image-Skill/1"}
        if url in {MODELS, GENERATE}:
            if not self.key:
                raise ClientError("API Key is missing; run configure")
            headers["Authorization"] = "Bearer " + self.key
        if idempotency:
            headers["Idempotency-Key"] = idempotency
        data = None if payload is None else encode(payload).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
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
            if "idempotency" in message.lower() or "processing" in message.lower():
                raise ApiError("unknown", status) from None
            if status in {401, 402, 403}:
                raise ApiError("blocked", status) from None
            if status in {400, 422} and (code in {"image_generation_failed", "invalid_request_error"}
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
        model.update(choices=[], problems=[])
        markets = [r for r in marketplace.get("models", [])
                   if r.get("channel_name") == m.get("channel_name")
                   and r.get("display_name") == m.get("display_name")
                   and r.get("is_available") is True and "文生图" in r.get("feature_labels", [])]
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
            "gateway_limitations": ["4K 仅可传 1:1", "A+ 不能正确传递", "多画质模型尚不能独立指定图片画质"]}


def choose(model, tier, ratio, quality=None):
    choices = [c for c in model["choices"] if c["tier"] == tier.upper() and c["ratio"] == ratio]
    qs = sorted({c["quality"] for c in choices})
    if quality is None:
        if len(qs) != 1:
            raise ClientError("Select an available picture quality first")
        quality = qs[0]
    rows = [c for c in choices if c["quality"] == quality]
    if len(rows) != 1:
        raise ClientError("Unsupported or ambiguous parameter combination")
    if len(model.get("quality_options") or []) > 1 or len({r["quality"] for r in model["choices"]}) > 1:
        raise ClientError("Current public gateway cannot independently select picture quality")
    choice = rows[0]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?:[0-9]+(?:\.[0-9]+)?", ratio):
        raise ClientError("Current public gateway cannot express this aspect ratio")
    if tier.upper() not in {"1K", "2K", "4K"} or (tier.upper() == "4K" and ratio != "1:1"):
        raise ClientError("Current public gateway cannot express this tier/ratio combination")
    return dict(choice)


def payload(plan, model_id):
    params = plan["choice"]
    return {"model": model_id, "prompt": plan["prompt"], "n": 1, "response_format": "url",
            "size": "4K" if params["tier"] == "4K" else params["ratio"],
            "quality": "hd" if params["tier"] == "2K" else "standard"}


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

    def models(self):
        rows = catalog(self.http.json(MODELS), self.http.json(MARKET), self.http.json(SELECTOR))
        write_json(self.store.local / "models.json", {"checked_at": time.time(), "models": rows})
        return rows

    def prepare(self, task_id, prompt, output_dir, model_id, tier, ratio, quality=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", task_id):
            raise ClientError("Task ID must contain 1-100 letters, digits, hyphens or underscores")
        if not 1 <= len(prompt.strip()) <= 20000:
            raise ClientError("Prompt must contain 1-20000 characters")
        directory = Path(output_dir).expanduser()
        if not directory.is_absolute():
            raise ClientError("Specify the user's absolute output directory")
        directory = directory.resolve()
        directory.mkdir(parents=True, exist_ok=True)
        request = dict(prompt=prompt, output_dir=str(directory), model_id=model_id,
                       tier=tier.upper(), ratio=ratio, quality=quality)
        existing = self.store.read(task_id)
        if existing:
            if existing["request"] != request:
                raise ClientError("Task ID already has a different immutable request")
            return summary(existing)
        # Verify writable output before any paid submission.
        with tempfile.TemporaryFile(dir=directory) as probe:
            probe.write(b"ok")
        model = selected_model(self.models(), model_id)
        choice = choose(model, tier, ratio, quality)
        job = dict(task_id=task_id, request=request, plan=dict(prompt=prompt, output_dir=str(directory),
                   model_id=model_id, choice=choice), state="ready", attempts=0, failures=0,
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
        if job["state"] not in {"ready", "retryable"}:
            return summary(job)
        while job["attempts"] < MAX_ATTEMPTS:
            try:
                models = self.models()
                plan = job["plan"]
                candidates = []
                for model in models:
                    c = plan["choice"]
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
            job["history"].append(dict(attempt=number, model_id=selected, idempotency_key=idem, state="dispatching"))
            self.store.save(job)  # Durable before network dispatch.
            try:
                result = self.http.json(GENERATE, payload(plan, selected), idem)
            except ApiError as exc:
                job["history"][-1].update(state=exc.kind, http_status=exc.status)
                if exc.kind == "failed":
                    job["failures"] += 1
                    job["state"] = "exhausted" if job["attempts"] >= MAX_ATTEMPTS else "retryable"
                    self.store.save(job)
                    if job["state"] == "retryable":
                        continue
                else:
                    job["state"] = exc.kind
                    self.store.save(job)
                return summary(job)
            # A malformed 2xx is uncertain, not permission for another POST.
            items = result.get("data") if isinstance(result, dict) and not result.get("error") else None
            item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
            if not (item.get("url") or item.get("b64_json")):
                job["state"] = "unknown"
                job["history"][-1]["state"] = "unknown"
                self.store.save(job)
                return summary(job)
            job["result"] = {k: item[k] for k in ("url", "b64_json") if item.get(k)}
            job["state"] = "succeeded"
            job["history"][-1]["state"] = "succeeded"
            self.store.save(job)  # A download error must never reopen generation.
            return self.download(task_id)
        job["state"] = "exhausted"
        self.store.save(job)
        return summary(job)

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
    result = {k: job.get(k) for k in ("task_id", "state", "attempts", "failures", "saved_path", "dimensions")}
    result["generation_succeeded"] = bool(job.get("result"))
    result["model_id"] = job["history"][-1]["model_id"] if job["history"] else job["plan"]["model_id"]
    result["parameters"] = job["plan"]["choice"]
    result["output_dir"] = job["plan"]["output_dir"]
    result["max_attempts"] = MAX_ATTEMPTS
    messages = {
        "unknown": "服务端结果未知；请核对 API 使用记录，禁止重新生成。",
        "dispatching": "请求已记录为发送中；不可重复提交。",
        "exhausted": "已明确失败 4 次。是否需要重新选择其他线路模型开启新任务？",
        "download_pending": "生图已成功，只能补下载；禁止再次生图。",
        "selection_changed": "可用模型、价格或参数发生变化，需要重新选择；尚未再次提交。",
        "blocked": "认证、账户余额或额度阻止调用，请处理后再安排任务。",
    }
    if job["state"] in messages:
        result["message"] = messages[job["state"]]
    return result


def check_docs(store):
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(DOCS, timeout=30) as response:
        html = response.read(4 * 1024 * 1024).decode("utf-8")
    match = re.search(r'src="([^"<>]*dashboard/api-docs/page-[^"<>]+\.js)"', html)
    if not match or not match[1].startswith("/_next/static/"):
        raise ClientError("Documentation page structure changed; inspect it manually")
    with opener.open(BASE + match[1], timeout=30) as response:
        data = response.read(8 * 1024 * 1024)
    observed = hashlib.sha256(data).hexdigest()
    contract = json.loads((store.root / "config" / "api-contract.json").read_text(encoding="utf-8"))
    value = dict(documentation_url=DOCS, checked_at=time.time(), observed_sha256=observed,
                 reviewed_sha256=contract["documentation_bundle_sha256"],
                 needs_review=observed != contract["documentation_bundle_sha256"])
    write_json(store.local / "docs-check.json", value)
    return value


def main():
    parser = argparse.ArgumentParser(description="图片生成(Ai-token): persistent, single-success image tasks")
    sub = parser.add_subparsers(dest="command", required=True)
    config = sub.add_parser("configure")
    config.add_argument("--key-stdin", action="store_true")
    for cmd in ("config-status", "check-docs", "models", "tasks"):
        sub.add_parser(cmd)
    options = sub.add_parser("options")
    options.add_argument("--model-id", required=True)
    options.add_argument("--tier")
    options.add_argument("--ratio")
    prepare = sub.add_parser("prepare")
    for flag in ("task-id", "prompt-file", "output-dir", "model-id", "tier", "ratio"):
        prepare.add_argument("--" + flag, required=True)
    prepare.add_argument("--quality")
    for cmd in ("run", "download", "status"):
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
                    result = client.models()
                elif args.command == "options":
                    result = option_menu(selected_model(client.models(), args.model_id), args.tier, args.ratio)
                elif args.command == "prepare":
                    prompt = Path(args.prompt_file).read_text(encoding="utf-8")
                    result = client.prepare(args.task_id, prompt, args.output_dir, args.model_id, args.tier, args.ratio, args.quality)
                elif args.command == "run":
                    result = client.run(args.task_id)
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
