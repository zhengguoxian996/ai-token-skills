"""Offline regression tests: no credentials or paid/live API requests."""
import base64
import copy
from email.parser import BytesParser
from email.policy import default
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
import urllib.parse
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
        api["data"].append(dict(id=f"{route} - {name}", channel_name=route, display_name=name, capabilities=["image"],
            office_image_contract=dict(version=2, explicit_parameters=True, supports_reference=True, max_reference_images=4)))
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
        self.query_responses = []
        self.queries = []
        self.resume_posts = []
        self.edit_posts = []

    def answer(self, responses):
        answer = responses.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return copy.deepcopy(answer)

    def json(self, url, payload=None, idempotency=None):
        if url == mod.GENERATE:
            self.posts.append((copy.deepcopy(payload), idempotency))
            return self.answer(self.responses)
        if url.startswith(mod.RESULTS + "?"):
            self.queries.append(url)
            return self.answer(self.query_responses) if self.query_responses else {"status": "unknown", "data": []}
        if url.startswith(mod.RESUME):
            self.resume_posts.append(url)
            return {"status": "running"}
        return copy.deepcopy({mod.MODELS: self.api, mod.MARKET: self.market,
                              mod.SELECTOR: self.selector, mod.EDIT_SELECTOR: self.selector}[url])

    def edit(self, fields, references, idempotency):
        self.posts.append((copy.deepcopy(fields), idempotency))
        self.edit_posts.append(mod.read_references(references))
        return self.answer(self.responses)

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

    def test_prompt_limit_applies_to_transmitted_text_including_whitespace(self):
        with self.assertRaises(mod.ClientError):
            self.prepare(prompt="x" + " " * 20000)
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


class EditingAndRecoveryTests(unittest.TestCase):
    setUp = WorkflowTests.setUp
    tearDown = WorkflowTests.tearDown
    prepare = WorkflowTests.prepare

    def reference(self, name="素材.png"):
        path = self.root / name
        path.write_bytes(png_bytes())
        return path

    def waiting(self, status="query_paused", can_resume=True, **extra):
        return dict(status=status, data=[], tasks=[dict(task_id=123, status=status, can_resume=can_resume)], **extra)

    def test_edit_uploads_approved_original_bytes_once(self):
        images = [self.reference(), self.reference("参考图二.png")]
        prepared = self.prepare(operation="edit", images=images, tier="4K", ratio="16:9")
        self.assertEqual(prepared["operation"], "edit")
        self.assertEqual(prepared["reference_count"], 2)
        self.assertEqual(self.http.posts, [])
        result = self.client.run("request-001")
        self.assertEqual(result["state"], "saved")
        self.assertEqual([raw for _, raw in self.http.edit_posts[0]], [p.read_bytes() for p in images])
        self.assertEqual(self.http.posts[0][0]["aspect_ratio"], "16:9")
        self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 1)

    def test_mode_and_reference_count_must_be_explicit(self):
        image = self.reference()
        for args in [dict(operation="edit"), dict(images=[image]),
                     dict(operation="edit", images=[image] * 5)]:
            with self.subTest(args=args), self.assertRaises(mod.ClientError):
                self.prepare(**args)
        self.assertEqual(self.http.posts, [])

    def test_edit_checks_model_reference_capability_and_count(self):
        image = self.reference()
        contract = self.http.api["data"][0]["office_image_contract"]
        contract["supports_reference"] = False
        with self.assertRaises(mod.ClientError):
            self.prepare(operation="edit", images=[image])
        contract.update(supports_reference=True, max_reference_images=1)
        with self.assertRaises(mod.ClientError):
            self.prepare(operation="edit", images=[image, image])
        self.assertIsNone(self.store.read("request-001"))

    def test_edit_tampered_reference_cannot_reach_network(self):
        image = self.reference()
        self.prepare(operation="edit", images=[image])
        image.write_bytes(b"changed after quote")
        with self.assertRaises(mod.ClientError):
            self.client.run("request-001")
        self.assertEqual(self.http.posts, [])
        self.assertEqual(self.store.read("request-001")["attempts"], 0)

    def test_edit_request_is_immutable_across_modes_and_files(self):
        image = self.reference()
        self.prepare(operation="edit", images=[image])
        for args in [dict(), dict(operation="edit", images=[self.reference("other.png")])]:
            with self.assertRaises(mod.ClientError):
                self.prepare(**args)

    def test_edit_fallback_never_uses_model_without_reference_support(self):
        self.http.responses = [mod.ApiError("failed", 400), success()]
        self.http.api["data"][1]["office_image_contract"]["supports_reference"] = False
        self.prepare(operation="edit", images=[self.reference()])
        self.assertEqual(self.client.run("request-001")["state"], "saved")
        self.assertEqual([p[0]["model"] for p in self.http.posts], [self.model, self.model])
        self.assertEqual(len(self.http.edit_posts), 2)

    def test_pending_then_query_success_never_resubmits(self):
        self.http.responses = [self.waiting("processing", False)]
        self.prepare()
        self.assertEqual(self.client.run("request-001")["state"], "pending")
        self.store.close()
        self.store = mod.Store(self.root / "skill")
        self.client = mod.Client(self.store, self.http)
        self.assertEqual(self.client.run("request-001")["state"], "pending")
        self.http.query_responses = [success()]
        self.assertEqual(self.client.query("request-001")["state"], "saved")
        self.assertEqual(len(self.http.posts), 1)

    def test_query_edit_uses_original_key_and_endpoint(self):
        self.http.responses = [mod.ApiError("unknown", 504)]
        self.prepare(operation="edit", images=[self.reference()])
        self.client.run("request-001")
        self.http.query_responses = [success()]
        result = self.client.query("request-001")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.http.queries[0]).query)
        self.assertEqual(query["endpoint"], ["/v1/images/edits"])
        self.assertEqual(query["idempotency_key"], [self.http.posts[0][1]])
        self.assertEqual(result["state"], "saved")
        self.assertEqual(len(self.http.posts), 1)

    def test_query_404_and_unavailable_do_not_unlock_unknown(self):
        self.http.responses = [mod.ApiError("unknown", 504)]
        self.prepare()
        self.client.run("request-001")
        for kind in ("not_found", "unknown", "blocked"):
            self.http.query_responses = [mod.ApiError(kind, 404)]
            self.assertEqual(self.client.query("request-001")["state"], "unknown")
            self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 1)

    def test_query_confirmed_unbilled_failure_can_use_remaining_budget(self):
        self.http.responses = [mod.ApiError("unknown", 504), success()]
        self.prepare()
        self.client.run("request-001")
        failure = dict(status="failed", data=[], tasks=[], billed_amount=0, reserved_amount=0)
        self.http.query_responses = [failure, failure]
        self.assertEqual(self.client.query("request-001")["state"], "retryable")
        self.assertEqual(self.client.query("request-001")["failures"], 1)
        self.assertEqual(self.client.run("request-001")["state"], "saved")
        self.assertEqual(len(self.http.posts), 2)
        self.assertNotEqual(self.http.posts[0][1], self.http.posts[1][1])

    def test_query_missing_or_nonzero_accounting_never_authorizes_retry(self):
        self.http.responses = [mod.ApiError("unknown", 504)]
        self.prepare()
        self.client.run("request-001")
        for accounting in ({}, dict(billed_amount=1, reserved_amount=0),
                           dict(billed_amount=0, reserved_amount=1), dict(billed_amount="NaN", reserved_amount=0)):
            self.http.query_responses = [dict(status="failed", data=[], **accounting)]
            self.assertEqual(self.client.query("request-001")["state"], "unknown")
            self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 1)

    def test_repeated_failure_query_counts_attempt_once_even_after_pending(self):
        self.http.responses = [mod.ApiError("unknown", 504)]
        self.prepare()
        self.client.run("request-001")
        failure = dict(status="failed", data=[], tasks=[], billed_amount=0, reserved_amount=0)
        self.http.query_responses = [failure, self.waiting("processing", False), failure]
        for _ in range(3):
            self.client.query("request-001")
        self.assertEqual(self.store.read("request-001")["failures"], 1)

    def test_success_with_missing_result_is_sticky_across_failure_response(self):
        self.http.responses = [self.waiting("result_fetch_failed")]
        self.prepare()
        initial = self.client.run("request-001")
        self.assertTrue(initial["generation_succeeded"])
        self.http.query_responses = [dict(status="failed", data=[], billed_amount=0, reserved_amount=0)]
        self.assertEqual(self.client.query("request-001")["state"], "result_fetch_failed")
        self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 1)

    def test_inconsistent_failure_payload_does_not_authorize_new_generation(self):
        self.http.responses = [mod.ApiError("unknown", 504)]
        self.prepare()
        self.client.run("request-001")
        for task_status in ("running", "query_paused", "succeeded"):
            self.http.query_responses = [self.waiting("failed", False, billed_amount=0, reserved_amount=0)]
            self.http.query_responses[0]["tasks"][0]["status"] = task_status
            result = self.client.query("request-001")
            self.assertNotEqual(result["state"], "retryable")
            self.client.run("request-001")
        self.assertTrue(result["generation_succeeded"])
        self.assertEqual(len(self.http.posts), 1)

    def test_resume_refreshes_permission_and_only_posts_original_task(self):
        paused = self.waiting()
        self.http.responses = [paused]
        self.prepare()
        self.client.run("request-001")
        self.http.query_responses = [paused, success()]
        result = self.client.resume("request-001")
        self.assertEqual(result["state"], "saved")
        self.assertEqual(self.http.resume_posts, [mod.RESUME + "123/resume"])
        self.assertEqual(len(self.http.posts), 1)
        self.assertEqual(self.store.read("request-001")["attempts"], 1)

    def test_resume_needs_fresh_permission_and_respects_admin_flag(self):
        self.http.responses = [self.waiting()]
        self.prepare()
        self.client.run("request-001")
        for reply in (self.waiting(can_resume=False), self.waiting(requires_admin=True),
                      {"status": "unknown", "data": []}, mod.ApiError("not_found", 404)):
            self.http.query_responses = [reply]
            self.client.resume("request-001")
        self.assertEqual(self.http.resume_posts, [])
        self.assertEqual(len(self.http.posts), 1)

    def test_resume_timeout_does_not_retry_resume_or_generate(self):
        paused = self.waiting()
        self.http.responses = [paused]
        self.prepare()
        self.client.run("request-001")
        self.http.query_responses = [paused]
        original_json = self.http.json
        def timeout(url, *args):
            if url.startswith(mod.RESUME):
                self.http.resume_posts.append(url)
                raise mod.ApiError("unknown", 504)
            return original_json(url, *args)
        with patch.object(self.http, "json", side_effect=timeout):
            result = self.client.resume("request-001")
        self.assertEqual(result["state"], "query_paused")
        self.assertEqual(self.http.resume_posts, [mod.RESUME + "123/resume"])
        self.assertEqual(self.store.read("request-001")["resume_history"][-1]["state"], "unknown")
        self.client.run("request-001")
        self.assertEqual(len(self.http.posts), 1)

    def test_local_status_does_not_query_or_expose_reference_paths(self):
        self.prepare(operation="edit", images=[self.reference()])
        result = mod.summary(self.client.get("request-001"))
        self.assertNotIn("素材.png", json.dumps(result, ensure_ascii=False))
        self.assertEqual(self.http.queries, [])

    def test_legacy_unsubmitted_quote_is_not_reinterpreted(self):
        self.prepare()
        job = self.client.get("request-001")
        job.pop("api_contract")
        job["request"].pop("operation")
        job["request"].pop("references")
        self.store.save(job)
        self.prepare()
        self.assertEqual(self.client.run("request-001")["state"], "selection_changed")
        self.assertEqual(self.http.posts, [])

    def test_legacy_unknown_request_keeps_original_idempotency_key(self):
        self.prepare()
        job = self.client.get("request-001")
        job.pop("api_contract")
        job.update(state="unknown", attempts=1,
                   history=[dict(attempt=1, model_id=self.model, idempotency_key="legacy-original", state="unknown")])
        self.store.save(job)
        self.http.query_responses = [success()]
        self.assertEqual(self.client.query("request-001")["state"], "saved")
        self.assertIn("idempotency_key=legacy-original", self.http.queries[0])
        self.assertEqual(self.http.posts, [])


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
            mod.choose(model, "1K", "1:1")
        self.assertEqual(mod.choose(model, "1K", "1:1", "low")["credits"], "9")

    def test_explicit_4k_aplus_and_quality_are_not_downgraded(self):
        model = mod.catalog(*fixtures())[0]
        for tier, ratio in (("4K", "16:9"), ("2K", "A+")):
            choice = mod.choose(model, tier, ratio)
            request = mod.payload(dict(choice=choice, prompt="test"), model["id"])
            self.assertEqual((request["image_size"], request["aspect_ratio"], request["image_quality"]), (tier, ratio, "high"))
            self.assertNotIn("size", request)
            self.assertNotIn("quality", request)
        with self.assertRaises(mod.ClientError):
            mod.choose(model, "8K", "16:9")

    def test_explicit_contract_required(self):
        model = mod.catalog(*fixtures())[0]
        model["office_image_contract"] = {}
        with self.assertRaises(mod.ClientError):
            mod.choose(model, "1K", "1:1")

    def test_no_independent_quality_transmits_empty_string(self):
        api, market, selector = fixtures()
        for model in selector["models"]:
            model["grsai_drawing_options"]["quality_options"] = []
            for row in model["grsai_drawing_options"]["parameter_rows"]:
                row["quality"] = ""
        model = mod.catalog(api, market, selector)[0]
        choice = mod.choose(model, "1K", "1:1")
        self.assertEqual(mod.payload(dict(choice=choice, prompt="test"), model["id"])["image_quality"], "")


class TransportTests(unittest.TestCase):
    def test_multipart_contains_original_images_and_exact_parameters(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return b'{"status":"processing","data":[]}'
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "参考图名字.png"
            image.write_bytes(png_bytes())
            references = mod.reference_files([str(image), str(image)])
            fields = dict(model="线路甲 - 绘图模型", prompt="替换背景\r\n保留商品", n=1,
                          image_size="4K", aspect_ratio="16:9", image_quality="", response_format="url", result_delivery="storage")
            with patch.object(urllib.request, "build_opener") as builder:
                builder.return_value.open.return_value = Response()
                result = mod.Http("test-only-secret").edit(fields, references, "original-key")
                self.assertEqual(result["status"], "processing")
                request = builder.return_value.open.call_args[0][0]
                self.assertEqual(request.get_method(), "POST")
                self.assertEqual(request.full_url, mod.EDIT)
                self.assertEqual(request.get_header("Authorization"), "Bearer test-only-secret")
                self.assertEqual(request.get_header("Idempotency-key"), "original-key")
                message = BytesParser(policy=default).parsebytes(
                    ("Content-Type: " + request.get_header("Content-type") + "\r\n\r\n").encode() + request.data)
                text_fields, files = {}, []
                for part in message.iter_parts():
                    name = part.get_param("name", header="content-disposition")
                    raw = part.get_payload(decode=True)
                    if name == "image":
                        files.append(raw)
                        self.assertEqual(part.get_content_type(), "image/png")
                    else:
                        text_fields[name] = raw.decode("utf-8")
                self.assertEqual(files, [png_bytes(), png_bytes()])
                self.assertEqual(text_fields, {k: str(v) for k, v in fields.items()})

    def test_new_endpoints_keep_auth_scoped_to_platform_and_gets_read_only(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, *args): return b'{}'
        with patch.object(urllib.request, "build_opener") as builder:
            builder.return_value.open.return_value = Response()
            client = mod.Http("test-only-secret")
            for url in (mod.EDIT_SELECTOR, mod.DOCS_CATALOG, mod.DOCS_MODELS + "model-1?revision=rev-1"):
                client.json(url)
                self.assertNotIn("Authorization", builder.return_value.open.call_args[0][0].headers)
            for url, body, method in ((mod.RESULTS + "?idempotency_key=original", None, "GET"),
                                      (mod.RESUME + "123/resume", {}, "POST")):
                client.json(url, body)
                request = builder.return_value.open.call_args[0][0]
                self.assertEqual(request.get_method(), method)
                self.assertEqual(request.get_header("Authorization"), "Bearer test-only-secret")
            for url in (mod.RESULTS + "?idempotency_key=original", mod.MARKET):
                with self.assertRaises(mod.ClientError):
                    client.json(url, {})
            for url in ("https://example.com/v1/images/results", mod.RESUME + "../models/resume"):
                with self.assertRaises(mod.ClientError):
                    client.json(url)

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
                 (402, {"code": "image_generation_failed", "message": "test-only-secret"}, "blocked"),
                 (400, {"code": "image_edit_failed", "message": "failed"}, "failed")]
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

    def test_error_with_existing_task_is_not_classified_as_terminal_failure(self):
        body = {"error": {"code": "image_generation_failed"}, "tasks": [{"task_id": 1, "status": "running"}]}
        error = urllib.error.HTTPError(mod.GENERATE, 400, "failed", {}, io.BytesIO(json.dumps(body).encode()))
        with patch.object(urllib.request, "build_opener") as builder:
            builder.return_value.open.side_effect = error
            with self.assertRaises(mod.ApiError) as context:
                mod.Http("test-only-secret").json(mod.GENERATE, {"n": 1})
            self.assertEqual(context.exception.kind, "unknown")

    def test_private_and_non_https_image_urls_rejected(self):
        for url in ("file:///tmp/key", "http://example.com/a", "https://user:pass@example.com/a"):
            with self.assertRaises(mod.ClientError):
                mod.public_image_url(url)
        with patch.object(mod.socket, "getaddrinfo", return_value=[(None, None, None, None, ("127.0.0.1", 443))]):
            with self.assertRaises(mod.ClientError):
                mod.public_image_url("https://localhost/image")


class DocumentationTests(unittest.TestCase):
    def test_published_json_changes_are_detected_without_updating_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = mod.Store(root)
            try:
                (root / "config").mkdir()
                catalog = {"published": True, "revision": "rev-1", "models": []}
                document = {"operations": [{"path": "/v1/images/edits"}]}
                fingerprint = mod.digest(mod.encode(dict(catalog=catalog, document=document)))
                contract = dict(reviewed_image_document_id="image-1", documentation_revision="rev-1", documentation_sha256=fingerprint)
                mod.write_json(root / "config" / "api-contract.json", contract)
                with patch.object(mod.Http, "json", side_effect=[catalog, document]):
                    self.assertFalse(mod.check_docs(store)["needs_review"])
                document["operations"].append({"path": "/v1/images/results"})
                with patch.object(mod.Http, "json", side_effect=[catalog, document]):
                    self.assertTrue(mod.check_docs(store)["needs_review"])
                self.assertEqual(json.loads((root / "config" / "api-contract.json").read_text(encoding="utf-8")), contract)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
