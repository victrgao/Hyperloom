# SPDX-FileCopyrightText: 2026 Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Running a case: its exit code, the end of its output and its few small files come back; nothing is read into them."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from hyperloom.measure.repro import (
    CASE_SCRIPT,
    EXIT_FILE,
    LOG_FILE,
    OUTPUT_DIR,
    REPRO_DIR,
    STANDALONE_SCRIPT,
    Case,
    ReproError,
    find_case,
    read_exit_code,
    run_case,
)


def _case(tmp_path: Path, body: str, *, attachment: str = "case", name: str = CASE_SCRIPT) -> Path:
    script = tmp_path / "attach" / attachment / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body, encoding="utf-8")
    return script


def _run(script: Path, tmp_path: Path, *, timeout: float = 30, env: dict[str, str] | None = None) -> int:
    given = {"PATH": os.environ["PATH"], **(env or {})}
    return run_case(script, env=given, timeout_sec=timeout, output_dir=tmp_path / "out")


def _log(tmp_path: Path) -> str:
    return (tmp_path / "out" / REPRO_DIR / LOG_FILE).read_text()


@pytest.mark.parametrize("code", [0, 1, 2, 127, 255])
def test_the_exit_code_is_returned_as_it_is_and_written_where_the_bundle_carries_it(tmp_path, code):
    script = _case(tmp_path, f"echo the case ran\nexit {code}\n")

    assert _run(script, tmp_path) == code

    out = tmp_path / "out" / REPRO_DIR
    assert json.loads((out / EXIT_FILE).read_text()) == {"exit_code": code}
    assert (out / LOG_FILE).read_text() == "the case ran\n"
    assert read_exit_code(tmp_path / "out") == code


def test_a_case_killed_by_a_signal_has_the_signal_as_its_exit_code(tmp_path):
    script = _case(tmp_path, "kill -9 $$\n")

    assert _run(script, tmp_path) == -9


def test_a_case_that_outlives_its_timeout_is_killed_with_its_whole_group_and_its_log_says_so(tmp_path):
    marker = tmp_path / "survived"
    script = _case(tmp_path, f"echo started\n(sleep 3; touch {marker}) &\nsleep 30\n")
    started = time.monotonic()

    assert _run(script, tmp_path, timeout=1) == -9

    assert time.monotonic() - started < 10
    time.sleep(3.5)
    assert not marker.exists(), "the whole process group of the case is killed, not only its shell"
    assert _log(tmp_path) == "started\n\n[killed: not finished within 1 s]\n"


def test_the_case_runs_in_its_attachment_with_the_environment_it_is_given_and_no_other(tmp_path, monkeypatch):
    monkeypatch.setenv("NOT_GIVEN", "from the runner's own process")
    script = _case(tmp_path, 'echo "$GIVEN|${NOT_GIVEN:-unset}|$PWD"\n')

    _run(script, tmp_path, env={"GIVEN": "yes"})

    assert _log(tmp_path).strip() == f"yes|unset|{script.parent}"


def test_only_the_tail_of_a_long_log_is_kept(tmp_path):
    script = _case(tmp_path, "head -c 300000 /dev/zero | tr '\\0' a\necho THE-END\nexit 1\n")

    _run(script, tmp_path)

    log = (tmp_path / "out" / REPRO_DIR / LOG_FILE).read_bytes()
    assert len(log) <= 64 * 1024 and log.endswith(b"THE-END\n")


def test_a_case_that_floods_its_output_is_killed(tmp_path, monkeypatch):
    monkeypatch.setattr("hyperloom.measure.repro._MAX_FILE_BYTES", 1024 * 1024)
    script = _case(tmp_path, "yes\nexit 1\n")

    assert _run(script, tmp_path) == -25  # SIGXFSZ


def test_the_files_at_the_top_of_the_output_directory_come_back(tmp_path):
    script = _case(tmp_path, 'echo "shape,us" > "$HL_OUTPUT_DIR/table.csv"\necho 1 > "$HL_OUTPUT_DIR/b.txt"\n')

    _run(script, tmp_path)

    kept = tmp_path / "out" / REPRO_DIR / OUTPUT_DIR
    assert sorted(path.name for path in kept.iterdir()) == ["b.txt", "table.csv"]
    assert (kept / "table.csv").read_text() == "shape,us\n"
    assert _log(tmp_path) == "", "nothing was dropped, so the log says nothing of the directory"


def test_what_does_not_fit_the_output_caps_is_dropped_by_name_and_the_log_names_it(tmp_path, monkeypatch):
    monkeypatch.setattr("hyperloom.measure.repro.OUTPUT_MAX_FILES", 2)
    monkeypatch.setattr("hyperloom.measure.repro.OUTPUT_MAX_BYTES", 100)
    script = _case(
        tmp_path,
        'cd "$HL_OUTPUT_DIR"\necho a > a\nhead -c 200 /dev/zero > big\necho c > c\necho d > d\n'
        "mkdir sub && echo e > sub/e\nln -s a link\n",
    )

    _run(script, tmp_path)

    kept = tmp_path / "out" / REPRO_DIR / OUTPUT_DIR
    assert sorted(path.name for path in kept.iterdir()) == ["a", "c"]
    assert "not kept from HL_OUTPUT_DIR" in _log(tmp_path) and _log(tmp_path).rstrip().endswith(": big, d, link, sub]")


@pytest.mark.parametrize(("name", "needs_server"), [(CASE_SCRIPT, True), (STANDALONE_SCRIPT, False)])
def test_the_case_is_the_one_entry_script_and_its_name_says_whether_it_needs_a_server(tmp_path, name, needs_server):
    script = _case(tmp_path, "exit 0\n", attachment="b", name=name)
    (tmp_path / "attach" / "a").mkdir()
    (tmp_path / "attach" / "data").write_text("a file attachment")

    assert find_case(tmp_path / "attach", ["a", "b", "data"]) == Case(script, needs_server)


def test_no_case_is_no_run(tmp_path):
    (tmp_path / "attach" / "a").mkdir(parents=True)

    with pytest.raises(ReproError, match="no attachment holds repro.sh or standalone.sh"):
        find_case(tmp_path / "attach", ["a"])


def test_two_entry_scripts_are_ambiguous_not_a_pick(tmp_path):
    _case(tmp_path, "exit 0\n", attachment="a")
    _case(tmp_path, "exit 1\n", attachment="a", name=STANDALONE_SCRIPT)

    with pytest.raises(ReproError, match="more than one of"):
        find_case(tmp_path / "attach", ["a"])


def test_a_script_that_cannot_be_started_is_no_run(tmp_path):
    script = _case(tmp_path, "exit 0\n")

    with pytest.raises(ReproError, match="could not be started"):
        run_case(script, env={"PATH": "/nonexistent"}, timeout_sec=5, output_dir=tmp_path / "out")

    assert not (tmp_path / "out" / REPRO_DIR / EXIT_FILE).exists()


@pytest.mark.parametrize(
    ("content", "match"),
    [
        (None, "no readable repro/verdict.json"),
        ("not json", "no readable repro/verdict.json"),
        ("[1]", "holds None"),
        ('{"exit_code": "1"}', "holds '1'"),
        ('{"exit_code": true}', "holds True"),
        ('{"verdict": "reproduced"}', "holds None"),
    ],
)
def test_a_landed_bundle_without_an_exit_code_does_not_read_as_one(tmp_path, content, match):
    if content is not None:
        (tmp_path / REPRO_DIR).mkdir()
        (tmp_path / REPRO_DIR / EXIT_FILE).write_text(content)

    with pytest.raises(ReproError, match=match):
        read_exit_code(tmp_path)


def test_a_bundle_landed_while_the_runner_wrote_the_verdict_beside_the_code_still_reads(tmp_path):
    (tmp_path / REPRO_DIR).mkdir()
    (tmp_path / REPRO_DIR / EXIT_FILE).write_text('{"verdict": "reproduced", "exit_code": 1}')

    assert read_exit_code(tmp_path) == 1
