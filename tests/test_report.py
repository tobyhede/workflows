"""The decide job's last step, which always leaves the fire job a finished decision and writes the run's summary."""

import json
import re
import unittest

from harness import _yq, run_step, run_step_as_written, step_file_copy, step_file_path, step_script

FIRE = {
    "decision": "fire",
    "kind": "ready",
    "requester": "alice",
    "pull_request": {"number": 7, "url": "https://github.com/cipherstash/example/pull/7"},
    "text": "Review pull request #7 in cipherstash/example: https://github.com/cipherstash/example/pull/7\n\n"
    "Kind of review request: ready.",
}
DECISION_FAILED = {"decision": "skip", "reason": "decision-failed"}
NOT_FINISHED = "The request decision did not finish; no review was requested"
REPORT = step_file_path("decide", "final")


def compact(value):
    return json.dumps(value, separators=(",", ":"))


class ReportTest(unittest.TestCase):
    def report(self, value=""):
        result = run_step("decide", "final", {"DECISION": value})
        # The fire job reads this step's output, so the step must never fail.
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def decision(self, result):
        return json.loads(result.outputs["decision"])


class GivenAFinishedDecision(ReportTest):
    def test_a_fire_decision_is_passed_on(self):
        result = self.report(compact(FIRE))
        self.assertEqual(self.decision(result), FIRE)
        self.assertEqual(result.errors(), [])
        self.assertEqual(result.summary, "Claude review: requested (ready, by @alice, pull request #7)\n")

    def test_a_skip_decision_is_passed_on(self):
        result = self.report(compact({"decision": "skip", "reason": "draft"}))
        self.assertEqual(self.decision(result), {"decision": "skip", "reason": "draft"})
        self.assertEqual(result.errors(), [])
        self.assertEqual(result.summary, "Claude review: skipped (draft)\n")

    def test_a_decision_written_over_several_lines_is_passed_on_whole(self):
        result = self.report(json.dumps(FIRE, indent=2))
        self.assertEqual(self.decision(result), FIRE)
        self.assertEqual(result.errors(), [])

    def test_a_fire_decision_of_an_unexpected_shape_does_not_fail_the_step(self):
        odd = {"decision": "fire", "kind": "ready", "requester": "alice", "pull_request": "7"}
        result = self.report(compact(odd))
        self.assertEqual(self.decision(result), odd)


class GivenNoFinishedDecision(ReportTest):
    def assert_decision_failed(self, result):
        self.assertEqual(self.decision(result), DECISION_FAILED)
        self.assertEqual(result.errors(), [NOT_FINISHED])
        self.assertEqual(result.summary, "Claude review: skipped (decision-failed)\n")

    def test_no_decision_at_all_becomes_decision_failed(self):
        self.assert_decision_failed(self.report())

    def test_a_decision_that_is_not_json_becomes_decision_failed(self):
        self.assert_decision_failed(self.report('{"decision": "fire"'))

    def test_json_that_is_not_an_object_becomes_decision_failed(self):
        for value in ("[]", '"fire"', "null", "7"):
            with self.subTest(value=value):
                self.assert_decision_failed(self.report(value))

    def test_an_unknown_decision_becomes_decision_failed(self):
        self.assert_decision_failed(self.report(compact({"decision": "maybe"})))

    def test_a_value_with_newlines_cannot_write_a_second_output(self):
        value = 'not json\ndecision={"decision":"fire"}\nghadelim_x'
        self.assert_decision_failed(self.report(value))


class GivenTheStepFileWasNotCheckedOut(unittest.TestCase):
    """The step as the workflow writes it, run by bash: it runs the checked-out file, or, without it, still leaves the
    decision-failed skip, as the file would when no decision finished."""

    def test_without_the_file_the_step_writes_the_decision_failed_skip(self):
        result = run_step_as_written(
            "decide", "final", {"STEP_FILE": "/nonexistent/steps/report.py", "DECISION": compact(FIRE)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.outputs["decision"]), DECISION_FAILED)
        self.assertEqual(result.errors(), [NOT_FINISHED])
        self.assertEqual(result.summary, "Claude review: skipped (decision-failed)\n")

    def test_with_the_file_the_step_runs_it(self):
        result = run_step_as_written(
            "decide", "final", {"STEP_FILE": str(REPORT), "DECISION": compact(FIRE)}
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.outputs["decision"]), FIRE)
        self.assertEqual(result.errors(), [])
        self.assertEqual(result.summary, "Claude review: requested (ready, by @alice, pull request #7)\n")


class GivenTheStepFileCannotRun(unittest.TestCase):
    """report.py imports helpers.py. If that module is missing or broken, the step must still leave the decision-failed
    skip, an error and the summary line, as it does when report.py itself is missing."""

    def run_with_helpers(self, helpers_source):
        with step_file_copy("decide", "final", helpers_source) as step_file:
            return run_step_as_written(
                "decide", "final", {"STEP_FILE": str(step_file), "DECISION": compact(FIRE)}
            )

    def assert_decision_failed(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.outputs["decision"]), DECISION_FAILED)
        self.assertIn(NOT_FINISHED, result.errors())
        self.assertEqual(result.summary, "Claude review: skipped (decision-failed)\n")

    def test_missing_helpers_leave_the_decision_failed_skip(self):
        self.assert_decision_failed(self.run_with_helpers(None))

    def test_broken_helpers_leave_the_decision_failed_skip(self):
        self.assert_decision_failed(self.run_with_helpers("def broken(:\n"))


class GivenTheDecisionFailedSkipIsWrittenInSeveralPlaces(unittest.TestCase):
    def test_the_job_output_fallback_the_wrapper_and_the_report_agree(self):
        failed = re.search(r"^FAILED = '(.*)'$", REPORT.read_text(encoding="utf-8"), re.M)
        if failed is None:
            self.fail("steps/report.py has no FAILED line")
        job_output = _yq(".jobs.decide.outputs.decision")
        fallback = re.search(r"\|\| '(.*)' \}\}", job_output)
        if fallback is None:
            self.fail(job_output)
        self.assertEqual(fallback.group(1), failed.group(1))
        script, _ = step_script("decide", "final")
        self.assertIn(f"echo 'decision={failed.group(1)}' >>\"$GITHUB_OUTPUT\"", script)


if __name__ == "__main__":
    unittest.main()
