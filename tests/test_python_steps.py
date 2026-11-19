"""Every Python step in the reusable workflow: the rules that hold for all of them."""

import ast
import sys
import unittest

from harness import STEPS_DIR, file_steps, file_steps_without_id, step_shells


class PythonStepsTest(unittest.TestCase):
    def test_no_step_runs_python_inline(self):
        # Every Python step runs from a file in steps/, so it imports the shared helper module rather than keeping its
        # own copy of it, and the tests run that file. A step with a Python shell would bring the copies back.
        shells = step_shells()
        self.assertNotEqual(shells, [])
        self.assertEqual([shell for shell in shells if "python" in shell], [])

    def test_the_report_the_routine_call_and_the_reaction_run_from_files(self):
        self.assertEqual(
            set(file_steps()),
            {("decide", "final"), ("fire", "send"), ("react", "reaction")},
        )

    def test_each_step_file_step_has_an_id_so_it_is_listed_and_checked(self):
        self.assertEqual(file_steps_without_id(), [])

    def test_each_python_step_is_python_3_10(self):
        # The steps keep to Python 3.10, so a runner image with an older Python than today's still runs them. This
        # checks the grammar only, not which standard library functions a step calls.
        for path in sorted(STEPS_DIR.glob("*.py")):
            with self.subTest(file=path.name):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))

    def test_steps_is_flat(self):
        # The harness and the checkout test list steps/*.py only, so a subdirectory with Python in it would go untested.
        self.assertEqual(sorted(p.as_posix() for p in STEPS_DIR.rglob("*.py") if p.parent != STEPS_DIR), [])

    def test_no_step_module_is_named_like_a_standard_library_module(self):
        # steps/ is first on sys.path when a step runs, so a module named like a standard library one would replace it.
        self.assertEqual(sorted(p.stem for p in STEPS_DIR.glob("*.py") if p.stem in sys.stdlib_module_names), [])


if __name__ == "__main__":
    unittest.main()
