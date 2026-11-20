"""The react job's step: adds a 👀 reaction to a comment command's comment once its review request has been sent.

Only HTTP 201 (added) or 200 (already there) counts as reacted. Anything else is a warning annotation and a failed step,
which the step's continue-on-error keeps from turning the check red. A missing reaction is cosmetic: this step never
reports an error.

Inputs, all environment variables: GITHUB_TOKEN (the job's token, which needs issues: write and pull-requests: write),
DECISION (the request decision) and the runner's GITHUB_API_URL and GITHUB_REPOSITORY.
Python's standard library only (3.10 features); tests/test_react.py runs this file.
"""

import json
import os
import re
import sys
import urllib.request

try:
    from helpers import Unreachable, github_headers, send, warning
except Exception as failure:  # noqa: BLE001 A broken helper module is still only a warning, like any other failure.
    print(
        f"::warning::Could not add the 👀 reaction: the shared helper module could not be loaded "
        f"({type(failure).__name__}); the review was still requested",
        flush=True,
    )
    sys.exit(1)

# The id goes into the URL, so anything but ASCII digits is refused.
COMMENT_ID = re.compile(r"[0-9]+")


class NotReacted(Exception):
    """The reaction was not added; the message says why."""


def comment_id(decision: str) -> str | None:
    """The decision's comment id as a string of digits, or None. A JSON number of any size keeps every digit."""
    try:
        value = json.loads(decision).get("comment_id")
    except (ValueError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    value = str(value)
    return value if COMMENT_ID.fullmatch(value) else None


def post(url: str, token: str) -> int:
    """POSTs the 👀 reaction to url once and returns the answer's status. There is no retry. The token is only ever a
    request header."""
    # The URL is the runner's GITHUB_API_URL with digits-only parts added, so no event field can choose its scheme.
    request = urllib.request.Request(  # noqa: S310
        url,
        data=json.dumps({"content": "eyes"}, separators=(",", ":")).encode("utf-8"),
        method="POST",
        headers={**github_headers(token), "Content-Type": "application/json"},
    )
    try:
        # The status is the whole answer: the time limit ends as soon as it is known, and the body is not read, so
        # nothing after this can turn a 201 into a warning.
        status, _ = send(request)
    except Unreachable as failure:
        raise NotReacted(
            f"Could not reach GitHub to add the 👀 reaction ({failure.kind}); the review was still requested"
        ) from None
    return status


def react(token: str, decision: str) -> None:
    comment = comment_id(decision)
    if comment is None:
        raise NotReacted("Could not react to the comment: its id is not a number")
    if not token:
        raise NotReacted("Could not react to the comment: GITHUB_TOKEN is empty; the review was still requested")
    api_url, repository = os.environ["GITHUB_API_URL"], os.environ["GITHUB_REPOSITORY"]
    status = post(f"{api_url}/repos/{repository}/issues/comments/{comment}/reactions", token)
    # 201 adds the reaction; 200 means it was already there.
    if status not in (200, 201):
        raise NotReacted(
            f"Could not add the 👀 reaction to comment {comment} (HTTP {status}); the review was still requested. "
            "Check that the calling job grants issues: write and pull-requests: write"
        )
    print(f"Added the 👀 reaction to comment {comment} (HTTP {status})", flush=True)


try:
    react(os.environ.get("GITHUB_TOKEN", ""), os.environ.get("DECISION", ""))
except NotReacted as failure:
    warning(str(failure))
    sys.exit(1)
except Exception as failure:  # noqa: BLE001 Anything unexpected is reported by its kind only.
    warning(f"Could not add the 👀 reaction: the step broke ({type(failure).__name__}); the review was still requested")
    sys.exit(1)
