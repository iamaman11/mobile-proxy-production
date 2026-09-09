from __future__ import annotations

import sys
import tempfile
from pathlib import Path

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
) -> None:
    root = proc / str(pid)
    task = root / "task" / str(pid)
    task.mkdir(parents=True)
    (root / "cmdline").write_bytes(b"\0".join(item.encode("utf-8") for item in argv) + b"\0")
    (root / "environ").write_bytes(b"HTTPS_PROXY=http://redacted.invalid\0NO_PROXY=localhost\0")
    (task / "children").write_text(" ".join(str(item) for item in children), encoding="ascii")


def test_runner_listener_argv_contract_accepts_only_supported_run_forms() -> None:
    assert probe._is_runner_listener(("/opt/runner/bin/Runner.Listener", "run")) is True
    assert probe._is_runner_listener(
        ("/opt/runner/bin/Runner.Listener", "run", "--startuptype", "service")
    ) is True

    for argv in (
        None,
        ("/opt/runner/bin/Runner.Listener",),
        ("/opt/runner/bin/Runner.Listener", "configure"),
        ("/opt/runner/bin/Runner.Listener", "run", "--once"),
        ("/opt/runner/bin/Runner.Listener", "run", "--startuptype", "interactive"),
        ("/opt/runner/bin/not-Runner.Listener", "run"),
    ):
        assert probe._is_runner_listener(argv) is False


def test_plain_run_listener_descendant_is_selected() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proc = Path(raw)
        _write_process(proc, 42, argv=("/bin/bash", "./runsvc.sh"), children=(43,))
        _write_process(proc, 43, argv=("/usr/bin/node", "bin/RunnerService.js"), children=(44,))
        _write_process(proc, 44, argv=("/opt/runner/bin/Runner.Listener", "run"))

        assert probe._listener_environment_pid(proc, 42) == 44


def test_mixed_supported_listener_descendants_remain_ambiguous_and_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        proc = Path(raw)
        _write_process(proc, 42, argv=("/bin/bash", "./runsvc.sh"), children=(43, 44))
        _write_process(proc, 43, argv=("/opt/runner/bin/Runner.Listener", "run"))
        _write_process(
            proc,
            44,
            argv=("/opt/runner/bin/Runner.Listener", "run", "--startuptype", "service"),
        )

        assert probe._listener_environment_pid(proc, 42) is None
