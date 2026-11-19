"""The self-test workflow, .github/workflows/self-test.yml: what it calls, with which routine, and when."""

import json
import os
import re
import subprocess
import tempfile
import unittest

from harness import ROOT, SELF_TEST, _yq, file_steps, step_field, step_file_path, step_shells

REPOSITORY = "cipherstash/workflows"
DECISION_FILE = ROOT / "request-decision" / "decision.jq"


def value(query):
    return _yq(query, SELF_TEST).strip()


class Expression:
    """Evaluates a job's `if:` condition as GitHub does, for the little of GitHub's expression language the self-test
    uses: `github.` context paths, string literals, true, false, ==, !=, !, && and || with parentheses, and the
    functions contains, startsWith, fromJSON and format. As in GitHub, a missing property is null, && and || give one
    of their operands, and == compares strings ignoring case, as contains and startsWith do."""

    TOKEN = re.compile(r"\s*(?:(&&|\|\||==|!=|!|\(|\)|,)|'((?:[^']|'')*)'|([A-Za-z_][A-Za-z0-9_.-]*)(?=\s*(\())?)")

    def __init__(self, text, context):
        text = text.strip()
        if text.startswith("${{") and text.endswith("}}"):
            text = text[3:-2]
        self.tokens = []
        position = 0
        while text[position:].strip():
            match = self.TOKEN.match(text, position)
            if match is None:
                raise ValueError(f"cannot read the expression at {text[position:]!r}")
            operator, literal, name, call = match.groups()
            if operator:
                self.tokens.append(("op", operator))
            elif literal is not None:
                self.tokens.append(("value", literal.replace("''", "'")))
            elif call:
                self.tokens.append(("function", name))
            else:
                self.tokens.append(("value", self.lookup(name, context)))
            position = match.end()
        self.position = 0

    @staticmethod
    def lookup(name, context):
        if name in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[name]
        found = context
        for part in name.split("."):
            found = found.get(part) if isinstance(found, dict) else None
        return found

    def peek(self):
        return self.tokens[self.position] if self.position < len(self.tokens) else (None, None)

    def take(self, operator):
        if self.peek() == ("op", operator):
            self.position += 1
            return True
        return False

    def evaluate(self):
        return bool(self.result())

    def result(self):
        result = self.either()
        if self.position != len(self.tokens):
            raise ValueError("the expression has text left over")
        return result

    def render(self):
        """The value as GitHub writes it into an `env:` entry: true and false in lower case, null as empty."""
        result = self.result()
        if isinstance(result, bool):
            return "true" if result else "false"
        return "" if result is None else str(result)

    def either(self):
        left = self.both()
        while self.take("||"):
            right = self.both()
            left = left or right
        return left

    def both(self):
        left = self.comparison()
        while self.take("&&"):
            right = self.comparison()
            left = left and right
        return left

    def comparison(self):
        left = self.unary()
        for operator in ("==", "!="):
            if self.take(operator):
                right = self.unary()
                same = self.same(left, right)
                return same if operator == "==" else not same
        return left

    @staticmethod
    def same(left, right):
        if isinstance(left, str) and isinstance(right, str):
            return left.lower() == right.lower()
        return type(left) is type(right) and left == right

    @staticmethod
    def text(value):
        """A value as GitHub casts it to a string: null is empty, true and false are lower case."""
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    @classmethod
    def call(cls, name, arguments):
        if name == "contains":
            haystack, needle = arguments
            if isinstance(haystack, list):
                return any(cls.same(item, needle) for item in haystack)
            return cls.text(needle).lower() in cls.text(haystack).lower()
        if name == "startsWith":
            whole, start = arguments
            return cls.text(whole).lower().startswith(cls.text(start).lower())
        if name == "fromJSON":
            return json.loads(arguments[0])
        if name == "format":
            template, *values = arguments
            for number, item in enumerate(values):
                template = template.replace(f"{{{number}}}", cls.text(item))
            return template
        raise ValueError(f"the function {name!r} is not known")

    def unary(self):
        if self.take("!"):
            return not self.unary()
        if self.take("("):
            result = self.either()
            if not self.take(")"):
                raise ValueError("a parenthesis is not closed")
            return result
        kind, token = self.peek()
        if kind == "function":
            self.position += 1
            if not self.take("("):
                raise ValueError(f"the function {token!r} has no arguments")
            arguments = [self.either()]
            while self.take(","):
                arguments.append(self.either())
            if not self.take(")"):
                raise ValueError(f"the call to {token!r} is not closed")
            return self.call(token, arguments)
        if kind != "value":
            raise ValueError(f"expected a value, found {token!r}")
        self.position += 1
        return token


def event(action="opened", draft=False, fork=False, author_type="User", reviewer=None, team=None, state="open"):
    """A pull_request event's payload, trimmed to what the jobs' conditions read."""
    payload = {
        "action": action,
        "pull_request": {
            "state": state,
            "draft": draft,
            "head": {"repo": {"full_name": "someone/workflows" if fork else REPOSITORY}},
            "user": {"login": "monalisa", "type": author_type},
        },
    }
    if reviewer is not None:
        payload["requested_reviewer"] = {"login": reviewer, "type": "User"}
    if team is not None:
        payload["requested_team"] = {"slug": team}
    return payload


def comment_event(
    body="@cipherstash-agent review",
    login="monalisa",
    author_type="User",
    association="MEMBER",
    state="open",
    on_pull_request=True,
    action="created",
):
    """An issue_comment event's payload, with the fields the jobs' conditions and the request decision read."""
    issue = {
        "number": 42,
        "state": state,
        "html_url": f"https://github.com/{REPOSITORY}/pull/42",
        "user": {"login": "cipherstash-agent", "type": "User"},
    }
    if on_pull_request:
        issue["pull_request"] = {
            "url": f"https://api.github.com/repos/{REPOSITORY}/pulls/42",
            "html_url": f"https://github.com/{REPOSITORY}/pull/42",
        }
    return {
        "action": action,
        "issue": issue,
        "comment": {
            "id": 2500000001,
            "body": body,
            "user": {"login": login, "type": author_type},
            "author_association": association,
        },
        "repository": {"full_name": REPOSITORY},
        "sender": {"login": login, "type": author_type},
    }


def context_of(payload):
    event_name = "issue_comment" if "comment" in payload else "pull_request"
    return {"github": {"event": payload, "event_name": event_name, "repository": REPOSITORY}}


def decision_for(payload):
    """What the review workflow's request decision makes of the comment, for a team member: the independent judge of
    what is a comment command."""
    request = {
        "event_name": "issue_comment",
        "event": payload,
        "review_agent": "cipherstash-agent",
        "membership": "member",
        "run_url": None,
    }
    result = subprocess.run(
        ["jq", "--compact-output", "--from-file", str(DECISION_FILE)],
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


# Comments the request decision takes as a comment command, by a team member.
COMMANDS = (
    "@cipherstash-agent review",
    "@CipherStash-Agent REVIEW",
    "@cipherstash-agent review please look at the parser",
    "@cipherstash-agent review\nFocus on the error handling.",
    "@cipherstash-agent review\r\nFocus on the error handling.",
    "@cipherstash-agent review\tsecurity only",
)
# Comments that are not a comment command, though some come close.
NOT_COMMANDS = (
    "",
    "Thanks!",
    "@cipherstash-agent",
    "@cipherstash-agent thanks for the review",
    "@cipherstash-agent reviewer",
    "@cipherstash-agent reviews",
    "@cipherstash-agent please review",
    "@cipherstash-agent-2 review",
    "@cipherstash-agentreview",
    " @cipherstash-agent review",
    "> @cipherstash-agent review",
    "    @cipherstash-agent review",
    "```\n@cipherstash-agent review\n```",
    "Please do this.\n@cipherstash-agent review",
    "@cipherstash-agent review\rmore",
    "integration-test: received comment-command request from "
    "https://github.com/cipherstash/workflows/actions/runs/1/attempts/1",
)


def skipped_job_says(payload):
    """What the `skipped` job's step reports for the payload: its env filled in as GitHub would, and its script run."""
    context = context_of(payload)
    names = value(".jobs.skipped.steps[0].env | keys | .[]").split()
    env = {name: Expression(value(f".jobs.skipped.steps[0].env.{name}"), context).render() for name in names}
    script = _yq(".jobs.skipped.steps[0].run", SELF_TEST)
    with tempfile.TemporaryDirectory() as temp:
        summary = os.path.join(temp, "summary")
        result = subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, **env, "GITHUB_STEP_SUMMARY": summary},
            capture_output=True,
            text=True,
            check=True,
        )
        with open(summary, encoding="utf-8") as file:
            return result.stdout, file.read()


def jobs_run(payload):
    """The names of the self-test's jobs, out of skipped and review, that run for the payload (a pull_request event's,
    or an issue_comment event's)."""
    context = context_of(payload)
    return {job for job in ("skipped", "review") if Expression(value(f".jobs.{job}.if"), context).evaluate()}


class SelfTestWorkflowTest(unittest.TestCase):
    def test_it_runs_on_the_ready_requested_reviewer_and_comment_events(self):
        self.assertEqual(sorted(value(".on | keys | .[]").split()), ["issue_comment", "pull_request"])
        self.assertEqual(
            value(".on.pull_request.types[]").split(), ["opened", "reopened", "ready_for_review", "review_requested"]
        )
        # A new comment only: an edited comment is never a comment command.
        self.assertEqual(value(".on.issue_comment.types[]").split(), ["created"])

    def test_it_calls_this_pull_requests_own_review_workflow(self):
        # GitHub's self-repository form: this repository, at the exact commit that is running, not a pinned commit.
        self.assertEqual(value(".jobs.review.uses"), "$/.github/workflows/claude-review.yml")

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

    def test_a_ready_request_from_this_repository_runs_the_review_job(self):
        for action in ("opened", "reopened", "ready_for_review"):
            with self.subTest(action=action):
                self.assertEqual(jobs_run(event(action)), {"review"})

    def test_a_draft_fork_or_bots_ready_request_runs_only_the_skipped_job(self):
        for action in ("opened", "reopened", "ready_for_review"):
            for case in ({"draft": True}, {"fork": True}, {"author_type": "Bot"}):
                with self.subTest(action=action, **case):
                    self.assertEqual(jobs_run(event(action, **case)), {"skipped"})

    def test_requesting_the_review_agent_runs_the_review_job_on_drafts_too(self):
        for reviewer in ("cipherstash-agent", "CipherStash-Agent"):
            for draft in (False, True):
                with self.subTest(reviewer=reviewer, draft=draft):
                    self.assertEqual(jobs_run(event("review_requested", draft=draft, reviewer=reviewer)), {"review"})

    def test_requesting_the_review_agent_on_a_fork_or_bots_pull_request_runs_only_the_skipped_job(self):
        for case in ({"fork": True}, {"author_type": "Bot"}, {"fork": True, "draft": True}):
            with self.subTest(**case):
                self.assertEqual(jobs_run(event("review_requested", reviewer="cipherstash-agent", **case)), {"skipped"})

    def test_requesting_any_other_reviewer_or_a_team_runs_no_job(self):
        for case in (
            {"reviewer": "monalisa"},
            {"reviewer": "cipherstash-agent-2"},
            {"reviewer": "other", "draft": True},
            {"reviewer": "other", "fork": True},
            {"reviewer": "other", "author_type": "Bot"},
            {"team": "developers"},
            {"team": "cipherstash-agent"},
            {},
        ):
            with self.subTest(**case):
                self.assertEqual(jobs_run(event("review_requested", **case)), set())

    def test_a_review_requested_on_a_closed_pull_request_runs_no_job(self):
        for reviewer in ("cipherstash-agent", "CipherStash-Agent"):
            for case in ({}, {"draft": True}, {"fork": True}, {"author_type": "Bot"}):
                with self.subTest(reviewer=reviewer, **case):
                    closed = event("review_requested", reviewer=reviewer, state="closed", **case)
                    self.assertEqual(jobs_run(closed), set())

    def test_a_team_members_comment_command_on_a_pull_request_runs_the_review_job(self):
        for body in COMMANDS:
            for association in ("OWNER", "MEMBER", "COLLABORATOR"):
                with self.subTest(body=body, association=association):
                    self.assertEqual(jobs_run(comment_event(body, association=association)), {"review"})

    def test_every_comment_the_review_job_runs_for_is_a_comment_command_to_the_review_workflow(self):
        # The job's condition is only a filter, so it must never let through a comment the review workflow would skip
        # as not a command: the verify job would then fail. The request decision itself is the judge.
        for body in COMMANDS + NOT_COMMANDS:
            with self.subTest(body=body):
                payload = comment_event(body)
                decision = decision_for(payload)
                if jobs_run(payload):
                    self.assertEqual((decision["decision"], decision.get("kind")), ("fire", "comment-command"))
                if body in COMMANDS:
                    self.assertEqual(decision.get("kind"), "comment-command", "a listed command is not one")

    def test_a_comment_that_is_not_a_comment_command_runs_no_job(self):
        for body in NOT_COMMANDS:
            with self.subTest(body=body):
                self.assertEqual(jobs_run(comment_event(body)), set())

    def test_a_comment_with_no_body_runs_no_job(self):
        payload = comment_event()
        payload["comment"]["body"] = None
        self.assertEqual(jobs_run(payload), set())

    def test_a_comment_command_by_a_bot_or_by_the_review_agent_runs_no_job(self):
        # The stub routine posts as the review agent, so its reply can never start another run, whatever it says.
        for case in (
            {"author_type": "Bot", "login": "dependabot[bot]"},
            {"author_type": "Bot", "login": "github-actions[bot]"},
            {"login": "cipherstash-agent"},
            {"login": "CipherStash-Agent"},
        ):
            with self.subTest(**case):
                self.assertEqual(jobs_run(comment_event(**case)), set())

    def test_a_comment_command_runs_the_review_job_whatever_the_commenters_association(self):
        # GitHub reports a team member whose organisation membership is private as CONTRIBUTOR or NONE, so the
        # association is no filter: the review workflow's team membership check decides, and ends the run red for a
        # non-member.
        for association in ("CONTRIBUTOR", "FIRST_TIME_CONTRIBUTOR", "FIRST_TIMER", "NONE", "MANNEQUIN", None):
            with self.subTest(association=association):
                self.assertEqual(jobs_run(comment_event(association=association)), {"review"})

    def test_a_bot_or_review_agent_command_runs_no_job_whatever_its_association(self):
        for association in ("OWNER", "CONTRIBUTOR", "NONE", None):
            for case in ({"author_type": "Bot", "login": "dependabot[bot]"}, {"login": "cipherstash-agent"}):
                with self.subTest(association=association, **case):
                    self.assertEqual(jobs_run(comment_event(association=association, **case)), set())

    def test_the_review_job_does_not_filter_comments_by_author_association(self):
        self.assertNotIn("author_association", value(".jobs.review.if"))

    def test_a_comment_command_on_a_closed_pull_request_or_a_plain_issue_runs_no_job(self):
        self.assertEqual(jobs_run(comment_event(state="closed")), set())
        self.assertEqual(jobs_run(comment_event(on_pull_request=False)), set())

    def test_an_edited_or_deleted_comment_runs_no_job(self):
        for action in ("edited", "deleted"):
            with self.subTest(action=action):
                self.assertEqual(jobs_run(comment_event(action=action)), set())

    def test_the_comment_filter_names_the_review_agent_the_review_workflow_acts_for(self):
        agent = _yq('.on.workflow_call.inputs."review-agent".default').strip()
        condition = value(".jobs.review.if")
        self.assertIn(f"github.event.comment.user.login != '{agent}'", condition)
        self.assertIn(f"'@{agent} review'", condition)

    def test_the_skipped_job_says_a_draft_is_why(self):
        for action in ("opened", "reopened"):
            with self.subTest(action=action):
                notice, summary = skipped_job_says(event(action, draft=True))
                self.assertIn("::notice title=Self-test skipped::Not tested: the pull request is a draft", notice)
                self.assertIn("requesting a review from cipherstash-agent, runs the test", notice)
                self.assertIn("Self-test: skipped (the pull request is a draft", summary)

    def test_the_skipped_job_does_not_call_a_requested_draft_a_draft(self):
        # A review requested on a draft is tested, so a draft never explains a skip for it: its fork or bot does.
        notice, _ = skipped_job_says(event("review_requested", draft=True, fork=True, reviewer="cipherstash-agent"))
        self.assertIn("comes from a fork", notice)
        notice, _ = skipped_job_says(
            event("review_requested", draft=True, author_type="Bot", reviewer="cipherstash-agent")
        )
        self.assertIn("opened by a bot", notice)

    def test_the_skipped_job_says_a_fork_or_a_bot_is_why(self):
        notice, summary = skipped_job_says(event("opened", fork=True))
        self.assertIn("Not tested: the pull request comes from a fork", notice)
        self.assertIn("Self-test: skipped (the pull request comes from a fork", summary)
        notice, summary = skipped_job_says(event("ready_for_review", author_type="Bot"))
        self.assertIn("Not tested: the pull request was opened by a bot", notice)
        self.assertIn("Self-test: skipped (the pull request was opened by a bot", summary)

    def test_the_skipped_job_names_a_draft_before_a_fork(self):
        notice, _ = skipped_job_says(event("opened", draft=True, fork=True))
        self.assertIn("is a draft", notice)

    def test_the_review_agent_requested_is_the_one_the_review_workflow_acts_for(self):
        # The conditions name the review agent, as a job's `if:` cannot read the review workflow's input default.
        agent = _yq('.on.workflow_call.inputs."review-agent".default').strip()
        self.assertEqual(agent, "cipherstash-agent")
        for job in ("skipped", "review"):
            with self.subTest(job=job):
                self.assertIn(f"github.event.requested_reviewer.login == '{agent}'", value(f".jobs.{job}.if"))

    def test_the_verify_job_waits_for_the_review_job_and_only_reads(self):
        self.assertEqual(value(".jobs.verify.needs"), "review")
        self.assertEqual(value(".jobs.verify.if"), "${{ !cancelled() && needs.review.result != 'skipped' }}")
        self.assertEqual(
            value('.jobs.verify.permissions | to_entries | map(.key + "=" + .value) | .[]').split(),
            ["issues=read"],
        )

    def test_the_verify_job_is_told_which_event_started_the_run(self):
        verify = '.jobs.verify.steps[] | select(.id == "verify") | .env'
        self.assertEqual(value(f"{verify}.EVENT_NAME"), "${{ github.event_name }}")
        self.assertEqual(value(f"{verify}.EVENT_ACTION"), "${{ github.event.action }}")
        # The comment that started a comment command's run, whose 👀 reaction the step checks. Empty for other events.
        self.assertEqual(value(f"{verify}.COMMENT_ID"), "${{ github.event.comment.id }}")

    def test_the_verify_step_accepts_the_reaction_only_from_the_review_workflows_token(self):
        # The review workflow's react job adds the reaction with the job's token, which acts as this account.
        self.assertEqual(
            value('.jobs.verify.steps[] | select(.id == "verify") | .env.REACTION_AUTHOR'), "github-actions[bot]"
        )

    def test_the_verify_job_reads_the_review_workflows_outputs(self):
        self.assertEqual(
            value(".jobs.verify.steps[] | select(.id == \"verify\") | .env.DECISION"),
            "${{ needs.review.outputs.decision }}",
        )
        self.assertEqual(
            value(".jobs.verify.steps[] | select(.id == \"verify\") | .env.SENT"), "${{ needs.review.outputs.sent }}"
        )

    def test_the_verify_step_accepts_a_reply_only_from_the_stub_routines_account(self):
        self.assertEqual(
            value(".jobs.verify.steps[] | select(.id == \"verify\") | .env.REPLY_AUTHOR"), "cipherstash-agent"
        )

    def test_the_verify_job_does_not_filter_the_reply_by_author_association(self):
        # The stub's reply is accepted by its author's login alone.
        self.assertNotIn("author_association", value(".jobs.verify | tojson"))

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
