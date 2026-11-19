"""The self-test's verify step: it passes only when a review was requested and the stub routine replied naming this
run, and otherwise fails with a message that says what went wrong."""

import json
import threading
import time
import unittest

from harness import RUN_URL, SELF_TEST, StubServer, closed_port_url, run_step

TOKEN = "ghs_unitTestVerifyTokenThatMustNeverBePrinted0001"
COMMENTS_PATH = "/repos/cipherstash/example/issues/42/comments?per_page=100&page={page}"
PAGE_1 = COMMENTS_PATH.format(page=1)
PAGE_2 = COMMENTS_PATH.format(page=2)
REPLY = f"integration-test: received ready request from {RUN_URL}"
# Short waits, so a test that waits for a reply that never comes ends quickly.
QUICK = {"WAIT_SECONDS = 300": "WAIT_SECONDS = 1", "POLL_SECONDS = 15": "POLL_SECONDS = 0.1"}


def fire_decision():
    return json.dumps(
        {
            "decision": "fire",
            "kind": "ready",
            "requester": "cipherstash-agent",
            "pull_request": {"number": 42, "url": "https://github.com/cipherstash/example/pull/42"},
            "react": False,
            "text": f"Review pull request #42 in cipherstash/example\n\nSent by workflow run: {RUN_URL}",
        }
    )


def comment(body, login="cipherstash-agent", number=1):
    return {"id": number, "body": body, "user": {"login": login, "type": "User"}}


def page(*comments):
    return (200, json.dumps(list(comments)))


class VerifyTest(unittest.TestCase):
    def verify(self, routes=None, api_url=None, substitutions=None, **overrides):
        env = {"GITHUB_TOKEN": TOKEN, "DECISION": fire_decision(), "SENT": "true"}
        env.update(overrides)
        with StubServer(routes) as stub:
            result = run_step(
                "verify",
                "verify",
                env,
                api_url=api_url or stub.url,
                substitutions={**QUICK, **(substitutions or {})},
                workflow=SELF_TEST,
            )
        self.stub = stub
        self.assertNotIn(TOKEN, result.everything(), "the job's token was printed or written to an output")
        return result

    def assert_passed(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.errors(), [])

    def assert_failed(self, result, message):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(result.errors()), 1, result.stdout + result.stderr)
        self.assertIn(message, result.errors()[0])


class GivenTheStubReplies(VerifyTest):
    def test_a_reply_naming_this_run_passes_and_is_shown_with_its_author(self):
        result = self.verify({PAGE_1: page(comment("Thanks"), comment(REPLY, login="claude-stub[bot]", number=2))})
        self.assert_passed(result)
        self.assertIn("@claude-stub[bot]", result.stdout)
        self.assertIn(REPLY, result.stdout)
        self.assertIn(REPLY, result.summary)

    def test_the_comments_are_read_with_the_jobs_token(self):
        self.verify({PAGE_1: page(comment(REPLY))})
        method, path, headers = self.stub.requests[0]
        self.assertEqual((method, path), ("GET", PAGE_1))
        self.assertEqual(headers.get("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(headers.get("Accept"), "application/vnd.github+json")

    def test_a_reply_on_a_later_page_of_comments_is_found(self):
        others = [comment(f"comment {n}", number=n) for n in range(100)]
        result = self.verify({PAGE_1: page(*others), PAGE_2: page(comment(REPLY))})
        self.assert_passed(result)
        self.assertEqual(self.stub.paths()[:2], [PAGE_1, PAGE_2])

    def test_a_reply_that_arrives_while_waiting_is_found(self):
        routes = {PAGE_1: page(comment("Thanks"))}

        def reply_later():
            time.sleep(0.3)
            routes[PAGE_1] = page(comment("Thanks"), comment(REPLY))

        with StubServer(routes) as stub:
            threading.Thread(target=reply_later, daemon=True).start()
            # The stub's routes are this dictionary, so the reply appears once reply_later adds it.
            stub.routes = routes
            result = run_step(
                "verify",
                "verify",
                {"GITHUB_TOKEN": TOKEN, "DECISION": fire_decision(), "SENT": "true"},
                api_url=stub.url,
                substitutions={"WAIT_SECONDS = 300": "WAIT_SECONDS = 5", "POLL_SECONDS = 15": "POLL_SECONDS = 0.1"},
                workflow=SELF_TEST,
            )
        self.assert_passed(result)
        self.assertGreater(len(stub.requests), 1)

    def test_a_reply_with_a_workflow_command_in_it_cannot_add_an_annotation(self):
        body = f"{REPLY}\n::error::injected\n::warning::also"
        result = self.verify({PAGE_1: page(comment(body))})
        self.assert_passed(result)
        self.assertEqual(result.warnings(), [])
        self.assertFalse(any(line.startswith("::error") for line in result.stdout.splitlines()), result.stdout)


class GivenNoReplyNamesThisRun(VerifyTest):
    def test_no_reply_in_time_fails_naming_the_run_and_the_marker(self):
        started = time.monotonic()
        result = self.verify({PAGE_1: page(comment("Thanks"))})
        self.assert_failed(result, "No reply from the stub routine")
        self.assertIn(RUN_URL, result.errors()[0])
        self.assertIn("integration-test: received", result.errors()[0])
        self.assertLess(time.monotonic() - started, 10, "the step waited past its time")

    def test_a_reply_naming_another_run_does_not_count(self):
        for other in (
            "https://github.com/cipherstash/example/actions/runs/123456789",
            "https://github.com/cipherstash/example/actions/runs/12345678901",
            "https://github.com/cipherstash/other/actions/runs/1234567890",
        ):
            with self.subTest(other=other):
                result = self.verify({PAGE_1: page(comment(f"integration-test: received ready request from {other}"))})
                self.assert_failed(result, "No reply from the stub routine")

    def test_a_comment_naming_this_run_without_the_marker_does_not_count(self):
        result = self.verify({PAGE_1: page(comment(f"Sent by workflow run: {RUN_URL}"))})
        self.assert_failed(result, "No reply from the stub routine")

    def test_github_errors_are_retried_and_named_if_no_reply_comes(self):
        result = self.verify({PAGE_1: (500, "{}")})
        self.assert_failed(result, "No reply from the stub routine")
        self.assertIn("HTTP 500", result.errors()[0])
        self.assertGreater(len(self.stub.requests), 1, "the step did not keep trying")

    def test_an_unreachable_api_is_retried_and_named_if_no_reply_comes(self):
        result = self.verify(api_url=closed_port_url())
        self.assert_failed(result, "No reply from the stub routine")
        self.assertIn("Could not reach GitHub", result.errors()[0])


class GivenNoReviewWasRequested(VerifyTest):
    def test_a_skip_fails_naming_the_reason_without_reading_comments(self):
        for reason in ("draft", "membership-lookup-failed", "decision-failed"):
            with self.subTest(reason=reason):
                result = self.verify(DECISION=json.dumps({"decision": "skip", "reason": reason}))
                self.assert_failed(result, f"decided not to request a review ({reason})")
                self.assertEqual(self.stub.requests, [])

    def test_a_missing_or_broken_decision_fails_without_reading_comments(self):
        for value in ("", "not json", "[]", json.dumps({"decision": "lookup"})):
            with self.subTest(value=value):
                result = self.verify(DECISION=value)
                self.assert_failed(result, "The review workflow gave no decision to request a review")
                self.assertEqual(self.stub.requests, [])

    def test_a_request_that_was_not_sent_fails_without_reading_comments(self):
        for sent in ("", "false"):
            with self.subTest(sent=sent):
                result = self.verify(SENT=sent)
                self.assert_failed(result, "the request was not sent to the stub routine")
                self.assertEqual(self.stub.requests, [])

    def test_a_decision_with_no_pull_request_number_fails_without_reading_comments(self):
        for number in (None, "42/../1", -1, True, "42"):
            with self.subTest(number=number):
                value = json.loads(fire_decision())
                value["pull_request"]["number"] = number
                result = self.verify(DECISION=json.dumps(value))
                self.assert_failed(result, "no pull request number")
                self.assertEqual(self.stub.requests, [])


class GivenTheRunIsNotKnown(VerifyTest):
    def test_without_the_runs_details_the_step_fails_without_reading_comments(self):
        for name in ("GITHUB_SERVER_URL", "GITHUB_RUN_ID"):
            with self.subTest(missing=name):
                with StubServer() as stub:
                    result = run_step(
                        "verify",
                        "verify",
                        {"GITHUB_TOKEN": TOKEN, "DECISION": fire_decision(), "SENT": "true"},
                        api_url=stub.url,
                        substitutions=QUICK,
                        runner={name: None},
                        workflow=SELF_TEST,
                    )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(len(result.errors()), 1, result.stdout + result.stderr)
                self.assertIn("this run's link", result.errors()[0])
                self.assertEqual(stub.requests, [])


if __name__ == "__main__":
    unittest.main()
