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
STUB_LOGIN = "cipherstash-agent"
REPLY = f"integration-test: received ready request from {RUN_URL}"
# Short waits, so a test that waits for a reply that never comes ends quickly.
QUICK = {"WAIT_SECONDS = 300": "WAIT_SECONDS = 1", "POLL_SECONDS = 15": "POLL_SECONDS = 0.1"}


def fire_decision(kind="ready"):
    return json.dumps(
        {
            "decision": "fire",
            "kind": kind,
            "requester": "cipherstash-agent",
            "pull_request": {"number": 42, "url": "https://github.com/cipherstash/example/pull/42"},
            "react": False,
            "text": f"Review pull request #42 in cipherstash/example\n\nSent by workflow run: {RUN_URL}",
        }
    )


def comment(body, login=STUB_LOGIN, number=1, association="MEMBER"):
    return {
        "id": number,
        "body": body,
        "user": {"login": login, "type": "User"},
        "author_association": association,
    }


def page(*comments):
    return (200, json.dumps(list(comments)))


class VerifyTest(unittest.TestCase):
    def verify(self, routes=None, api_url=None, substitutions=None, runner=None, **overrides):
        env = {
            "GITHUB_TOKEN": TOKEN,
            "DECISION": fire_decision(),
            "SENT": "true",
            "EVENT_ACTION": "opened",
            "REPLY_AUTHOR": STUB_LOGIN,
        }
        env.update(overrides)
        with StubServer(routes) as stub:
            result = run_step(
                "verify",
                "verify",
                env,
                api_url=api_url or stub.url,
                substitutions={**QUICK, **(substitutions or {})},
                runner=runner,
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
        result = self.verify({PAGE_1: page(comment("Thanks"), comment(REPLY, number=2))})
        self.assert_passed(result)
        self.assertIn("@cipherstash-agent", result.stdout)
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
                {
                    "GITHUB_TOKEN": TOKEN,
                    "DECISION": fire_decision(),
                    "SENT": "true",
                    "EVENT_ACTION": "opened",
                    "REPLY_AUTHOR": STUB_LOGIN,
                },
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


class GivenEachKindOfRequest(VerifyTest):
    def test_a_ready_request_for_a_ready_event_passes(self):
        for action in ("opened", "reopened", "ready_for_review"):
            with self.subTest(action=action):
                self.assert_passed(self.verify({PAGE_1: page(comment(REPLY))}, EVENT_ACTION=action))

    def test_a_requested_reviewer_request_for_a_review_requested_event_passes(self):
        reply = f"integration-test: received requested-reviewer request from {RUN_URL}"
        result = self.verify(
            {PAGE_1: page(comment(reply))},
            EVENT_ACTION="review_requested",
            DECISION=fire_decision("requested-reviewer"),
        )
        self.assert_passed(result)

    def test_a_request_of_another_kind_than_the_event_makes_fails_without_reading_comments(self):
        for action, kind, expected in (
            ("review_requested", "ready", "requested-reviewer"),
            ("review_requested", "comment-command", "requested-reviewer"),
            ("ready_for_review", "requested-reviewer", "ready"),
            ("opened", None, "ready"),
        ):
            with self.subTest(action=action, kind=kind):
                result = self.verify(EVENT_ACTION=action, DECISION=fire_decision(kind))
                self.assert_failed(result, f"a {action} event makes a {expected} request")
                self.assertIn(f"({kind})", result.errors()[0])
                self.assertEqual(self.stub.requests, [])

    def test_an_event_the_self_test_does_not_run_on_fails_without_reading_comments(self):
        for action in ("", "synchronize", "review_request_removed"):
            with self.subTest(action=action):
                result = self.verify(EVENT_ACTION=action)
                self.assert_failed(result, "does not test")
                self.assertEqual(self.stub.requests, [])


class GivenTheSummaryCannotBeWritten(VerifyTest):
    def test_a_reply_that_was_found_still_passes_with_a_warning(self):
        unwritable = {"GITHUB_STEP_SUMMARY": "/nonexistent-directory/summary"}
        result = self.verify({PAGE_1: page(comment(REPLY))}, runner=unwritable)
        self.assert_passed(result)
        self.assertEqual(len(result.warnings()), 1, result.stdout + result.stderr)
        self.assertIn("summary", result.warnings()[0])
        self.assertIn("Stub routine replied", result.stdout)


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

    def test_a_reply_naming_another_attempt_of_this_run_does_not_count(self):
        base = "https://github.com/cipherstash/example/actions/runs/1234567890"
        for other in (f"{base}/attempts/2", f"{base}/attempts/10", f"{base}/attempts/11", base, f"{base}/attempts/"):
            with self.subTest(other=other):
                result = self.verify({PAGE_1: page(comment(f"integration-test: received ready request from {other}"))})
                self.assert_failed(result, "No reply from the stub routine")

    def test_a_reply_naming_this_attempt_passes_even_when_text_follows_it(self):
        result = self.verify({PAGE_1: page(comment(f"integration-test: received ready request from {RUN_URL}."))})
        self.assert_passed(result)

    def test_a_reply_counts_whatever_association_the_token_sees_for_the_stub(self):
        # The job's token may see a private organisation member as CONTRIBUTOR or NONE; only the login decides.
        for association in ("OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR", "NONE", "FIRST_TIMER", None, 7):
            with self.subTest(association=association):
                self.assert_passed(self.verify({PAGE_1: page(comment(REPLY, association=association))}))

    def test_a_comment_with_no_association_field_counts_for_the_stub(self):
        reply = comment(REPLY)
        del reply["author_association"]
        self.assert_passed(self.verify({PAGE_1: page(reply)}))

    def test_the_expected_login_is_compared_without_regard_to_case(self):
        stub = page(comment(REPLY, login="CipherStash-Agent"))
        result = self.verify({PAGE_1: stub}, REPLY_AUTHOR="cipherstash-AGENT")
        self.assert_passed(result)

    def test_a_copy_of_the_reply_from_another_login_is_ignored_whatever_its_association(self):
        for association in ("OWNER", "MEMBER", "COLLABORATOR", "NONE"):
            with self.subTest(association=association):
                result = self.verify({PAGE_1: page(comment(REPLY, login="stranger", association=association))})
                self.assert_failed(result, "No reply from the stub routine")

    def test_a_login_that_only_contains_the_expected_one_is_ignored(self):
        for login in ("cipherstash-agent2", "not-cipherstash-agent", "cipherstash-agent[bot]"):
            with self.subTest(login=login):
                result = self.verify({PAGE_1: page(comment(REPLY, login=login))})
                self.assert_failed(result, "No reply from the stub routine")

    def test_a_comment_with_no_login_is_ignored(self):
        for user in (None, {}, {"login": 7}, "cipherstash-agent"):
            with self.subTest(user=user):
                reply = comment(REPLY)
                reply["user"] = user
                self.assert_failed(self.verify({PAGE_1: page(reply)}), "No reply from the stub routine")

    def test_a_copy_of_the_reply_from_someone_else_does_not_hide_the_real_one(self):
        result = self.verify(
            {PAGE_1: page(comment(REPLY, login="stranger", number=1, association="NONE"), comment(REPLY, number=2))}
        )
        self.assert_passed(result)
        self.assertIn("@cipherstash-agent", result.stdout)

    def test_a_near_match_from_another_login_is_noticed_once_naming_its_login_and_association(self):
        result = self.verify({PAGE_1: page(comment(REPLY, login="stranger", association="CONTRIBUTOR"))})
        self.assert_failed(result, "No reply from the stub routine")
        notices = [line for line in result.stdout.splitlines() if line.startswith("::notice") and "stranger" in line]
        self.assertEqual(len(notices), 1, result.stdout)
        self.assertIn("@stranger", notices[0])
        self.assertIn("CONTRIBUTOR", notices[0])
        self.assertIn("cipherstash-agent", notices[0])

    def test_the_author_in_a_notice_cannot_add_an_annotation(self):
        result = self.verify(
            {PAGE_1: page(comment(REPLY, login="x\n::error::injected%0A", association="NONE\n::warning::too"))}
        )
        self.assertEqual(len(result.errors()), 1, result.stdout)
        self.assertEqual(result.warnings(), [])
        self.assertFalse(any(line.startswith("::warning") for line in result.stdout.splitlines()), result.stdout)

    def test_a_comment_without_the_marker_or_this_attempt_is_not_a_near_match(self):
        other = "https://github.com/cipherstash/example/actions/runs/1234567890/attempts/2"
        result = self.verify(
            {
                PAGE_1: page(
                    comment("Thanks", login="stranger"),
                    comment(f"integration-test: received ready request from {other}", login="stranger", number=2),
                )
            }
        )
        self.assertFalse(any(line.startswith("::notice") for line in result.stdout.splitlines()), result.stdout)
        self.assertIn("took 2 comments", result.errors()[0])
        self.assertIn("0 with the marker and this attempt's link were ignored", result.errors()[0])

    def test_the_timeout_says_how_many_comments_were_read_and_how_many_near_matches_were_ignored(self):
        result = self.verify(
            {
                PAGE_1: page(
                    comment("Thanks"),
                    comment(REPLY, login="stranger", number=2),
                    comment(REPLY, login="other", number=3),
                )
            }
        )
        self.assert_failed(result, "No reply from the stub routine")
        self.assertIn("took 3 comments", result.errors()[0])
        self.assertIn("2 with the marker and this attempt's link were ignored", result.errors()[0])
        self.assertIn("cipherstash-agent", result.errors()[0])

    def test_a_missing_or_empty_expected_login_fails_without_reading_comments(self):
        for value in ("", "  ", None):
            with self.subTest(value=value):
                result = self.verify({PAGE_1: page(comment(REPLY))}, REPLY_AUTHOR=value)
                self.assert_failed(result, "REPLY_AUTHOR")
                self.assertNotIn("No reply from the stub routine", result.errors()[0])
                self.assertEqual(self.stub.requests, [])

    def test_the_deadline_is_checked_between_pages_too(self):
        full = StubServer.slow(0.6, page(*[comment(f"comment {n}", number=n) for n in range(100)]))
        routes = {COMMENTS_PATH.format(page=n): full for n in range(1, 31)}
        started = time.monotonic()
        result = self.verify(routes)
        self.assert_failed(result, "No reply from the stub routine")
        self.assertLess(time.monotonic() - started, 6, "one read of many pages outlasted the limit")
        self.assertLess(len(self.stub.requests), 10)

    def test_a_comment_naming_this_run_without_the_marker_does_not_count(self):
        result = self.verify({PAGE_1: page(comment(f"Sent by workflow run: {RUN_URL}"))})
        self.assert_failed(result, "No reply from the stub routine")

    def test_github_errors_are_retried_and_named_if_no_reply_comes(self):
        result = self.verify({PAGE_1: (500, "{}")})
        self.assert_failed(result, "No reply from the stub routine")
        self.assertIn("HTTP 500", result.errors()[0])
        self.assertGreater(len(self.stub.requests), 1, "the step did not keep trying")

    def test_a_refusal_that_retrying_cannot_fix_fails_at_once_naming_the_status(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                started = time.monotonic()
                result = self.verify({PAGE_1: (status, "{}")})
                self.assert_failed(result, f"HTTP {status}")
                self.assertNotIn("No reply from the stub routine", result.errors()[0])
                self.assertEqual(len(self.stub.requests), 1, "the step retried a refusal")
                self.assertLess(time.monotonic() - started, 0.9)

    def test_rate_limits_are_retried(self):
        for status in (429, 502):
            with self.subTest(status=status):
                result = self.verify({PAGE_1: (status, "{}")})
                self.assert_failed(result, "No reply from the stub routine")
                self.assertIn(f"HTTP {status}", result.errors()[0])
                self.assertGreater(len(self.stub.requests), 1)

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
                self.assertIn("newer request", result.errors()[0])
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
        for name in ("GITHUB_SERVER_URL", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
            with self.subTest(missing=name):
                with StubServer() as stub:
                    result = run_step(
                        "verify",
                        "verify",
                        {
                            "GITHUB_TOKEN": TOKEN,
                            "DECISION": fire_decision(),
                            "SENT": "true",
                            "EVENT_ACTION": "opened",
                            "REPLY_AUTHOR": STUB_LOGIN,
                        },
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
