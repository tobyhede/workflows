"""The workflow's paths to the files it checks out from this repository must point at real files in this repository.

The decide job checks out the request decision and the Python step files from this repository, and the fire and react
jobs check out the step files, and each runs them by path. A rename would pass every other test and fail only in
production, where every run would end as decision-failed, with no review requested, or with no reaction.
"""

import functools
import unittest

from harness import (
    CHECKOUT_PATH,
    ROOT,
    STEPS_DIR,
    WORKFLOW_FILES,
    _yq,
    checked_out_file,
    file_steps,
    step_field,
    step_file_path,
)


def checkout(job="decide"):
    """The job's checkout directory and its sparse-checkout patterns."""
    patterns = [line.strip() for line in step_field(job, "checkout", '.with."sparse-checkout"').splitlines()]
    return step_field(job, "checkout", ".with.path"), [pattern for pattern in patterns if pattern]


def checked_out(path, patterns):
    """Whether path, relative to this repository's root, matches a sparse-checkout pattern: a file's own path, or a
    directory's path ending in /. These are the only two kinds of pattern the workflow uses."""
    for pattern in patterns:
        if pattern == f"/{path}" or (pattern.endswith("/") and f"/{path}".startswith(pattern)):
            return True
    return False


class DecisionFileTest(unittest.TestCase):
    def test_the_decision_file_is_in_the_checkout_and_matched_by_a_sparse_pattern(self):
        _, patterns = checkout()
        path = checked_out_file("decide", "decision", "REQUEST_DECISION_FILE")
        self.assertTrue(path.is_file(), path)
        relative = path.relative_to(ROOT).as_posix()
        self.assertTrue(checked_out(relative, patterns), (relative, patterns))


class CheckoutTest(unittest.TestCase):
    def test_each_job_that_checks_out_this_repository_names_the_checkout_in_its_env(self):
        # The step files and the request decision are named from WORKFLOW_FILES, so it must be the checkout's path.
        for job in sorted({job for job, _ in file_steps()}):
            with self.subTest(job=job):
                self.assertEqual(_yq(f".jobs.{job}.env.WORKFLOW_FILES").strip(), WORKFLOW_FILES)

    def test_each_sparse_pattern_names_a_file_or_directory_in_this_repository(self):
        _, patterns = checkout()
        self.assertNotEqual(patterns, [])
        for pattern in patterns:
            with self.subTest(pattern=pattern):
                self.assertTrue(pattern.startswith("/"), pattern)
                path = ROOT / pattern[1:]
                self.assertTrue(path.is_dir() if pattern.endswith("/") else path.is_file(), pattern)


class StepFilesTest(unittest.TestCase):
    def test_each_step_file_is_a_file_in_this_repository_in_the_steps_directory(self):
        self.assertNotEqual(file_steps(), [])
        for job, step_id in file_steps():
            with self.subTest(job=job, step=step_id):
                path = step_file_path(job, step_id)
                self.assertTrue(path.is_file(), path)
                self.assertEqual(path.parent, STEPS_DIR)

    def test_each_step_file_is_checked_out_by_its_own_job(self):
        # A job sees only its own workspace, so each job that runs a step file checks out this repository itself, in
        # a step before the file's step, into the path the harness expects, and its sparse patterns take in the file
        # and every Python file beside it, such as the shared helper module it imports.
        for job, step_id in file_steps():
            with self.subTest(job=job, step=step_id):
                ids = _yq(f".jobs.{job}.steps[].id").split()
                self.assertIn("checkout", ids)
                self.assertLess(ids.index("checkout"), ids.index(step_id))
                checkout_path, patterns = checkout(job)
                self.assertEqual(checkout_path, CHECKOUT_PATH)
                files = sorted(STEPS_DIR.glob("*.py"))
                self.assertNotEqual(files, [])
                for file in files:
                    relative = file.relative_to(ROOT).as_posix()
                    self.assertTrue(checked_out(relative, patterns), (relative, patterns))


class CheckoutStepsTest(unittest.TestCase):
    """Every job that checks out this repository does it the same way, so a hardening fix to one reaches them all."""

    def jobs(self):
        return sorted({job for job, _ in file_steps()})

    def test_the_decide_fire_and_react_jobs_check_out_this_repository(self):
        self.assertEqual(self.jobs(), ["decide", "fire", "react"])

    def test_each_checkout_is_the_same_apart_from_its_name_and_sparse_patterns(self):
        # Compared value by value, because each yq prints a whole step differently (one keeps its comments).
        def step(job):
            keys = [key for key in step_field(job, "checkout", "keys | .[]").split() if key != "name"]
            settings = [key for key in step_field(job, "checkout", ".with | keys | .[]").split()
                        if key != "sparse-checkout"]
            values = {key: step_field(job, "checkout", f'."{key}"') for key in keys if key != "with"}
            values.update({f"with.{key}": step_field(job, "checkout", f'.with."{key}"') for key in settings})
            return sorted(keys), sorted(settings), values

        for job in self.jobs():
            with self.subTest(job=job):
                self.assertEqual(step(job), step("decide"))

    def test_each_checkout_is_of_this_repository_at_the_pinned_commit_without_credentials(self):
        for job in self.jobs():
            with self.subTest(job=job):
                field = functools.partial(step_field, job, "checkout")
                self.assertEqual(field(".if"), "job.workflow_repository != '' && job.workflow_sha != ''")
                self.assertEqual(field('."continue-on-error"'), "true")
                self.assertEqual(field(".with.repository"), "${{ job.workflow_repository }}")
                self.assertEqual(field(".with.ref"), "${{ job.workflow_sha }}")
                self.assertEqual(field('.with."persist-credentials"'), "false")
                self.assertEqual(field('.with."sparse-checkout-cone-mode"'), "false")


class NoReactionWithoutASentRequestTest(unittest.TestCase):
    """The react job runs only once the fire job's send step said sent. If the fire job's checkout failed, its step
    sends nothing and writes no sent output, so the comment gets no reaction."""

    def test_the_fire_jobs_sent_output_is_the_send_steps_alone(self):
        self.assertEqual(_yq(".jobs.fire.outputs.sent").strip(), "${{ steps.send.outputs.sent }}")

    def test_the_react_job_runs_only_when_the_request_was_sent(self):
        self.assertTrue(_yq(".jobs.react.if").strip().startswith("needs.fire.outputs.sent == 'true'"))


if __name__ == "__main__":
    unittest.main()
