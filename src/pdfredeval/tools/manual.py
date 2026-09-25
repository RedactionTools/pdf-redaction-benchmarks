"""The manual transport: an adapter whose transport is a person.

This is the class that keeps the architecture honest. Because manual submission
implements the same `submit -> poll -> fetch` contract as an HTTP call, nothing
downstream - aligner, probers, scorer, publisher - knows or cares which was used.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from ..capabilities import Capabilities
from ..errors import NotReadyError
from ..types import ORIENTATIONS, POLARITIES, PROVENANCES, Case, Channel, Status, Transport
from .base import MANIFEST_NAME, Handle, RunResult, Tool, parse_tool_id, sha256

TASK_NAME = "TASK.md"

_TASK_TEMPLATE = """# Manual run {run_id}

Tool: **{tool_id}**{url_line}
Case: `{case_id}`
Attempt: {attempt}

## Settings to apply

```json
{settings}
```

## Steps

1. Submit `{input_path}` to the tool.
2. Apply exactly the settings above. If an option is unavailable, stop and note it below
   rather than substituting - settings are part of this result's identity.
3. Download the result into this folder, **exactly as downloaded**. Any file name works
   (`{output_name}` if you are choosing one); keep only that one PDF here.
   Do not open and re-save it: any viewer can silently strip the earlier revisions,
   metadata and annotations the leak checks look for.
4. Record anything the UI did that the file cannot show - warnings, forced OCR, a
   silently dropped page - under Notes.
5. Screenshots of the settings screen go in `screenshots/` (optional, encouraged).

## Notes

{instructions}
"""


class ManualTool(Tool):
    """Base for UI-only tools. Subclass it, set `tool_id` and `capabilities`.

    `submit()` writes a task for the operator; `poll()` reports READY once the output
    PDF exists; `fetch()` reads it. The degenerate async case.
    """

    transport: ClassVar[Transport] = Transport.MANUAL
    #: Tool-specific guidance rendered into the task file.
    instructions: ClassVar[str] = "_(none)_"

    def _submit(self, case: Case, settings: Mapping[str, Any], handle: Handle) -> None:
        (handle.run_dir / "screenshots").mkdir(exist_ok=True)
        task = _TASK_TEMPLATE.format(
            run_id=handle.run_id,
            tool_id=self.tool_id,
            url_line=f"\nURL: {self.url}" if self.url else "",
            case_id=case.case_id,
            attempt=handle.attempt,
            settings=json.dumps(dict(settings), indent=2, sort_keys=True),
            input_path=Path(case.pdf_path).resolve(),
            output_name=handle.output_path.name,
            instructions=self.instructions,
        )
        (handle.run_dir / TASK_NAME).write_text(task)
        handle.extra["task_path"] = str(handle.run_dir / TASK_NAME)

    def poll(self, handle: Handle) -> Status:
        if handle.output_path.exists() and handle.output_path.stat().st_size > 0:
            return Status.READY
        if (handle.run_dir / "REJECTED").exists():
            return Status.REJECTED
        if (handle.run_dir / "FAILED").exists():
            return Status.FAILED
        return Status.PENDING

    def fetch(self, handle: Handle) -> bytes:
        return handle.output_path.read_bytes()

    def collect(self, handle: Handle) -> RunResult:
        """Collect an operator-supplied output.

        The output is already at its final path, so `fetch` + rewrite would be a no-op
        that risks touching the bytes. Read, hash, and write only the manifest.
        """
        status = self.poll(handle)
        if status is not Status.READY:
            raise NotReadyError(
                f"no {handle.output_path.name} in {handle.run_dir}; the operator has not "
                f"delivered this run yet"
            )
        digest = sha256(handle.output_path.read_bytes())
        manifest = self.manifest(handle, output_sha256=digest)
        manifest.save(handle.run_dir / MANIFEST_NAME)
        return RunResult(
            handle=handle,
            manifest=manifest,
            output_path=handle.output_path,
            output_sha256=digest,
        )


def placeholder(tool_id: str) -> type[ManualTool]:
    """A manual adapter for a tool nobody has written one for yet.

    It lays out the run - directory, task, handle - so an operator can start collecting
    outputs before any adapter code exists. With nothing known about the tool, it claims
    every channel and every rendering condition: `Capabilities()` defaults are narrow on
    purpose, and would excuse most of a case as `unsupported` on a claim nobody made.
    """
    parse_tool_id(tool_id)  # an invalid id is still an error, not a folder

    class Placeholder(ManualTool):
        instructions = (
            "No adapter is registered for this tool, so there are no tool-specific "
            "instructions and no declared capabilities."
        )

        @property
        def capabilities(self) -> Capabilities:
            return Capabilities(
                channels=frozenset(c.value for c in Channel),
                max_pages=None,
                orientations=frozenset(ORIENTATIONS),
                polarities=frozenset(POLARITIES),
                provenances=frozenset(PROVENANCES),
            )

    Placeholder.tool_id = tool_id
    Placeholder.__qualname__ = Placeholder.__name__ = f"Placeholder[{tool_id}]"
    return Placeholder
