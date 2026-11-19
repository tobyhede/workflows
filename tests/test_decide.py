"""The decide job's first step, run as the workflow writes it: the decision's input it builds from the run."""

import json
import pathlib
import tempfile
import unittest

from harness import ROOT, RUN_URL, run_step_as_written

CASE = ROOT / "request-decision" / "cases" / "ready-review-agent-author-fires-without-lookup"


class GivenAReadyRequestByTheReviewAgent(unittest.TestCase):
    def decide(self, **runner):
        event = json.loads((CASE / "input.json").read_text(encoding="utf-8"))["event"]
        with tempfile.TemporaryDirectory() as temp:
            event_path = pathlib.Path(temp) / "event.json"
            event_path.write_text(json.dumps(event), encoding="utf-8")
            result = run_step_as_written(
                "decide",
                "first",
                {
                    "REVIEW_AGENT": "cipherstash-agent",
                    "CHECKOUT_OUTCOME": "success",
                    "REQUEST_DECISION_FILE": str(ROOT / "request-decision" / "decision.jq"),
                },
                runner={"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event_path), **runner},
            )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.outputs["decision"])

    def test_the_request_text_ends_with_a_link_to_this_run(self):
        text = self.decide()["text"]
        self.assertTrue(text.endswith(f"\n\nSent by workflow run: {RUN_URL}"), text)

    def test_without_the_runs_details_the_request_is_sent_without_the_link(self):
        for name in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"):
            with self.subTest(missing=name):
                decision = self.decide(**{name: None})
                self.assertEqual(decision["decision"], "fire")
                self.assertNotIn("Sent by workflow run", decision["text"])


if __name__ == "__main__":
    unittest.main()
