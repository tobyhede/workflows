"""The self-test workflow, .github/workflows/self-test.yml: what it calls, with which routine, and when."""

import re
import unittest

from harness import SELF_TEST, _yq, file_steps, step_field, step_file_path, step_shells


def value(query):
    return _yq(query, SELF_TEST).strip()


class SelfTestWorkflowTest(unittest.TestCase):
    def test_it_runs_on_the_ready_request_events(self):
        self.assertEqual(value(".on | keys | .[]"), "pull_request")
        self.assertEqual(value(".on.pull_request.types[]").split(), ["opened", "reopened", "ready_for_review"])

    def test_it_calls_this_pull_requests_own_review_workflow(self):
        # A local path, so the version under test is the pull request's, not a pinned commit.
        self.assertEqual(value(".jobs.review.uses"), "./.github/workflows/claude-review.yml")

    def test_it_uses_the_stub_routines_trigger_and_token(self):
        self.assertEqual(
            value('.jobs.review.with."routine-trigger-id"'), "${{ vars.CLAUDE_ROUTINE_INTEGRATION_TEST_TRIGGER_ID }}"
        )
        self.assertEqual(
            value(".jobs.review.secrets.CLAUDE_ROUTINE_TOKEN"), "${{ secrets.CLAUDE_ROUTINE_INTEGRATION_TEST_TOKEN }}"
        )

    def test_it_never_names_the_real_routines_trigger_or_token(self):
        text = SELF_TEST.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"secrets\.CLAUDE_ROUTINE_TOKEN\b", text))
        self.assertIsNone(re.search(r"\bvars\.CLAUDE_ROUTINE_TRIGGER_ID\b", text))
        self.assertIsNone(re.search(r"secrets\s*:\s*inherit", text))

    def test_the_skipped_job_runs_exactly_when_the_review_job_does_not(self):
        # Each condition names the same three tests, one negated in the other.
        review = " ".join(value(".jobs.review.if").split())
        skipped = " ".join(value(".jobs.skipped.if").split())
        self.assertEqual(
            review,
            "github.event.pull_request.draft == false "
            "&& github.event.pull_request.head.repo.full_name == github.repository "
            "&& github.event.pull_request.user.type != 'Bot'",
        )
        self.assertEqual(
            skipped,
            "github.event.pull_request.draft || github.event.pull_request.head.repo.full_name != github.repository "
            "|| github.event.pull_request.user.type == 'Bot'",
        )

    def test_the_verify_job_waits_for_the_review_job_and_only_reads(self):
        self.assertEqual(value(".jobs.verify.needs"), "review")
        self.assertEqual(value(".jobs.verify.if"), "${{ !cancelled() && needs.review.result != 'skipped' }}")
        self.assertEqual(
            value('.jobs.verify.permissions | to_entries | map(.key + "=" + .value) | .[]').split(),
            ["contents=read", "issues=read", "pull-requests=read"],
        )

    def test_the_verify_job_reads_the_review_workflows_outputs(self):
        self.assertEqual(
            value(".jobs.verify.steps[] | select(.id == \"verify\") | .env.DECISION"),
            "${{ needs.review.outputs.decision }}",
        )
        self.assertEqual(
            value(".jobs.verify.steps[] | select(.id == \"verify\") | .env.SENT"), "${{ needs.review.outputs.sent }}"
        )

    def test_the_review_workflow_gives_its_decision_and_whether_it_sent_the_request(self):
        outputs = '.on.workflow_call.outputs'
        self.assertEqual(_yq(f"{outputs}.decision.value").strip(), "${{ jobs.decide.outputs.decision }}")
        self.assertEqual(_yq(f"{outputs}.sent.value").strip(), "${{ jobs.fire.outputs.sent }}")

    def test_the_only_step_file_is_the_verify_step_and_it_is_checked_out(self):
        self.assertEqual(set(file_steps(SELF_TEST)), {("verify", "verify")})
        self.assertEqual([shell for shell in step_shells(SELF_TEST) if "python" in shell], [])
        step_file = step_file_path("verify", "verify", SELF_TEST)
        self.assertTrue(step_file.is_file(), step_file)
        patterns = step_field("verify", "checkout", '.with."sparse-checkout"', SELF_TEST).split()
        self.assertEqual(patterns, ["/steps/"])
        self.assertEqual(step_field("verify", "checkout", ".with.path", SELF_TEST), ".cipherstash-workflows")
        self.assertEqual(step_field("verify", "checkout", '.with."persist-credentials"', SELF_TEST), "false")


if __name__ == "__main__":
    unittest.main()
