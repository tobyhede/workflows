"""Runs one step of the reusable workflow on its own, with a controlled environment, for the unit tests.

Every Python step runs from a file in steps/, which the workflow checks out from this repository. Such a step names its
file in its env, as STEP_FILE, and run_step runs that same file from this repository, beside the shared helper module
it imports, so the tests run exactly what ships, the way the runner would: with python3, and with GITHUB_OUTPUT,
GITHUB_STEP_SUMMARY and RUNNER_TEMP pointing at temporary files. Nothing else is inherited from the environment running
the tests, so no proxy settings or real tokens leak in.

No Python code is read out of the workflow. yq reads only the workflow's structure (jobs, step ids, env, settings) and
the short bash run scripts that start each step file, which run_step_as_written runs with bash.

Needs python3 (3.10 or later) and yq. Either yq works: mikefarah's (as on GitHub's ubuntu-latest runners) or
the Python jq wrapper.

StubServer is a GitHub API stand-in on 127.0.0.1, answering each path with the status and body a test gives it.
"""

import contextlib
import email.message
import functools
import http.server
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "claude-review.yml"
# This repository's own test of the reusable workflow, whose verify job runs a step file too. Each function below that
# reads a workflow takes it as workflow, which is the reusable workflow unless a test names this one.
SELF_TEST = ROOT / ".github" / "workflows" / "self-test.yml"
# Where the step files live in this repository.
STEPS_DIR = ROOT / "steps"
# Where the workflow checks out this repository, in the job's workspace. Each job that checks it out names that
# directory in its env as WORKFLOW_FILES, and a step names a file in it from there.
CHECKOUT_PATH = ".cipherstash-workflows"
WORKFLOW_FILES = f"${{{{ github.workspace }}}}/{CHECKOUT_PATH}"
WORKFLOW_FILES_PREFIX = "${{ env.WORKFLOW_FILES }}/"


@functools.cache
def _yq(query, workflow=WORKFLOW):
    """yq's raw output for query over the workflow, which the tests only read, so each query runs once."""
    result = subprocess.run(["yq", "-r", query, str(workflow)], capture_output=True, text=True, check=True)
    return result.stdout


def _step_query(job, step_id):
    return f'.jobs.{job}.steps[] | select(.id == "{step_id}")'


def step_field(job, step_id, path, workflow=WORKFLOW):
    """yq's raw output for path (such as .with.path or ."continue-on-error") in the step, without its line break."""
    return _yq(f"{_step_query(job, step_id)} | {path}", workflow).strip()


class StepResult:
    """What a step run left behind: its exit code, its standard output and error, its outputs and its summary."""

    def __init__(self, returncode, stdout, stderr, output_file, summary):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.output_file = output_file
        self.summary = summary
        self.outputs = parse_outputs(output_file)

    def errors(self):
        """The messages of the ::error:: annotations the step printed."""
        return [line[len("::error::"):] for line in self.stdout.splitlines() if line.startswith("::error::")]

    def warnings(self):
        """The messages of the ::warning:: annotations the step printed."""
        return [line[len("::warning::"):] for line in self.stdout.splitlines() if line.startswith("::warning::")]

    def everything(self):
        """All the text the step produced, for checking that a secret appears nowhere."""
        return "\n".join([self.stdout, self.stderr, self.output_file, self.summary])


def parse_outputs(text):
    """Reads a GITHUB_OUTPUT file as the runner does. A name written twice keeps its last value."""
    outputs = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line == "":
            continue
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            name, delimiter = line.split("<<", 1)
            value = []
            while i < len(lines) and lines[i] != delimiter:
                value.append(lines[i])
                i += 1
            if i == len(lines):
                raise ValueError(f"output {name!r} has no closing delimiter")
            i += 1
            outputs[name] = "\n".join(value)
        else:
            name, value = line.split("=", 1)
            outputs[name] = value
    return outputs


@functools.cache
def step_script(job, step_id, workflow=WORKFLOW):
    """The step's run script and shell, read out of the workflow. Only bash scripts are run from it."""
    script = _yq(_step_query(job, step_id) + " | .run", workflow)
    if script.strip() in ("", "null"):
        raise LookupError(f"no step with id {step_id!r} and a run script in job {job!r} of {workflow}")
    shell = _yq(_step_query(job, step_id) + ' | .shell // ""', workflow).strip()
    return script, shell


@functools.cache
def file_steps(workflow=WORKFLOW):
    """(job, step id) for every step of the workflow that runs a step file, named by its env's STEP_FILE."""
    steps = []
    # A job that calls a reusable workflow has no steps.
    for job in _yq(".jobs | keys | .[]", workflow).split():
        for step_id in _yq(f'.jobs.{job}.steps[]? | select(.env.STEP_FILE != null) | .id', workflow).split("\n"):
            if step_id:
                steps.append((job, step_id))
    return tuple(steps)


def file_steps_without_id(workflow=WORKFLOW):
    """The names of the steps that run a step file but have no id, which file_steps cannot list and the tests cannot
    find."""
    names: list[str] = []
    for job in _yq(".jobs | keys | .[]", workflow).split():
        query = f'.jobs.{job}.steps[]? | select(.env.STEP_FILE != null and .id == null) | (.name // "(unnamed)")'
        names.extend(line for line in _yq(query, workflow).split("\n") if line)
    return names


def step_shells(workflow=WORKFLOW):
    """The shell every step of the workflow names, or "" for the default."""
    return _yq('.jobs[].steps[]? | (.shell // "")', workflow).splitlines()


def checked_out_file(job, step_id, name, workflow=WORKFLOW):
    """The file, in this repository, that the step's env value name gives as a path in the job's WORKFLOW_FILES."""
    value = _yq(_step_query(job, step_id) + f' | .env.{name} // ""', workflow).strip()
    job_files = _yq(f'.jobs.{job}.env.WORKFLOW_FILES // ""', workflow).strip()
    if job_files != WORKFLOW_FILES or not value.startswith(WORKFLOW_FILES_PREFIX):
        raise LookupError(
            f"step {step_id!r} in job {job!r} has no {name} in {CHECKOUT_PATH}: {value!r}, "
            f"with WORKFLOW_FILES {job_files!r}"
        )
    return ROOT / value[len(WORKFLOW_FILES_PREFIX):]


@functools.cache
def step_file_path(job, step_id, workflow=WORKFLOW):
    """The step file, in this repository, that the step's STEP_FILE names in the workflow's checkout."""
    return checked_out_file(job, step_id, "STEP_FILE", workflow)


@contextlib.contextmanager
def step_file_copy(job, step_id, helpers_source=None):
    """Copies the step's file into a temporary directory of its own and yields the copy's path, for a step whose
    checkout is partial or broken: helpers_source, if given, is written beside it as helpers.py, which is otherwise
    missing."""
    with tempfile.TemporaryDirectory() as temp:
        step_file = step_file_path(job, step_id)
        copy = pathlib.Path(temp) / step_file.name
        shutil.copy(step_file, copy)
        if helpers_source is not None:
            (copy.parent / "helpers.py").write_text(helpers_source, encoding="utf-8")
        yield copy


@functools.cache
def step_env_names(job, step_id, workflow=WORKFLOW):
    """The names in the step's env block."""
    return frozenset(_yq(_step_query(job, step_id) + " | (.env // {}) | keys | .[]", workflow).split())


def run_step(
    job, step_id, env, api_url="http://127.0.0.1:9", substitutions=None, timeout=20, runner=None, workflow=WORKFLOW
):
    """Runs the step with only the given env (plus the runner's own variables) and returns a StepResult.

    The step must name a step file; it runs that file, from a copy of steps/ so that the shared helper module is beside
    it.

    substitutions maps text in the step's code to its replacement, such as a timeout constant lowered for a test. For
    a step file, the code is the file and the modules beside it, such as the shared helper module. Each piece of text
    must appear exactly once in all of that code, so a test fails loudly if the code changes under it.
    """
    undeclared = set(env) - step_env_names(job, step_id, workflow)
    if undeclared:
        raise AssertionError(f"step {step_id!r} does not declare env {sorted(undeclared)}")
    # The step file and every module beside it, which the step may import. step_file_path raises for a step that
    # names no step file.
    step_file = step_file_path(job, step_id, workflow)
    sources = {path.name: path.read_text(encoding="utf-8") for path in step_file.parent.glob("*.py")}
    main = step_file.name
    for name, source in sources.items():
        # Untrusted text must reach a script through env, never through an expression placed in the script.
        if "${{" in source:
            raise AssertionError(f"step {step_id!r} has a workflow expression inside {name}")
    for old, new in (substitutions or {}).items():
        count = sum(source.count(old) for source in sources.values())
        if count != 1:
            raise AssertionError(f"expected {old!r} once in step {step_id!r}, found it {count} times")
        sources = {name: source.replace(old, new) for name, source in sources.items()}

    def command(temp):
        # A directory of its own, so python3 finds the step's modules beside it and nothing else.
        code = temp / "code"
        code.mkdir()
        for name, source in sources.items():
            (code / name).write_text(source, encoding="utf-8")
        return [sys.executable, str(code / main)]

    return _run(command, env, api_url, timeout, runner)


def run_step_as_written(job, step_id, env, api_url="http://127.0.0.1:9", timeout=20, runner=None, workflow=WORKFLOW):
    """Runs the step's run script exactly as the workflow writes it, with bash, and returns a StepResult.

    For a step whose script only starts its step file, this checks what the script does around the file, such as when
    the file was not checked out; the env gives the file's path as STEP_FILE. The file itself is tested with run_step.
    """
    undeclared = set(env) - step_env_names(job, step_id, workflow)
    if undeclared:
        raise AssertionError(f"step {step_id!r} does not declare env {sorted(undeclared)}")
    script, shell = step_script(job, step_id, workflow)
    if shell not in ("", "bash"):
        raise AssertionError(f"step {step_id!r} uses shell {shell!r}, not bash")
    if "${{" in script:
        raise AssertionError(f"step {step_id!r} has a workflow expression inside its script")

    def command(temp):
        path = temp / "step.sh"
        path.write_text(script, encoding="utf-8")
        # The runner's default shell for a run script.
        return ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(path)]

    return _run(command, env, api_url, timeout, runner)


# The runner's own variables that every step sees, as GitHub sets them for a run in cipherstash/example. A test changes
# or adds one with run_step's or run_step_as_written's runner argument.
RUNNER_VARIABLES = {
    "GITHUB_REPOSITORY": "cipherstash/example",
    "GITHUB_SERVER_URL": "https://github.com",
    "GITHUB_RUN_ID": "1234567890",
    "GITHUB_RUN_ATTEMPT": "1",
}
RUN_URL = "https://github.com/cipherstash/example/actions/runs/1234567890/attempts/1"


def _run(command, env, api_url, timeout, runner=None):
    """Runs command(temp directory) with only env plus the runner's own variables, and returns a StepResult. runner
    changes or adds runner variables, such as GITHUB_EVENT_PATH; one set to None is left out."""
    with tempfile.TemporaryDirectory() as temp_name:
        temp = pathlib.Path(temp_name)
        output_path = temp / "output"
        summary_path = temp / "summary"
        output_path.touch()
        summary_path.touch()
        runner_temp = temp / "runner"
        runner_temp.mkdir()
        full_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C.UTF-8",
            "GITHUB_OUTPUT": str(output_path),
            "GITHUB_STEP_SUMMARY": str(summary_path),
            "GITHUB_API_URL": api_url,
            **RUNNER_VARIABLES,
            "RUNNER_TEMP": str(runner_temp),
            **(runner or {}),
            **env,
        }
        full_env = {name: value for name, value in full_env.items() if value is not None}
        # A step that fails is a result to check, not an error here.
        completed = subprocess.run(
            command(temp), env=full_env, capture_output=True, text=True, timeout=timeout, check=False
        )
        return StepResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            output_path.read_text(encoding="utf-8"),
            summary_path.read_text(encoding="utf-8"),
        )


def closed_port_url():
    """A URL on 127.0.0.1 that nothing listens on, for a connection that is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class StubServer:
    """A stand-in for the GitHub API on 127.0.0.1.

    routes maps a path, for GET and POST alike, to one of:

    - (status, body), or (status, body, headers) to add headers to the answer. A 3xx answer without a Location header
      gets one pointing at a port nothing listens on.
    - slow(seconds, route), for one of these answers after that many seconds.
    - HANG, for an answer that never comes.
    - DROP, for a connection closed without an answer.
    - drip(status), for an answer whose status and headers arrive at once and whose body then sends one byte every
      50 ms and never ends, so no single read times out but the answer never completes. DRIP is drip(200).

    A path with no route answers 404. Every request is recorded in requests as (method, path, headers), and its body,
    as bytes (empty for a GET), at the same place in bodies. Headers are looked up by name ignoring case, as HTTP does.
    Use as a context manager.
    """

    class Drip:
        """An answer with this status whose body never ends."""

        def __init__(self, status):
            self.status = status

    class Slow:
        """An answer that arrives only after this many seconds."""

        def __init__(self, seconds, route):
            self.seconds = seconds
            self.route = route

    HANG = object()
    DROP = object()
    DRIP = Drip(200)

    @staticmethod
    def slow(seconds, route):
        return StubServer.Slow(seconds, route)

    @staticmethod
    def drip(status):
        return StubServer.Drip(status)

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests: list[tuple[str, str, email.message.Message]] = []
        self.bodies: list[bytes] = []
        self._release = threading.Event()
        stub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.answer("GET")

            def do_POST(self):
                self.answer("POST")

            def answer(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                # Headers stay a message, so a lookup by name ignores case as HTTP does.
                stub.requests.append((method, self.path, self.headers))
                stub.bodies.append(body)
                route = stub.routes.get(self.path, (404, '{"message": "Not Found"}'))
                if isinstance(route, StubServer.Slow):
                    time.sleep(route.seconds)
                    route = route.route
                if route is StubServer.HANG:
                    stub._release.wait(10)
                    return
                if route is StubServer.DROP:
                    # Closes the connection without an answer.
                    self.close_connection = True
                    return
                if isinstance(route, StubServer.Drip):
                    self.send_response(route.status)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    try:
                        while not stub._release.wait(0.05):
                            self.wfile.write(b" ")
                            self.wfile.flush()
                    except OSError:
                        pass
                    self.close_connection = True
                    return
                status, body, *rest = route
                headers = dict(rest[0]) if rest else {}
                if 300 <= status < 400:
                    headers.setdefault("Location", "http://127.0.0.1:9/elsewhere")
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.005}, daemon=True)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        return False

    def paths(self):
        return [path for _, path, _ in self.requests]
