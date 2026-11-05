"""Exercise desktop handoff through the Ledger daemon without a live app."""

import base64
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from test_azure_daemon import GRADE, SCHEMA, png


DAEMON_PATH = Path(__file__).resolve().parents[1] / "daemon" / "vertex-api-daemon.py"
MODEL = "codex/gpt-6.1-sol"
SYNTHETIC_SECRET = "synthetic-desktop-result-detail"


class CodexDaemonTest(unittest.TestCase):
    def setUp(self):
        temp_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.jobs = temp_dir / "desktop jobs"
        env_path = temp_dir / "codex-test.env"
        env_path.write_text(
            "LEDGER_CODEX_ENABLED=true\n"
            f"LEDGER_CODEX_JOB_DIR={self.jobs}\n"
            f"LEDGER_CODEX_TIMEOUT_S=1\n",
            encoding="utf-8",
        )
        self.enterContext(patch.dict(os.environ, {"LEDGER_ENV_PATH": str(env_path)}, clear=True))
        self.enterContext(patch.object(sys, "path", [str(DAEMON_PATH.parent), *sys.path]))

        spec = importlib.util.spec_from_file_location("ledger_codex_test_daemon", DAEMON_PATH)
        self.daemon = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.daemon)
        self.client = self.daemon.app.test_client()
        self.master = png("blue")
        self.live = png("red")

    def fake_desktop(self, result):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            request_path = next(self.jobs.glob("job-*/request.json"), None)
            if request_path is None:
                time.sleep(0.01)
                continue

            request = json.loads(request_path.read_text(encoding="utf-8"))
            job = request_path.parent
            self.assertEqual(request["job_id"], job.name)
            images = []
            for name in request["images"]:
                relative = Path(name)
                self.assertFalse(relative.is_absolute())
                self.assertNotIn("..", relative.parts)
                self.assertEqual(relative.suffix, ".png")
                images.append((job / relative).read_bytes())

            result_path = job / "result.json"
            staged_result = job / "desktop-result.tmp"
            staged_result.write_text(
                json.dumps({"job_id": request["job_id"], **result}), encoding="utf-8"
            )
            staged_result.replace(result_path)
            return request, images

        self.fail("Desktop peer did not observe a request manifest")

    def post_with_desktop(self, endpoint, payload, result):
        with ThreadPoolExecutor(max_workers=1) as executor:
            observed = executor.submit(self.fake_desktop, result)
            response = self.client.post(endpoint, json=payload)
            request, images = observed.result()
        self.assert_jobs_clean()
        return response, request, images

    def assert_jobs_clean(self):
        self.assertEqual(list(self.jobs.glob("job-*")), [])

    def stage_context(self):
        response = self.client.post("/api/cache/build", json={
            "system_instruction": "Grade against the blue master.",
            "images_b64": [self.master],
        })
        self.assertEqual(response.get_json(), {"status": "success", "text": "codex-inline"})
        self.assert_jobs_clean()

    def grade_payload(self, **images):
        return {
            "prompt": "Grade the synthetic answer.",
            "schema": SCHEMA,
            **images,
        }

    def assert_images(self, observed, *expected):
        self.assertEqual(observed, [base64.b64decode(image) for image in expected])

    def test_desktop_default_routes_ocr_to_the_job_directory(self):
        health = self.client.get("/").get_json()
        self.assertEqual(health["codex_route"], "desktop")
        self.assertEqual(health["codex_job_dir"], str(self.jobs.resolve()))
        self.assertEqual(health["codex_default_model"], MODEL)
        self.assertEqual(health["default_model"], MODEL)
        self.assertEqual(self.daemon._provider_for("gpt-6.1-sol"), "azure-openai")

        expected = {"bartonian_id": 125}
        response, request, images = self.post_with_desktop(
            "/api/silo1",
            {"prompt": "Read the synthetic ID.", "image_b64": self.live},
            {"text": json.dumps(expected)},
        )
        body = response.get_json()
        self.assertEqual(body["status"], "success")
        self.assertEqual(json.loads(body["text"]), expected)
        self.assertEqual(request["kind"], "ocr")
        self.assertEqual(request["model"], "gpt-6.1-sol")
        self.assertIn("Read the synthetic ID.", request["prompt"])
        self.assert_images(images, self.live)

    def test_namespaced_astra_routes_to_desktop(self):
        response, request, _ = self.post_with_desktop(
            "/api/silo1",
            {
                "model_name": "codex/gpt-6-astra",
                "prompt": "Read the synthetic title.",
                "image_b64": self.live,
            },
            {"text": json.dumps({"assignment_title": "Synthetic"})},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request["model"], "gpt-6-astra")

    def test_grading_scopes_essay_master_override_to_one_request(self):
        self.stage_context()
        replacement = png("green")
        page2 = png("yellow")
        response, request, images = self.post_with_desktop(
            "/api/silo2",
            self.grade_payload(images_b64=[self.live, page2], master_images_b64=[replacement]),
            {"results": [GRADE]},
        )
        self.assertEqual(response.get_json()["status"], "success")
        self.assertEqual(json.loads(response.get_json()["text"]), [GRADE])
        self.assertEqual(request["kind"], "grade")
        self.assertEqual(request["master_image_count"], 1)
        self.assertIn("Grade against the blue master.", request["instructions"])
        self.assertIn("Grade the synthetic answer.", request["prompt"])
        self.assertEqual(request["schema"]["type"], "object")
        self.assertEqual(request["schema"]["properties"]["results"]["type"], "array")
        self.assert_images(images, replacement, self.live, page2)

        response, request, images = self.post_with_desktop(
            "/api/silo2", self.grade_payload(image_b64=self.live), {"results": [GRADE]},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(request["master_image_count"], 1)
        self.assert_images(images, self.master, self.live)

    def test_cache_delete_clears_context_without_creating_a_job(self):
        self.stage_context()
        self.assertTrue(self.client.get("/").get_json()["cache_active"])
        self.assertEqual(self.client.post("/api/cache/delete").get_json()["status"], "success")
        self.assertFalse(self.client.get("/").get_json()["cache_active"])
        with self.assertLogs("ledger-daemon", level="ERROR"):
            response = self.client.post("/api/silo2", json=self.grade_payload(image_b64=self.live))
        self.assertEqual(response.status_code, 500)
        self.assertIn("No active evaluation context", response.get_json()["message"])
        self.assert_jobs_clean()

    def test_mismatched_job_and_invalid_grade_never_return_a_score(self):
        self.stage_context()
        cases = {
            "wrong job": {"job_id": "unrelated-job", "results": [GRADE]},
            "partial row": {"results": [{"feedback": SYNTHETIC_SECRET, "points_earned": 1}]},
            "invalid score": {"results": [{**GRADE, "points_earned": "1"}]},
        }
        for case, result in cases.items():
            with self.subTest(case=case):
                with self.assertLogs("ledger-daemon", level="ERROR") as logs:
                    response, _, _ = self.post_with_desktop(
                        "/api/silo2", self.grade_payload(image_b64=self.live), result,
                    )
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.get_json()["status"], "error")
                self.assertNotIn("text", response.get_json())
                self.assertNotIn(SYNTHETIC_SECRET, response.get_data(as_text=True))
                self.assertNotIn(SYNTHETIC_SECRET, "\n".join(logs.output))

    def test_missing_desktop_response_times_out_and_cleans_up(self):
        self.stage_context()
        with self.assertLogs("ledger-daemon", level="ERROR"):
            response = self.client.post("/api/silo2", json=self.grade_payload(image_b64=self.live))
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["status"], "error")
        self.assertIn("before the timeout", response.get_json()["message"])
        self.assertNotIn("text", response.get_json())
        self.assert_jobs_clean()


if __name__ == "__main__":
    unittest.main()
