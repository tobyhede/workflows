"""The decide job's last step: always leaves the fire job a finished decision, and writes the run's summary line.

It writes the "decision-failed" skip first, so that if anything below breaks, that skip is what the fire job sees; the
valid decision then replaces it.

Inputs, all environment variables: DECISION (the request decision, as the decide step wrote it) and the runner's
GITHUB_OUTPUT and GITHUB_STEP_SUMMARY.
Python's standard library only (3.10 features); tests/test_report.py runs this file.
"""

import json
import os
from typing import Any

from helpers import error, output

FAILED = '{"decision":"skip","reason":"decision-failed"}'


def finished(value: str) -> dict[str, Any] | None:
    """The decision value parsed, if it is a finished decision (a skip or a fire), or else None."""
    try:
        decision = json.loads(value)
    except ValueError:
        return None
    if isinstance(decision, dict) and decision.get("decision") in ("skip", "fire"):
        return decision
    return None


def text(value: object) -> str:
    # As jq's string interpolation: a string as it is, anything else as JSON.
    return value if isinstance(value, str) else json.dumps(value)


def summary(decision: dict[str, Any]) -> str:
    if decision.get("decision") == "fire":
        pull_request = decision.get("pull_request")
        number = pull_request.get("number") if isinstance(pull_request, dict) else None
        line = (
            f"Claude review: requested ({text(decision.get('kind'))}, by @{text(decision.get('requester'))}, "
            f"pull request #{text(number)})"
        )
    else:
        line = f"Claude review: skipped ({text(decision.get('reason'))})"
    # One line, whatever the decision holds, so it cannot start a workflow command.
    return line.replace("\r", " ").replace("\n", " ")


try:
    output("decision", FAILED)
    value = os.environ.get("DECISION") or ""
    decision = finished(value)
    if decision is None:
        error("The request decision did not finish; no review was requested")
        value, decision = FAILED, json.loads(FAILED)
    # Passed on as the decision step wrote it.
    output("decision", value)
    line = summary(decision)
    print(line, flush=True)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as file:
        file.write(line + "\n")
except Exception as failure:  # noqa: BLE001 The decision-failed skip, written first, stands.
    error(f"Could not report the request decision ({type(failure).__name__})")

