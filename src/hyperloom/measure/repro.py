# SPDX-FileCopyrightText: 2026 Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""Run the case of a ``repro`` test and keep what it left where the signed bundle carries it.

A case is an attachment directory with one entry script at its top, run with ``bash`` and that directory as its working
directory. The script's name says what the case needs: ``repro.sh`` runs against a server the test boots for it,
``standalone.sh`` runs with no server, on the test's cards alone. The runner unpacks every attachment under
``attach/<id>/``. Whoever runs the case gives it its environment; this module adds ``HL_OUTPUT_DIR``, a directory the
case may leave a few small files in.

The exit code is the case's result and is recorded as it is: this module reads no meaning into it, the requester does.
A negative code is the signal that ended the case, ``-9`` for one killed at its time limit. What comes back, below
``<output_dir>/repro`` so the result manifest lists it and the signature covers it:

* ``verdict.json``: the exit code (:func:`read_exit_code` reads it back from a landed bundle);
* ``case.log``: the tail of the case's output, then a line for each thing this module did to it;
* ``out/``: the regular files at the top of ``HL_OUTPUT_DIR``, the first of them by name that fit the caps.

A case that could not be started, or an attachment set that holds no entry script or several, is a
:class:`ReproError`: nothing ran, so there is no exit code.
"""

from __future__ import annotations

import contextlib
import json
import os
import resource
import shutil
import signal
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

__all__ = [
    "CASE_SCRIPT",
    "CASE_TIMEOUT_SEC",
    "EXIT_FILE",
    "LOG_FILE",
    "OUTPUT_DIR",
    "OUTPUT_DIR_ENV",
    "OUTPUT_MAX_BYTES",
    "OUTPUT_MAX_FILES",
    "REPRO_DIR",
    "STANDALONE_SCRIPT",
    "Case",
    "ReproError",
    "find_case",
    "read_exit_code",
    "run_case",
]

#: The directory, below a test's output directory, that holds what the case left.
REPRO_DIR = "repro"
#: Named for its first reader, the rule gate, which reads a verdict from the code; landed bundles carry the name.
EXIT_FILE = "verdict.json"
LOG_FILE = "case.log"
OUTPUT_DIR = "out"
OUTPUT_DIR_ENV = "HL_OUTPUT_DIR"
#: The entry point of a case that runs against the test's server.
CASE_SCRIPT = "repro.sh"
#: The entry point of a case that runs with no server.
STANDALONE_SCRIPT = "standalone.sh"
_NEEDS_SERVER = {CASE_SCRIPT: True, STANDALONE_SCRIPT: False}
#: How long a case may run before it is killed.
CASE_TIMEOUT_SEC = 600.0
_KIB = 1024
_MIB = _KIB * _KIB
_LOG_TAIL_BYTES = 64 * _KIB
#: The largest file the case may write (its output sink included): a case that floods the disk is killed.
_MAX_FILE_BYTES = 256 * _MIB
#: What ``HL_OUTPUT_DIR`` brings back: a result table, not a data set.
OUTPUT_MAX_FILES = 16
OUTPUT_MAX_BYTES = _MIB


class ReproError(Exception):
    """The case did not run; the message is the reason the result carries."""


@dataclass(frozen=True)
class Case:
    script: Path
    needs_server: bool


def find_case(attach_dir: Path, attachment_ids: Sequence[str]) -> Case:
    """The one entry script the attachment directories hold, and whether it runs against a server."""
    found = [
        Case(script, needs_server)
        for attachment_id in attachment_ids
        for name, needs_server in _NEEDS_SERVER.items()
        if (script := Path(attach_dir) / attachment_id / name).is_file()
    ]
    names = " or ".join(_NEEDS_SERVER)
    if not found:
        raise ReproError(f"no attachment holds {names} at its top")
    if len(found) > 1:
        raise ReproError(f"the attachments hold more than one of {names}")
    return found[0]


def _limit_file_size() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (_MAX_FILE_BYTES, _MAX_FILE_BYTES))


def _tail(handle: IO[bytes]) -> bytes:
    handle.seek(0, os.SEEK_END)
    handle.seek(max(0, handle.tell() - _LOG_TAIL_BYTES))
    return handle.read()


def _wait(process: subprocess.Popen[bytes], timeout_sec: float) -> tuple[int, bool]:
    """The exit code, and whether the case outlived ``timeout_sec`` and its process group was killed."""
    try:
        return process.wait(timeout=timeout_sec), False
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)  # the case's own group: the child was started as its leader
        return process.wait(), True


def _cap_output(directory: Path) -> list[str]:
    """Keep the regular files at the top of ``directory`` that fit the caps, taken by name; the names of the rest."""
    dropped, kept, total = [], 0, 0
    for entry in sorted(directory.iterdir()):
        size = entry.lstat().st_size
        if entry.is_file() and not entry.is_symlink() and kept < OUTPUT_MAX_FILES and total + size <= OUTPUT_MAX_BYTES:
            kept, total = kept + 1, total + size
            continue
        dropped.append(entry.name)
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    return dropped


def run_case(script: Path, *, env: Mapping[str, str], timeout_sec: float, output_dir: Path) -> int:
    """Run ``script`` with ``env`` and return its exit code, which is also written below ``output_dir``.

    The script is run with ``bash``: an attachment does not carry file modes.

    Raises:
        ReproError: The script could not be started.
    """
    directory = Path(output_dir) / REPRO_DIR
    kept = directory / OUTPUT_DIR
    kept.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile() as sink:
        try:
            process = subprocess.Popen(
                ["bash", script.name],
                cwd=script.parent,
                env={**env, OUTPUT_DIR_ENV: str(kept)},
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=_limit_file_size,
            )
        except OSError as exc:
            raise ReproError(f"{script.name} could not be started: {type(exc).__name__}") from exc
        code, timed_out = _wait(process, timeout_sec)
        log = _tail(sink)
    dropped = _cap_output(kept)
    notes = [
        *([f"killed: not finished within {timeout_sec:.0f} s"] if timed_out else []),
        *(
            [
                f"not kept from {OUTPUT_DIR_ENV} (at most {OUTPUT_MAX_FILES} regular files at its top, "
                f"{OUTPUT_MAX_BYTES} bytes in all): {', '.join(dropped)}"
            ]
            if dropped
            else []
        ),
    ]
    (directory / LOG_FILE).write_bytes(log + "".join(f"\n[{note}]\n" for note in notes).encode())
    (directory / EXIT_FILE).write_text(json.dumps({"exit_code": code}), encoding="utf-8")
    return code


def read_exit_code(directory: Path) -> int:
    """The exit code a landed bundle holds below ``directory`` (the test's output directory).

    Raises:
        ReproError: The bundle holds no exit code or an unreadable one.
    """
    path = Path(directory) / REPRO_DIR / EXIT_FILE
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReproError(f"the bundle holds no readable {REPRO_DIR}/{EXIT_FILE}") from exc
    code = document.get("exit_code") if isinstance(document, dict) else None
    if not isinstance(code, int) or isinstance(code, bool):
        raise ReproError(f"{REPRO_DIR}/{EXIT_FILE} holds {code!r}, not an exit code")
    return code
