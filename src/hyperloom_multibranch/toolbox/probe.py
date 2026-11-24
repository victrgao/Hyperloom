# SPDX-FileCopyrightText: 2026 Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

"""A specialist's probe, as the specialist handles it: the case it submits and what the case left.

A probe is a ``repro`` test with purpose ``probe``: the toolbox runs the specialist's script in a measurement sandbox
with the branch's patch applied and measures nothing. :func:`pack_probe` packs the script as the case the test carries
(``hyperloom.measure.repro`` runs it and says what it is given); :func:`read_case_output` reads what a landed case
left, cut to what one tool reply holds.
"""

from __future__ import annotations

from pathlib import Path

from hyperloom.measure.repro import CASE_SCRIPT, LOG_FILE, OUTPUT_DIR, REPRO_DIR, STANDALONE_SCRIPT
from hyperloom_multibranch.rulegate.fix_case import EXECUTABLE, pack_files

#: The attachment id of a probe's case; never the fix case's, which declares a correctness fix.
PROBE_CASE_ID = "probe-case"
#: The characters of case output one reply carries, and the share of them the case's files may take from its log.
#: Claude Code warns at 10,000 tokens of MCP output; 16,000 characters stay near 5,000 beside the result itself.
CASE_OUTPUT_CHARS = 16_000
_FILES_CHARS = CASE_OUTPUT_CHARS // 2
_UTF8 = "utf-8"


def pack_probe(script: str, *, needs_server: bool) -> bytes:
    """The case of a probe that runs ``script``: against a server the test boots, or on the test's cards alone."""
    return pack_files([(CASE_SCRIPT if needs_server else STANDALONE_SCRIPT, script, EXECUTABLE)])


def read_case_output(landed: Path) -> dict[str, object]:
    """What the case of the repro test landed in ``landed`` left: the end of its output and the files it kept.

    ``files`` holds the first files by name whose text fits; ``files_not_shown`` names the rest with their sizes.
    ``log`` is the end of the case's output, in the characters the files left.
    """
    directory = Path(landed) / REPRO_DIR
    files: dict[str, str] = {}
    not_shown: list[str] = []
    used = 0
    kept = directory / OUTPUT_DIR
    for path in sorted(kept.iterdir()) if kept.is_dir() else ():
        text = path.read_text(encoding=_UTF8, errors="replace")
        if used + len(text) > _FILES_CHARS:
            not_shown.append(f"{path.name} ({path.stat().st_size} bytes)")
            continue
        files[path.name] = text
        used += len(text)
    log = directory / LOG_FILE
    text = log.read_text(encoding=_UTF8, errors="replace") if log.is_file() else ""
    return {"log": text[-(CASE_OUTPUT_CHARS - used) :], "files": files, "files_not_shown": not_shown}


__all__ = ["CASE_OUTPUT_CHARS", "PROBE_CASE_ID", "pack_probe", "read_case_output"]
