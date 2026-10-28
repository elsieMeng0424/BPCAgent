"""Patient-stage adapter for the existing real Shell and Docker Shell tools."""

import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..sandbox import DockerSandbox, SandboxConfig
from .tool import Tool
from .shell import ShellArgs, ShellTool, ShellToolConfig
from .sandbox_shell import SandboxShellTool


class PatientShellConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    mode: Literal["sandbox", "local", "disabled"] = "sandbox"
    allow_unsafe_local: bool = False
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)

    @model_validator(mode="after")
    def require_local_acknowledgement(self):
        if self.mode == "local" and not self.allow_unsafe_local:
            raise ValueError("Local Shell has host permissions; set allow_unsafe_local=true explicitly")
        return self


class PatientShellTool(Tool[ShellArgs]):
    """Export only host-bound snapshot views; sandbox startup is lazy and fail-closed."""

    args_schema = ShellArgs

    def __init__(self, directory, inputs, config=None):
        self.settings = config or PatientShellConfig()
        self.directory = Path(directory).resolve() / "shell"
        self.inputs = inputs
        self.executions = []
        self.sandbox = None
        execution = (
            "Configured mode: sandbox. Docker runs at /workspace with read-only input and no network. "
            if self.settings.mode == "sandbox" else
            "Configured mode: local. Commands run on the host in this turn's shell directory with the current user's permissions. "
        )
        super().__init__("run_shell", (
            "Run a real shell command after bpc_state_extractor. Inspect input/patient_state.json, "
            "input/state_check.json, input/normalized_markers.json and input/patient_evidence.json. "
            "Write diagnostics to scripts/, output/ or tmp/; never claim those files modify the shared snapshot. "
            + execution + "Use relative input/, scripts/, output/ and tmp/ paths and python3. "
            "Check exit_code and timed_out, disclose failures, and do not invent a successful check. "
            "Do not access raw datasets, credentials, future labels or background services."
        ))

    def _prepare(self):
        # Read the authoritative in-memory snapshot, never model-edited artifact files.
        inputs = self.inputs()
        if self.directory.is_symlink():
            raise ValueError("Shell workspace must not be a symlink")
        self.directory.mkdir(exist_ok=True)
        for name in DockerSandbox.directories:
            path = self.directory / name
            if path.is_symlink():
                raise ValueError("Shell directories must not be symlinks")
            path.mkdir(exist_ok=True)
        for name, data in inputs.items():
            if name not in {"patient_state", "state_check", "normalized_markers", "patient_evidence"}:
                raise ValueError("Unrecognized Shell input")
            path = self.directory / "input" / f"{name}.json"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)

    def run(self, parameters):
        if self.settings.mode == "disabled":
            raise ValueError("Patient Shell is disabled by the host")
        record = {"mode": self.settings.mode, "status": "running"}
        self.executions.append(record)
        try:
            self._prepare()
            if self.settings.mode == "sandbox":
                if self.sandbox is None:
                    self.sandbox = DockerSandbox(self.settings.sandbox, self.directory)
                # execute() owns per-command cleanup; never fall back to a host shell.
                result = SandboxShellTool(self.sandbox).run(parameters)
            else:
                result = ShellTool(ShellToolConfig(
                    work_dir=self.directory, timeout_seconds=self.settings.sandbox.command_timeout,
                    max_output_bytes=self.settings.sandbox.max_output_bytes,
                )).run(parameters)
            record.update(status="completed", exit_code=result["exit_code"], timed_out=result["timed_out"])
            return {**result, "mode": self.settings.mode, "authoritative_state_modified": False,
                    "isolation": "docker" if self.settings.mode == "sandbox" else "none"}
        except Exception as exc:
            record.update(status="failed", error_type=type(exc).__name__)
            raise

    def audit(self):
        return {"mode": self.settings.mode, "executions": list(self.executions),
                "workspace": str(self.directory),
                "sandbox": self.sandbox.metadata() if self.sandbox is not None else None,
                "warning": "Host filesystem/network access is unrestricted" if self.settings.mode == "local" else None}
