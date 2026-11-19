"""The self-test's verify step: checks that the review workflow requested a review from the stub routine, then waits for
the stub's reply on the pull request.

Passes only when a comment on the pull request contains the marker `integration-test: received` and this run's link,
which the review request ends with and the stub echoes back. Fails with one error annotation that says what went wrong:
the review workflow decided not to request a review (naming the skip reason), the request was not sent, or no reply
came within WAIT_SECONDS.

Inputs, all environment variables: DECISION and SENT (the review workflow's outputs), GITHUB_TOKEN (the job's token,
which only reads comments) and the runner's GITHUB_API_URL, GITHUB_REPOSITORY, GITHUB_SERVER_URL, GITHUB_RUN_ID,
GITHUB_OUTPUT and GITHUB_STEP_SUMMARY.
Python's standard library only (3.10 features); tests/test_verify.py runs this file.
"""

import json
import os
import re
import sys
import time
import urllib.request
from typing import Any

from helpers import Unreachable, error, escaped, github_headers, send

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

# A run's link, as the review workflow's decide job writes it and the request decision accepts it.
RUN_URL = re.compile(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/actions/runs/[0-9]+")


class Failed(Exception):
    """The self-test failed; the message says why."""


class Unread(Exception):
    """The comments could not be read this time; the message says why."""


def run_url() -> str:
    url = "{}/{}/actions/runs/{}".format(
        os.environ.get("GITHUB_SERVER_URL", ""),
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GITHUB_RUN_ID", ""),
    )
    if not RUN_URL.fullmatch(url):
        raise Failed("Cannot work out this run's link from the runner's variables, so no reply can be matched to it")
    return url


def requested_pull_request(decision: str, sent: str) -> int:
    """The number of the pull request a review was requested for, or Failed if none was."""
    try:
        value: Any = json.loads(decision)
    except ValueError:
        value = None
    if not isinstance(value, dict) or value.get("decision") not in ("fire", "skip"):
        raise Failed("The review workflow gave no decision to request a review: see its decide job")
    if value["decision"] == "skip":
        reason = value.get("reason")
        raise Failed(f"The review workflow decided not to request a review ({reason}): see its decide job")
    if sent != "true":
        raise Failed(
            "The review workflow decided to request a review, but the request was not sent to the stub routine: "
            "see the error in its fire job"
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
    if status != 200:
        raise Unread(f"GitHub answered HTTP {status} when asked for the pull request's comments")
    try:
        comments = json.loads(body) if len(body) <= MAX_BODY_BYTES else None
    except ValueError:
        comments = None
    if not isinstance(comments, list):
        raise Unread("GitHub's answer was not a page of comments")
    return comments


def find_reply(token: str, number: int, url: str) -> dict[str, Any] | None:
    """The first comment on the pull request that holds the marker and this run's link, not a longer run id."""
    names_this_run = re.compile(re.escape(url) + r"(?![0-9])")
    for page in range(1, MAX_PAGES + 1):
        comments = comments_page(token, number, page)
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            found: dict[str, Any] = comment
            body = found.get("body")
            if isinstance(body, str) and MARKER in body and names_this_run.search(body):
                return found
        if len(comments) < PAGE_SIZE:
            break
    return None


def wait_for_reply(token: str, number: int, url: str) -> dict[str, Any]:
    deadline = time.monotonic() + WAIT_SECONDS
    problem = ""
    while True:
        try:
            reply = find_reply(token, number, url)
            problem = ""
        except Unread as failure:
            reply = None
            problem = str(failure)
        if reply is not None:
            return reply
        if time.monotonic() + POLL_SECONDS > deadline:
            break
        time.sleep(POLL_SECONDS)
    message = (
        f"No reply from the stub routine within {WAIT_SECONDS:g} seconds: no comment on pull request #{number} "
        f"contains `{MARKER}` and this run's link, {url}. Check that the stub routine's prompt asks it to post that "
        "comment, and that it can comment on this repository (see Self-test in the README)"
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
        with open(summary, "a", encoding="utf-8") as file:
            file.write(f"Self-test: passed. The stub routine's reply, from {author}:\n\n{fence}\n{body}\n{fence}\n")


try:
    url = run_url()
    number = requested_pull_request(os.environ.get("DECISION", ""), os.environ.get("SENT", ""))
    report(wait_for_reply(os.environ.get("GITHUB_TOKEN", ""), number, url))
except Failed as failure:
    error(str(failure))
    sys.exit(1)
except Exception as failure:  # noqa: BLE001 Anything unexpected is reported by its kind only.
    error(f"The self-test's check broke ({type(failure).__name__})")
    sys.exit(1)
