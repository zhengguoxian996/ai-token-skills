"""Offline regression tests: no credentials or paid/live API requests."""
import base64
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from PIL import Image

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "aitoken_image.py"
spec = importlib.util.spec_from_file_location("aitoken_image", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def fixtures():
    api, market, selector = {"data": []}, {"models": []}, {"models": []}
    for index, (route, price) in enumerate((("线路甲", 10), ("线路乙", 10), ("线路贵", 11))):
        name = "绘图模型"
        api["data"].append(dict(id=f"{route} - {name}", channel_name=route, display_name=name, capabilities=["image"]))
        market["models"].append(dict(channel_name=route, display_name=name, provider=route,
                                     api_integration_id=index, model_id="img", is_available=True, feature_labels=["文生图"]))
        rows = [dict(tier=tier, ratio=ratio, quality="high", site_price=price)
                for tier in ("1K", "2K", "4K") for ratio in ("1:1", "16:9", "A+")]
        selector["models"].append(dict(name=name, provider=route, api_integration_id=index, model_id="img",
            is_enabled=True, pricing_source="site", grsai_drawing_options=dict(quality_options=["high"],
            quality_default="high", parameter_rows=rows)))
    return api, market, selector


def png_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(buffer, format="PNG")
    return buffer.getvalue()


def success():
    return {"data": [{"b64_json": base64.b64encode(png_bytes()).decode()}]}


class FakeHttp:
    def __init__(self, responses=None):
        self.api, self.market, self.selector = fixtures()
        self.responses = list(responses or [success()])
        self.posts = []
        self.downloads = 0
        self.image_failure = False

    def json(self, url, payload=None, idempotency=None):
        if url == mod.GENERATE:
            self.posts.append((copy.deepcopy(payload), idempotency))
            answer = self.responses.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return copy.deepcopy({mod.MODELS: self.api, mod.MARKET: self.market, mod.SELECTOR: self.selector}[url])

    def image(self, url):
        self.downloads += 1
        if self.image_failure:
            raise OSError("simulated download failure")
        return png_bytes()


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = mod.Store(self.root / "skill")
        self.http = FakeHttp()
        self.client = mod.Client(self.store, self.http)
        self.output = self.root / "用户选定目录"
        self.model = "线路甲 - 绘图模型"

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def prepare(self, **kw):
        args = dict(task_id="request-001", prompt="一个真实用户任务", output_dir=str(self.output),
                    model_id=self.model, tier="1K", ratio="1:1")
        args.update(kw)
        return self.client.prepare(**args)

    def test_success_once_across_repeat_and_reopen(self):
        self.prepare()
        one = self.client.run("request-001")
        self.assertEqual(one["state"], "saved")
        self.store.close()
        self.store = mod.Store(self.root / "skill")
        self.client = mod.Client(self.store, self.http)
        two = self.client.run("request-001")
        self.assertEqual(one["saved_path"], two["saved_path"])
        self.assertTrue(Path(one["saved_path"]).is_absolute())
        self.assertEqual(Path(one["saved_path"]).parent, self.output)
        self.assertEqual(len(self.http.posts), 1)
        self.assertEqual(self.http.posts[0][0]["n"], 1)
        self.assertTrue(self.http.posts[0][1].startswith("aitoken-img-"))

    def test_confirmed_failure_switches_only_equal_price_once(self):
        self.http.responses = [mod.ApiError("failed", 400), success()]
        self.prepare()
        result = self.client.run("request-001")
        self.assertEqual(result["attempts"], 2)
        self.assertEqual([p[0]["model"] for p in self.http.posts], [self.model, "线路乙 - 绘图模型"])
        self.assertNotEqual(self.http.posts[0][1], self.http.posts[1][1])
        self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 2)

    def test_four_failures_exhaust_budget_and_cannot_restart(self):
        self.http.responses = [mod.ApiError("failed", 400) for _ in range(5)]
        self.prepare()
        result = self.client.run("request-001")
        self.assertEqual((result["state"], result["failures"]), ("exhausted", 4))
        self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 4)

    def test_unknown_timeout_does_not_retry(self):
        self.http.responses = [mod.ApiError("unknown", 504), success()]
        self.prepare()
        result = self.client.run("request-001")
        self.assertEqual(result["state"], "unknown")
        self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 1)

    def test_crash_after_dispatch_is_not_retried(self):
        self.http.responses = [KeyboardInterrupt()]
        self.prepare()
        with self.assertRaises(KeyboardInterrupt):
            self.client.run("request-001")
        self.assertEqual(self.store.read("request-001")["state"], "dispatching")
        self.assertEqual(self.client.run("request-001")["state"], "unknown")
        self.assertEqual(len(self.http.posts), 1)

    def test_crash_after_result_before_download_is_recoverable(self):
        self.prepare()
        with patch.object(self.client, "download", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.client.run("request-001")
        self.assertEqual(self.store.read("request-001")["state"], "succeeded")
        self.assertEqual(self.client.run("request-001")["state"], "saved")
        self.assertEqual(len(self.http.posts), 1)

    def test_download_failure_then_resume_never_generates(self):
        self.http.responses = [{"data": [{"url": "https://example.com/image.png?private=sample"}]}]
        self.http.image_failure = True
        self.prepare()
        one = self.client.run("request-001")
        self.assertEqual(one["state"], "download_pending")
        self.assertTrue(one["generation_succeeded"])
        self.assertNotIn("private=", json.dumps(one))
        self.http.image_failure = False
        two = self.client.run("request-001")
        self.assertEqual(two["state"], "saved")
        self.assertEqual(len(self.http.posts), 1)

    def test_invalid_image_is_success_pending_download_not_new_generation(self):
        self.http.responses = [{"data": [{"b64_json": base64.b64encode(b"not an image").decode()}]}]
        self.prepare()
        self.assertEqual(self.client.run("request-001")["state"], "download_pending")
        self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 1)

    def test_malformed_200_is_unknown(self):
        self.http.responses = [{"data": []}, success()]
        self.prepare()
        self.assertEqual(self.client.run("request-001")["state"], "unknown")
        self.assertEqual(len(self.http.posts), 1)

    def test_auth_balance_error_stops_without_fallback(self):
        self.http.responses = [mod.ApiError("blocked", 402), success()]
        self.prepare()
        self.assertEqual(self.client.run("request-001")["state"], "blocked")
        self.assertEqual(len(self.http.posts), 1)

    def test_changed_price_stops_before_submit(self):
        self.prepare()
        for row in self.http.selector["models"][0]["grsai_drawing_options"]["parameter_rows"]:
            row["site_price"] = 13
        self.assertEqual(self.client.run("request-001")["state"], "selection_changed")
        self.assertEqual(self.http.posts, [])

    def test_same_task_request_is_immutable(self):
        self.prepare()
        with self.assertRaises(mod.ClientError):
            self.prepare(prompt="changed prompt")
        self.assertEqual(self.http.posts, [])

    def test_directory_required_and_prepare_has_no_post(self):
        with self.assertRaises(mod.ClientError):
            self.prepare(output_dir="relative")
        self.prepare()
        self.assertEqual(self.http.posts, [])

    def test_existing_conflicting_file_never_overwritten(self):
        self.prepare()
        target = self.output / ("aitoken_" + mod.digest("request-001")[:24] + ".png")
        target.write_bytes(b"user-owned")
        self.assertEqual(self.client.run("request-001")["state"], "download_pending")
        self.assertEqual(target.read_bytes(), b"user-owned")

    def test_deleted_local_result_redownloads_only(self):
        self.prepare()
        result = self.client.run("request-001")
        Path(result["saved_path"]).unlink()
        result = self.client.run("request-001")
        self.assertTrue(Path(result["saved_path"]).exists())
        self.assertEqual(len(self.http.posts), 1)

    def test_readonly_status_omits_prompt_and_result(self):
        self.prepare()
        self.client.run("request-001")
        value = mod.summary(self.store.read("request-001"))
        self.assertNotIn("真实用户", json.dumps(value, ensure_ascii=False))
        self.assertNotIn("b64_json", value)

    def test_cross_process_lock_blocks_second_runner(self):
        code = "import importlib.util,sys; from pathlib import Path; s=importlib.util.spec_from_file_location('c',sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m);\nwith m.process_lock(Path(sys.argv[2])): print('UNEXPECTED')"
        path = self.store.local / "process.lock"
        with mod.process_lock(path):
            process = subprocess.run([sys.executable, "-X", "utf8", "-c", code, str(SCRIPT), str(path)],
                                     capture_output=True, text=True, encoding="utf-8")
        self.assertNotEqual(process.returncode, 0)
        self.assertNotIn("UNEXPECTED", process.stdout)
        self.assertIn("Another skill process is active", process.stderr)


class MetadataTests(unittest.TestCase):
    def test_models_are_numbered_and_only_authorized(self):
        api, market, selector = fixtures()
        api["data"] = api["data"][:1]
        result = mod.catalog(api, market, selector)
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["label"].startswith("1. 线路甲 | 绘图模型 —"))
        self.assertIn("10 积分", result[0]["label"])

    def test_ambiguous_selector_is_not_guessed(self):
        api, market, selector = fixtures()
        selector["models"].append(copy.deepcopy(selector["models"][0]))
        self.assertFalse(mod.catalog(api, market, selector)[0]["choices"])

    def test_unknown_price_fails_closed(self):
        api, market, selector = fixtures()
        selector["models"][0]["grsai_drawing_options"]["parameter_rows"][0]["site_price"] = None
        with self.assertRaises(mod.ClientError):
            mod.catalog(api, market, selector)

    def test_single_quality_auto_and_multiple_quality_ask(self):
        api, market, selector = fixtures()
        model = mod.catalog(api, market, selector)[0]
        self.assertFalse(mod.option_menu(model, "1K", "1:1")["ask_quality"])
        self.assertEqual(mod.choose(model, "1K", "1:1")["quality"], "high")
        model["choices"].append(dict(tier="1K", ratio="1:1", quality="low", credits="9"))
        model["quality_options"] = ["low", "high"]
        self.assertTrue(mod.option_menu(model, "1K", "1:1")["ask_quality"])
        with self.assertRaises(mod.ClientError):
            mod.choose(model, "1K", "1:1", "low")

    def test_cannot_silently_downgrade_4k_or_aplus(self):
        model = mod.catalog(*fixtures())[0]
        for tier, ratio in (("4K", "16:9"), ("2K", "A+")):
            with self.assertRaises(mod.ClientError):
                mod.choose(model, tier, ratio)
        choice = mod.choose(model, "2K", "16:9")
        request = mod.payload(dict(choice=choice, prompt="test"), model["id"])
        self.assertEqual((request["size"], request["quality"]), ("16:9", "hd"))


class TransportTests(unittest.TestCase):
    def test_key_saved_only_in_private_config(self):
        with tempfile.TemporaryDirectory() as folder:
            store = mod.Store(Path(folder))
            try:
                with patch.object(mod.Http, "json", return_value={"data": []}):
                    output = store.configure("test-only-not-a-real-key")
                self.assertNotIn("test-only", json.dumps(output))
                self.assertEqual(store.key(), "test-only-not-a-real-key")
                self.assertFalse((Path(folder) / "config.json").exists())
            finally:
                store.close()

    def test_auth_only_goes_to_models_or_generation(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return b'{"data": []}'
        with patch.object(urllib.request, "build_opener") as builder:
            builder.return_value.open.return_value = Response()
            client = mod.Http("test-only-secret")
            client.json(mod.MARKET)
            self.assertNotIn("Authorization", builder.return_value.open.call_args[0][0].headers)
            client.json(mod.MODELS)
            self.assertEqual(builder.return_value.open.call_args[0][0].headers["Authorization"], "Bearer test-only-secret")
        with self.assertRaises(mod.ClientError):
            client.json("https://example.com/key-stealer")

    def test_http_failure_classification_never_leaks_response(self):
        cases = [(400, {"code": "image_generation_failed", "message": "failed"}, "failed"),
                 (400, {"code": "image_generation_failed", "message": "idempotency key is still processing"}, "unknown"),
                 (504, {"code": "timeout", "message": "test-only-secret"}, "unknown"),
                 (502, {"code": "image_generation_failed", "message": "test-only-secret"}, "unknown"),
                 (402, {"code": "image_generation_failed", "message": "test-only-secret"}, "blocked")]
        for status, error, kind in cases:
            with self.subTest(status=status, kind=kind):
                exc = urllib.error.HTTPError(mod.GENERATE, status, "err", {}, io.BytesIO(json.dumps({"error": error}).encode()))
                with patch.object(urllib.request, "build_opener") as builder:
                    builder.return_value.open.side_effect = exc
                    with self.assertRaises(mod.ApiError) as context:
                        mod.Http("test-only-secret").json(mod.GENERATE, {"n": 1})
                    self.assertEqual(context.exception.kind, kind)
                    self.assertNotIn("test-only-secret", str(context.exception))
                    self.assertEqual(builder.return_value.open.call_count, 1)

    def test_private_and_non_https_image_urls_rejected(self):
        for url in ("file:///tmp/key", "http://example.com/a", "https://user:pass@example.com/a"):
            with self.assertRaises(mod.ClientError):
                mod.public_image_url(url)
        with patch.object(mod.socket, "getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
            with self.assertRaises(mod.ClientError):
                mod.public_image_url("https://localhost/image")


if __name__ == "__main__":
    unittest.main()
