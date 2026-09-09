from __future__ import annotations

import json
import tempfile
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
CONTROLLER = ROOT / "controller"
sys.path.insert(0, str(CONTROLLER))

import runner_transport_probe as probe


def _write_process(
    proc: Path,
    pid: int,
    *,
    argv: tuple[str, ...],
    children: tuple[int, ...] = (),
    environment: bytes = b"",
) -> None:
    root = proc / str(pid)
    task = root / "task" / str(pid)
    task.mkdir(parents=True)
    (root / "cmdline").write_bytes(b"\0".join(item.encode("utf-8") for item in argv) + b"\0")
    (root / "environ").write_bytes(environment)
    (task / "children").write_text(" ".join(str(item) for item in children), encoding="ascii")


def _systemctl_runner(command, timeout, environment):
    prop = next(item for item in command if item.startswith("--property="))
    values = {
        "--property=ActiveState": "active\n",
        "--property=SubState": "running\n",
        "--property=MainPID": "42\n",
    }
    return 0, values[prop]


def test_runner_listener_descendant_environment_wins_over_systemd_wrapper() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proc = Path(raw)
        _write_process(proc, 42, argv=("/bin/bash", "./runsvc.sh"), children=(43,))
        _write_process(proc, 43, argv=("/usr/bin/node", "bin/RunnerService.js"), children=(44,))
        _write_process(
            proc,
            44,
            argv=("/opt/runner/bin/Runner.Listener", "run", "--startuptype", "service"),
            environment=(
                b"HTTPS_PROXY=http://secret.invalid:17890\0"
                b"NO_PROXY=localhost\0"
                b"SECRET_TOKEN=do-not-record\0"
            ),
        )

        runner, presence = probe.collect_runner_runtime(
            runner=_systemctl_runner,
            environment={"GITHUB_ACTIONS": "true", "HTTPS_PROXY": "job-secret"},
            proc_root=proc,
        )

    assert runner["service_state"] == "ACTIVE"
    assert runner["service_environment_observed"] is True
    assert presence["source"] == "SERVICE_PROCESS"
    assert presence["proxy_present"] is True
    assert presence["observer_environment_matches_presence"] is True
    rendered = json.dumps({"runner": runner, "presence": presence}, sort_keys=True)
    assert "secret.invalid" not in rendered
    assert "do-not-record" not in rendered
    assert "job-secret" not in rendered


def test_ambiguous_listener_descendants_fail_closed_without_using_wrapper_environment() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proc = Path(raw)
        _write_process(
            proc,
            42,
            argv=("/bin/bash", "./runsvc.sh"),
            children=(43, 44),
            environment=b"HTTPS_PROXY=http://wrapper.invalid:17890\0",
        )
        listener_argv = ("/opt/runner/bin/Runner.Listener", "run", "--startuptype", "service")
        _write_process(proc, 43, argv=listener_argv)
        _write_process(proc, 44, argv=listener_argv)

        runner, presence = probe.collect_runner_runtime(
            runner=_systemctl_runner,
            environment={"GITHUB_ACTIONS": "true"},
            proc_root=proc,
        )

    assert runner["service_environment_observed"] is False
    assert presence["source"] == "OBSERVER_PROCESS"
    assert presence["service_environment_observed"] is False
    assert presence["proxy_present"] is False
    assert presence["observer_environment_matches_presence"] is None
