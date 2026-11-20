"""Which permissions the review workflow's jobs hold, and what its callers grant them.

A reusable workflow's jobs can hold no more than the job that calls them, and GitHub refuses to start the workflow if
any of its jobs asks for more, so the example caller must grant exactly what the react job holds.
"""

import pathlib
import unittest

from harness import ROOT, WORKFLOW, _yq

EXAMPLE = ROOT / "examples" / "claude-review.yml"

# Adding a reaction to a pull request's conversation comment needs pull-requests: write. With issues: write alone,
# GitHub answered HTTP 403 on the first live comment command (run 44665882864).
REACT_PERMISSIONS = ["issues=write", "pull-requests=write"]


def permissions(path: str, workflow: pathlib.Path) -> list[str]:
    entries = _yq(f'{path}.permissions | to_entries | map(.key + "=" + .value) | .[]', workflow).split()
    return sorted(entries)


class PermissionsTest(unittest.TestCase):
    def test_the_react_job_can_react_to_a_pull_request_comment(self) -> None:
        self.assertEqual(permissions(".jobs.react", WORKFLOW), REACT_PERMISSIONS)

    def test_the_other_jobs_hold_no_permissions(self) -> None:
        jobs = _yq(".jobs | keys | .[]").split()
        for job in jobs:
            if job == "react":
                continue
            with self.subTest(job=job):
                self.assertEqual(_yq(f".jobs.{job}.permissions | length").strip(), "0")

    def test_the_example_caller_grants_what_the_react_job_holds(self) -> None:
        self.assertEqual(permissions(".jobs.review", EXAMPLE), REACT_PERMISSIONS)
        self.assertEqual(_yq(".permissions | length", EXAMPLE).strip(), "0")


if __name__ == "__main__":
    unittest.main()
