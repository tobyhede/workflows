"""The self-test's verify step: checks that the review workflow requested a review from the stub routine, then waits for
the stub's reply on the pull request.

Passes only when a comment on the pull request, by the stub routine's account (REPLY_AUTHOR), contains the marker
`integration-test: received` and this run attempt's link (`<server>/<repo>/actions/runs/<id>/attempts/<n>`), which the
review request includes and the stub echoes back. Fails with one error annotation that says what went wrong: no
account to expect, the review workflow decided not to request a review (naming the skip reason), the request was not
of the kind the event makes, the request was not sent, or no reply came within WAIT_SECONDS (saying how many comments
were read and how many carried the marker and this attempt's link but came from another author).

Inputs, all environment variables: DECISION and SENT (the review workflow's outputs), EVENT_ACTION (the action of the
pull_request event that started the run), REPLY_AUTHOR (the login the stub routine posts as), GITHUB_TOKEN (the job's
token, which only reads comments) and the runner's GITHUB_API_URL, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID,
GITHUB_RUN_ATTEMPT, GITHUB_OUTPUT and GITHUB_STEP_SUMMARY.
Python's standard library only (3.10 features); tests/test_verify.py runs this file.
"""

import json
import os
import re
import sys
import time
import urllib.request
from typing import Any

from helpers import Unreachable, error, escaped, github_headers, send, warning

# How long to wait for the stub's reply in all, and between two reads of the comments, in seconds.
WAIT_SECONDS = 300
POLL_SECONDS = 15
# The text the stub's reply starts with, as its prompt asks.
MARKER = "integration-test: received"
# Comments are read 100 to a page; this many pages is the most read each time.
PAGE_SIZE = 100
MAX_PAGES = 30
# The most of a page's body that is read. A larger answer is not read as a page.
MAX_BODY_BYTES = 8 * 1024 * 1024

# A run attempt's link, as the review workflow's decide job writes it and the request decision accepts it.
RUN_URL = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+/attempts/[0-9]+")
# The kind of review request each pull_request event the self-test runs on makes.
KINDS = {
    "opened": "ready",
    "reopened": "ready",
    "ready_for_review": "ready",
    "review_requested": "requested-reviewer",
}
# HTTP statuses that reading the comments again cannot fix: the token or the pull request is wrong.
REFUSED = (401, 403, 404)


class Failed(Exception):
    """The self-test failed; the message says why."""


class Unread(Exception):
    """The comments could not be read this time; the message says why."""


class Tally:
    """What the reads of the comments have seen, for the timeout's message and for noticing each near-match once."""

    def __init__(self) -> None:
        self.read = 0  # comments read in the last pass over the pages
        self.ignored: set[Any] = set()  # near-matches from another author, by comment id, across all passes


def reply_author() -> str:
    """The login the stub routine posts as. Anyone can comment on a public pull request, so a copy of the marker from
    another account is not the stub's reply; an empty login would accept everyone, so it is an error."""
    login = os.environ.get("REPLY_AUTHOR", "").strip()
    if not login:
        raise Failed(
            "REPLY_AUTHOR is empty or missing, so there is no account whose reply to accept: "
            "set it to the login the stub routine posts as in the self-test workflow"
        )
    return login


def run_url() -> str:
    """This run attempt's link. A re-run has the same run id and a new attempt, so only the attempt's link tells a reply
    to this attempt from a reply to an earlier one."""
    url = "{}/{}/actions/runs/{}/attempts/{}".format(
        os.environ.get("GITHUB_SERVER_URL", ""),
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GITHUB_RUN_ID", ""),
        os.environ.get("GITHUB_RUN_ATTEMPT", ""),
    )
    if not RUN_URL.fullmatch(url):
        raise Failed("Cannot work out this run's link from the runner's variables, so no reply can be matched to it")
    return url


def requested_pull_request(decision: str, sent: str, action: str) -> int:
    """The number of the pull request a review was requested for, or Failed if none was, or it was not the kind of
    request the event makes."""
    expected = KINDS.get(action)
    if expected is None:
        raise Failed(f"The self-test does not test a pull_request event with the action {action!r}")
    try:
        value: Any = json.loads(decision)
    except ValueError:
        value = None
    if not isinstance(value, dict) or value.get("decision") not in ("fire", "skip"):
        raise Failed("The review workflow gave no decision to request a review: see its decide job")
    if value["decision"] == "skip":
        reason = value.get("reason")
        raise Failed(f"The review workflow decided not to request a review ({reason}): see its decide job")
    kind = value.get("kind")
    if kind != expected:
        raise Failed(
            f"The review workflow decided to request a review of the wrong kind ({kind}): a {action} event makes a "
            f"{expected} request. See its decide job"
        )
    if sent != "true":
        raise Failed(
            "The review workflow decided to request a review, but the request was not sent to the stub routine: "
            "see the error in its fire job. If its fire job was cancelled, the request may have been replaced by a "
            "newer request"
        )
    pull_request = value.get("pull_request")
    number = pull_request.get("number") if isinstance(pull_request, dict) else None
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise Failed("The review workflow's decision has no pull request number, so its comments cannot be read")
    return number


def comments_page(token: str, number: int, page: int) -> list[Any]:
    path = f"repos/{os.environ['GITHUB_REPOSITORY']}/issues/{number}/comments?per_page={PAGE_SIZE}&page={page}"
    # The URL starts with the runner's GITHUB_API_URL, which no input can change.
    request = urllib.request.Request(  # noqa: S310
        f"{os.environ['GITHUB_API_URL']}/{path}", headers=github_headers(token)
    )
    try:
        status, body = send(request, read_ok_bytes=MAX_BODY_BYTES + 1)
    except Unreachable as failure:
        raise Unread(f"Could not reach GitHub to read the pull request's comments ({failure.kind})") from None
    if status in REFUSED:
        raise Failed(f"GitHub answered HTTP {status} when asked for the pull request's comments: check the job's token")
    if status != 200:
        raise Unread(f"GitHub answered HTTP {status} when asked for the pull request's comments")
    try:
        comments = json.loads(body) if len(body) <= MAX_BODY_BYTES else None
    except ValueError:
        comments = None
    if not isinstance(comments, list):
        raise Unread("GitHub's answer was not a page of comments")
    return comments


def find_reply(
    token: str, number: int, url: str, author: str, deadline: float, tally: Tally
) -> dict[str, Any] | None:
    """The first comment on the pull request by `author` (compared without regard to case) that holds the marker and
    this run attempt's link, not a longer number. A comment that holds both but is by someone else is counted in
    tally and noticed once. Stops looking at the deadline, even part way through the pages."""
    names_this_run = re.compile(re.escape(url) + r"(?![0-9])")
    tally.read = 0
    for page in range(1, MAX_PAGES + 1):
        comments = comments_page(token, number, page)
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            tally.read += 1
            found: dict[str, Any] = comment
            body = found.get("body")
            if not (isinstance(body, str) and MARKER in body and names_this_run.search(body)):
                continue
            user = found.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if isinstance(login, str) and login.casefold() == author.casefold():
                return found
            key = found.get("id", len(tally.ignored))
            if key not in tally.ignored:
                tally.ignored.add(key)
                # The login and the association come from the comment, so they are untrusted: escaped, and quoted.
                who = f"@{login}" if isinstance(login, str) else "an unknown author"
                association = found.get("author_association")
                print(
                    "::notice title=Reply ignored::"
                    + escaped(
                        f"A comment with the marker and this attempt's link is by {who} (association {association}), "
                        f"not by the expected author @{author}, so it is ignored"
                    ),
                    flush=True,
                )
        if len(comments) < PAGE_SIZE or time.monotonic() >= deadline:
            break
    return None


def wait_for_reply(token: str, number: int, url: str, author: str) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT_SECONDS
    tally = Tally()
    problem = ""
    while True:
        try:
            reply = find_reply(token, number, url, author, deadline, tally)
            problem = ""
        except Unread as failure:
            reply = None
            problem = str(failure)
        if reply is not None:
            return reply
        if time.monotonic() + POLL_SECONDS > deadline:
            break
        time.sleep(POLL_SECONDS)
    count = len(tally.ignored)
    message = (
        f"No reply from the stub routine within {WAIT_SECONDS:g} seconds: no comment on pull request #{number} by "
        f"@{author} contains `{MARKER}` and this run attempt's link, {url}. The last read took {tally.read} "
        f"{'comment' if tally.read == 1 else 'comments'} from the pull request, and in all {count} with the marker "
        f"and this attempt's link were ignored for being from another author. Check that the stub routine's prompt "
        f"asks it to post that comment, that it posts as @{author}, and "
        "that it can comment on this repository (see Self-test in the README)"
    )
    if problem:
        message += f". The last read of the comments failed: {problem}"
    raise Failed(message)


def report(reply: dict[str, Any]) -> None:
    user = reply.get("user")
    login = user.get("login") if isinstance(user, dict) else None
    author = f"@{login}" if isinstance(login, str) else "an unknown author"
    body = str(reply.get("body", ""))
    print(f"::notice title=Stub routine replied::{escaped(f'Reply from {author}: {body}')}", flush=True)
    # The summary is Markdown, not read as workflow commands. The fence is longer than any run of tildes in the reply.
    fence = "~" * max([3, *(len(run) + 1 for run in re.findall("~+", body))])
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as file:
                file.write(f"Self-test: passed. The stub routine's reply, from {author}:\n\n{fence}\n{body}\n{fence}\n")
        except OSError as failure:
            # The reply was found, so the test passed; only its record is missing.
            warning(f"Could not write the job summary ({type(failure).__name__}); the test passed")


try:
    author = reply_author()
    url = run_url()
    number = requested_pull_request(
        os.environ.get("DECISION", ""), os.environ.get("SENT", ""), os.environ.get("EVENT_ACTION", "")
    )
    report(wait_for_reply(os.environ.get("GITHUB_TOKEN", ""), number, url, author))
except Failed as failure:
    error(str(failure))
    sys.exit(1)
except Exception as failure:  # noqa: BLE001 Anything unexpected is reported by its kind only.
    error(f"The self-test's check broke ({type(failure).__name__})")
    sys.exit(1)
