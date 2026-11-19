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
REACTION_LOGIN = "github-actions[bot]"
COMMENT_ID = 2500000001
REACTIONS_PATH = (
    f"/repos/cipherstash/example/issues/comments/{COMMENT_ID}/reactions?content=eyes&per_page=100&page={{page}}"
)
REACTIONS_1 = REACTIONS_PATH.format(page=1)
REPLY = f"integration-test: received ready request from {RUN_URL}"
COMMAND_REPLY = f"integration-test: received comment-command request from {RUN_URL}"
# Short waits, so a test that waits for a reply or a reaction that never comes ends quickly.
QUICK = {
    "WAIT_SECONDS = 300": "WAIT_SECONDS = 1",
    "POLL_SECONDS = 15": "POLL_SECONDS = 0.1",
    "REACTION_WAIT_SECONDS = 60": "REACTION_WAIT_SECONDS = 0.5",
    "REACTION_POLL_SECONDS = 5": "REACTION_POLL_SECONDS = 0.1",
}
# The env every pull_request event's run gives the step.
PULL_REQUEST_ENV = {
    "GITHUB_TOKEN": TOKEN,
    "DECISION": "",
    "SENT": "true",
    "EVENT_NAME": "pull_request",
    "EVENT_ACTION": "opened",
    "COMMENT_ID": "",
    "REPLY_AUTHOR": STUB_LOGIN,
    "REACTION_AUTHOR": REACTION_LOGIN,
}


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


def command_decision(comment_id=COMMENT_ID, kind="comment-command"):
    return json.dumps(
        {
            "decision": "fire",
            "kind": kind,
            "requester": "octocat",
            "pull_request": {"number": 42, "url": "https://github.com/cipherstash/example/pull/42"},
            "comment_id": comment_id,
            "instructions": None,
            "react": True,
            "text": f"Review pull request #42 in cipherstash/example\n\nSent by workflow run: {RUN_URL}",
        }
    )


def reaction(login=REACTION_LOGIN, content="eyes", number=1):
    return {"id": number, "content": content, "user": {"login": login, "type": "Bot"}}


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
        env = {**PULL_REQUEST_ENV, "DECISION": fire_decision()}
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
                {**PULL_REQUEST_ENV, "DECISION": fire_decision()},
                api_url=stub.url,
                substitutions={**QUICK, "WAIT_SECONDS = 300": "WAIT_SECONDS = 5"},
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
        for name, action in (
            ("pull_request", ""),
            ("pull_request", "synchronize"),
            ("pull_request", "review_request_removed"),
            ("pull_request", "created"),
            ("issue_comment", "edited"),
            ("issue_comment", "opened"),
            ("", "opened"),
            ("push", "opened"),
        ):
            with self.subTest(name=name, action=action):
                result = self.verify(EVENT_NAME=name, EVENT_ACTION=action)
                self.assert_failed(result, f"does not test a {name or '(missing)'} event with the action {action!r}")
                self.assertEqual(self.stub.requests, [])

    def test_a_comment_command_for_a_pull_request_event_fails_without_reading_comments(self):
        result = self.verify(DECISION=command_decision())
        self.assert_failed(result, "a opened event makes a ready request")
        self.assertEqual(self.stub.requests, [])


class GivenACommentCommand(VerifyTest):
    """A run started by a comment command: the decision must be a comment command for that comment, the comment must
    have the review workflow's 👀 reaction, and the stub must reply naming this run attempt."""

    def verify_command(self, routes=None, **overrides):
        env = {
            "DECISION": command_decision(),
            "EVENT_NAME": "issue_comment",
            "EVENT_ACTION": "created",
            "COMMENT_ID": str(COMMENT_ID),
        }
        return self.verify(routes, **{**env, **overrides})

    def reacted_and_replied(self, *reactions):
        return {
            REACTIONS_1: page(*(reactions or (reaction(),))),
            PAGE_1: page(comment("@cipherstash-agent review", login="octocat"), comment(COMMAND_REPLY, number=2)),
        }

    def test_a_reaction_and_a_reply_pass_and_both_are_shown(self):
        result = self.verify_command(self.reacted_and_replied())
        self.assert_passed(result)
        self.assertIn("Stub routine replied", result.stdout)
        self.assertIn(COMMAND_REPLY, result.stdout)
        notices = [line for line in result.stdout.splitlines() if "Comment command reacted to" in line]
        self.assertEqual(len(notices), 1, result.stdout)
        self.assertIn(f"@{REACTION_LOGIN}", notices[0])
        self.assertIn(str(COMMENT_ID), notices[0])
        self.assertIn("reaction", result.summary)

    def test_the_reaction_is_read_first_with_the_jobs_token_then_the_comments(self):
        self.verify_command(self.reacted_and_replied())
        self.assertEqual(self.stub.paths()[:2], [REACTIONS_1, PAGE_1])
        method, _, headers = self.stub.requests[0]
        self.assertEqual(method, "GET")
        self.assertEqual(headers.get("Authorization"), f"Bearer {TOKEN}")

    def test_the_reactions_author_is_compared_without_regard_to_case(self):
        routes = self.reacted_and_replied(reaction(login="GitHub-Actions[bot]"))
        self.assert_passed(self.verify_command(routes))

    def test_a_reaction_among_others_is_found(self):
        routes = self.reacted_and_replied(
            reaction(login="octocat"), reaction(content="+1", number=2), reaction(number=3)
        )
        self.assert_passed(self.verify_command(routes))

    def test_a_reaction_on_a_later_page_is_found(self):
        others = [reaction(login=f"person{n}", number=n) for n in range(100)]
        routes = {
            REACTIONS_1: page(*others),
            REACTIONS_PATH.format(page=2): page(reaction()),
            PAGE_1: page(comment(COMMAND_REPLY)),
        }
        self.assert_passed(self.verify_command(routes))

    def test_a_reaction_that_arrives_while_waiting_is_found(self):
        routes = {REACTIONS_1: page(), PAGE_1: page(comment(COMMAND_REPLY))}

        def react_later():
            time.sleep(0.2)
            routes[REACTIONS_1] = page(reaction())

        with StubServer(routes) as stub:
            threading.Thread(target=react_later, daemon=True).start()
            # The stub's routes are this dictionary, so the reaction appears once react_later adds it.
            stub.routes = routes
            result = run_step(
                "verify",
                "verify",
                {
                    **PULL_REQUEST_ENV,
                    "DECISION": command_decision(),
                    "EVENT_NAME": "issue_comment",
                    "EVENT_ACTION": "created",
                    "COMMENT_ID": str(COMMENT_ID),
                },
                api_url=stub.url,
                substitutions={**QUICK, "REACTION_WAIT_SECONDS = 60": "REACTION_WAIT_SECONDS = 5"},
                workflow=SELF_TEST,
            )
        self.assert_passed(result)
        self.assertGreater(stub.paths().count(REACTIONS_1), 1)

    def test_no_reaction_in_time_fails_naming_the_comment_without_reading_comments(self):
        started = time.monotonic()
        result = self.verify_command({REACTIONS_1: page(), PAGE_1: page(comment(COMMAND_REPLY))})
        self.assert_failed(result, f"No 👀 reaction from @{REACTION_LOGIN} on comment {COMMENT_ID}")
        self.assertIn("react job", result.errors()[0])
        self.assertNotIn(PAGE_1, self.stub.paths())
        self.assertGreater(len(self.stub.requests), 1, "the step did not keep trying")
        self.assertLess(time.monotonic() - started, 5, "the step waited past its time")

    def test_a_reaction_by_anyone_else_or_of_another_kind_does_not_count(self):
        for found in (
            reaction(login="octocat"),
            reaction(login="github-actions"),
            reaction(login="github-actions[bot]2"),
            reaction(content="+1"),
            reaction(content="EYES"),
            {"id": 1, "content": "eyes", "user": None},
            {"id": 1, "content": "eyes"},
            "eyes",
        ):
            with self.subTest(found=found):
                result = self.verify_command({REACTIONS_1: page(found), PAGE_1: page(comment(COMMAND_REPLY))})
                self.assert_failed(result, "No 👀 reaction")

    def test_the_timeout_says_how_many_eyes_reactions_from_others_were_seen(self):
        result = self.verify_command({REACTIONS_1: page(reaction(login="octocat"), reaction(login="other", number=2))})
        self.assert_failed(result, "No 👀 reaction")
        self.assertIn("2 from other accounts", result.errors()[0])

    def test_a_reaction_without_the_stubs_reply_fails_as_no_reply(self):
        result = self.verify_command({REACTIONS_1: page(reaction()), PAGE_1: page(comment("Thanks"))})
        self.assert_failed(result, "No reply from the stub routine")
        self.assertIn("Comment command reacted to", result.stdout)

    def test_github_errors_reading_reactions_are_retried_and_named(self):
        for status in (500, 429, 502):
            with self.subTest(status=status):
                result = self.verify_command({REACTIONS_1: (status, "{}")})
                self.assert_failed(result, "No 👀 reaction")
                self.assertIn(f"HTTP {status}", result.errors()[0])
                self.assertGreater(len(self.stub.requests), 1)

    def test_a_refusal_reading_reactions_fails_at_once(self):
        for status in (401, 403, 404):
            with self.subTest(status=status):
                result = self.verify_command({REACTIONS_1: (status, "{}")})
                self.assert_failed(result, f"HTTP {status} when asked for the comment's reactions")
                self.assertEqual(len(self.stub.requests), 1, "the step retried a refusal")

    def test_an_answer_that_is_not_a_list_of_reactions_is_retried(self):
        result = self.verify_command({REACTIONS_1: (200, "{}")})
        self.assert_failed(result, "No 👀 reaction")
        self.assertIn("not a page of reactions", result.errors()[0])

    def test_an_unreachable_api_is_named(self):
        result = self.verify_command(api_url=closed_port_url())
        self.assert_failed(result, "No 👀 reaction")
        self.assertIn("Could not reach GitHub", result.errors()[0])

    def test_a_decision_for_another_comment_fails_without_reading_anything(self):
        for comment_id in (COMMENT_ID + 1, None, "2500000001/../1", True, -1):
            with self.subTest(comment_id=comment_id):
                result = self.verify_command(self.reacted_and_replied(), DECISION=command_decision(comment_id))
                self.assert_failed(result, "not the comment that started this run")
                self.assertEqual(self.stub.requests, [])

    def test_a_missing_or_broken_comment_id_fails_without_reading_anything(self):
        for value in ("", None, "abc", "12/../3", " 2500000001"):
            with self.subTest(value=value):
                result = self.verify_command(self.reacted_and_replied(), COMMENT_ID=value)
                self.assert_failed(result, "COMMENT_ID")
                self.assertEqual(self.stub.requests, [])

    def test_a_missing_or_empty_reaction_author_fails_without_reading_anything(self):
        for value in ("", "  ", None):
            with self.subTest(value=value):
                result = self.verify_command(self.reacted_and_replied(), REACTION_AUTHOR=value)
                self.assert_failed(result, "REACTION_AUTHOR")
                self.assertEqual(self.stub.requests, [])

    def test_a_request_of_another_kind_for_a_comment_fails_without_reading_anything(self):
        for kind in ("ready", "requested-reviewer"):
            with self.subTest(kind=kind):
                result = self.verify_command(DECISION=command_decision(kind=kind))
                self.assert_failed(result, "a created event makes a comment-command request")
                self.assertEqual(self.stub.requests, [])

    def test_a_skipped_comment_command_fails_naming_the_reason(self):
        result = self.verify_command(DECISION=json.dumps({"decision": "skip", "reason": "not-a-team-member"}))
        self.assert_failed(result, "decided not to request a review (not-a-team-member)")

    def test_a_request_that_was_not_sent_fails_before_the_reaction_is_read(self):
        result = self.verify_command(self.reacted_and_replied(), SENT="false")
        self.assert_failed(result, "the request was not sent to the stub routine")
        self.assertEqual(self.stub.requests, [])

    def test_a_reaction_author_with_a_workflow_command_in_it_cannot_add_an_annotation(self):
        routes = {REACTIONS_1: page(reaction(login="x\n::error::injected")), PAGE_1: page(comment(COMMAND_REPLY))}
        result = self.verify_command(routes)
        self.assertEqual(len(result.errors()), 1, result.stdout)
        self.assertFalse(any(line.startswith("::error::injected") for line in result.stdout.splitlines()))


class GivenThePullRequestEventHasNoComment(VerifyTest):
    def test_no_reaction_is_read_for_a_ready_or_requested_reviewer_request(self):
        result = self.verify({PAGE_1: page(comment(REPLY))})
        self.assert_passed(result)
        self.assertEqual(self.stub.paths(), [PAGE_1])
        self.assertNotIn("reacted", result.stdout)


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
                        {**PULL_REQUEST_ENV, "DECISION": fire_decision()},
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
