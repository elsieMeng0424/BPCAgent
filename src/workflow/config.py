"""File-based workflow configuration with separate local credentials."""

import json
import os
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from ..agents import Config
from ..agents.context import ContextPolicy
from ..tools.patient_shell import PatientShellConfig
from ..tools.guidelines import GuidelineToolConfig
from ..tools.jev import JevSettings
from .definition import WorkflowPlan


class SettingsModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class WorkflowSettings(SettingsModel):
    mode: Literal["demo", "live"] = "demo"
    proxy_port: int | None = Field(28374, ge=1, le=65535)
    guideline_debug_tools: bool = False
    data_dir: str | None = None
    workspace_root: str = "../workspace"
    rules_path: str | None = None
    models_file: str = "models.jsonc"
    providers_file: str = "providers.jsonc"
    agents_file: str = "agents.jsonc"
    tools_file: str | None = None
    secrets_file: str = "secrets.local.json"
    top_m: int = Field(5, gt=0)
    top_k: int = Field(5, gt=0)
    max_stage_attempts: int = Field(2, gt=0)
    plan_file: str | None = None


class ToolsSettings(SettingsModel):
    """Host-owned tool execution settings, separate from model and stage settings."""

    patient_shell: PatientShellConfig = Field(default_factory=PatientShellConfig)
    guidelines: GuidelineToolConfig = Field(default_factory=GuidelineToolConfig)
    jev_file: str | None = None


ProviderName = Literal["openai", "claude", "deepseek", "gateway"]


class ProviderSettings(SettingsModel):
    # An unused gateway can stay unconfigured; selecting it requires an address.
    base_url: str | None = Field(None, min_length=1)
    api_key_env: str = Field(min_length=1)
    api_mode: Literal["chat_completions", "responses"] = "chat_completions"


class ProvidersSettings(SettingsModel):
    providers: dict[ProviderName, ProviderSettings]


class LLMProfile(SettingsModel):
    provider: ProviderName
    model: str = Field(min_length=1)
    reasoning_effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None
    temperature: float | None = Field(None, ge=0, allow_inf_nan=False)
    max_tokens: int | None = Field(None, gt=0)
    timeout: float = Field(120, gt=0, allow_inf_nan=False)
    max_tool_iterations: int = Field(10, gt=0)
    context: ContextPolicy = Field(default_factory=ContextPolicy)


class ProfilesSettings(SettingsModel):
    profiles: dict[str, LLMProfile]


class AgentLLMSettings(SettingsModel):
    llm: str = Field(min_length=1)
    # Validate the merged policy, not a partial override against unrelated defaults.
    context: dict[str, object] | None = None


class AgentsSettings(SettingsModel):
    agents: dict[Literal["PatientStateAgent", "TargetSelectionAgent", "DrugSelectionAgent", "ReviewAgent", "MetaAgent"], AgentLLMSettings]


def strip_line_comments(source):
    """Strip JSONC // comments outside strings, preserving error coordinates."""
    result = list(source)
    in_string = False
    escaped = False
    i = 0
    while i < len(source):
        char = source[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif source[i:i + 2] == "//":
            while i < len(source) and source[i] not in "\r\n":
                result[i] = " "
                i += 1
            continue
        i += 1
    return "".join(result)


def read_object(path):
    path = Path(path)
    source = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonc":
        source = strip_line_comments(source)
    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        # Do not include input lines: the file might contain credentials.
        raise ValueError(f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}") from None
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return data


def validate(model, data, label):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        locations = [".".join(str(k) for k in e["loc"]) for e in exc.errors()]
        raise ValueError(f"Invalid {label} configuration fields: {', '.join(locations)}") from None


def relative_path(value, parent):
    path = Path(value).expanduser()
    return str((parent / path).resolve()) if not path.is_absolute() else str(path.resolve())


class WorkflowConfiguration:
    """Resolve paths relative to their owning config file and snapshot settings."""

    def __init__(self, path=None):
        self.path = Path(path).expanduser().resolve() if path is not None else None
        self.settings = validate(WorkflowSettings, read_object(self.path), "workflow") if self.path else WorkflowSettings()
        plan = WorkflowPlan() if self.settings.plan_file is None else validate(
            WorkflowPlan, read_object(relative_path(self.settings.plan_file, self.path.parent)), "workflow plan")
        self._plan_json = plan.model_dump_json()
        tools = ToolsSettings()
        jev = JevSettings()
        if self.settings.tools_file is not None:
            tools_path = Path(relative_path(self.settings.tools_file, self.path.parent))
            tools = validate(ToolsSettings, read_object(tools_path), "tools")
            if tools.jev_file is not None:
                jev_path = Path(relative_path(tools.jev_file, tools_path.parent))
                jev = validate(JevSettings, read_object(jev_path), "Jev")
                if jev.rubrics_file is not None:
                    jev = jev.model_copy(update={"rubrics_file": relative_path(jev.rubrics_file, jev_path.parent)})
            if tools.guidelines.library_config is not None:
                tools = tools.model_copy(update={"guidelines": tools.guidelines.model_copy(update={
                    "library_config": relative_path(tools.guidelines.library_config, tools_path.parent)})})
            docker_path = tools.patient_shell.sandbox.docker_path
            if docker_path is not None:
                sandbox = tools.patient_shell.sandbox.model_copy(update={
                    "docker_path": Path(relative_path(docker_path, tools_path.parent))})
                tools = tools.model_copy(update={"patient_shell": tools.patient_shell.model_copy(
                    update={"sandbox": sandbox})})
        self._tools_json = tools.model_dump_json()
        self._jev_json = jev.model_copy(update={"proxy_port": self.settings.proxy_port}).model_dump_json()

    def workflow_options(self, overrides):
        values = self.settings.model_dump(exclude={"models_file", "secrets_file", "providers_file", "agents_file", "tools_file", "plan_file", "proxy_port"})
        values["plan"] = self.workflow_plan().model_dump()
        values["patient_shell"] = ToolsSettings.model_validate_json(self._tools_json).patient_shell.model_dump(mode="json")
        values["guidelines"] = ToolsSettings.model_validate_json(self._tools_json).guidelines.model_dump(mode="json")
        values["jev"] = JevSettings.model_validate_json(self._jev_json).model_dump(mode="json")
        if self.path:
            for key in ("data_dir", "workspace_root", "rules_path"):
                if values[key] is not None:
                    values[key] = relative_path(values[key], self.path.parent)
        else:
            values["workspace_root"] = "workspace"
        values.update({k: v for k, v in overrides.items() if v is not None})
        if not values["data_dir"]:
            raise ValueError("Set data_dir in workflow.jsonc or pass --data-dir")
        return values

    def workflow_plan(self):
        return WorkflowPlan.model_validate_json(self._plan_json)

    def model_configs(self, environ: Mapping[str, str] | None = None, *, plan=None):
        """Load only models needed by the effective plan, including CLI overrides."""
        if self.path is None:
            raise ValueError("Live mode requires workflow.jsonc and per-agent model profiles")
        env = os.environ if environ is None else environ
        model_data = read_object(relative_path(self.settings.models_file, self.path.parent))
        # Validate before loading credentials; the legacy task/review/meta schema is rejected.
        validate(ProfilesSettings, model_data, "profiles")
        secrets = {}
        secrets_path = Path(relative_path(self.settings.secrets_file, self.path.parent))
        if secrets_path.exists():
            secrets = read_object(secrets_path)
            if any(not isinstance(v, str) for v in secrets.values()):
                raise ValueError("Local secrets must map environment variable names to strings")
        effective_plan = self.workflow_plan() if plan is None else WorkflowPlan.model_validate(plan)
        result = self._agent_configs(model_data, secrets, env, effective_plan)
        jev = JevSettings.model_validate_json(self._jev_json)
        if jev.enabled:
            key = env[jev.api_key_env] if jev.api_key_env in env else secrets.get(jev.api_key_env)
            if not isinstance(key, str) or not key.strip():
                raise ValueError(f"Enabled Jev requires {jev.api_key_env} in the environment or local secrets")
            result["jev_api_key"] = SecretStr(key)
        return result

    def _agent_configs(self, model_data, secrets, env, plan):
        profiles = validate(ProfilesSettings, model_data, "profiles").profiles
        providers = validate(ProvidersSettings, read_object(relative_path(
            self.settings.providers_file, self.path.parent)), "providers").providers
        agents = validate(AgentsSettings, read_object(relative_path(
            self.settings.agents_file, self.path.parent)), "agents").agents
        required = plan.active_roles
        if not set(required) <= set(agents):
            raise ValueError("agents.jsonc must configure all active task agents and ReviewAgent")
        configs = {}

        def resolve_profile(profile_name, label, context_override=None):
            """Resolve task and optional vision profiles through the same credential path."""
            if profile_name not in profiles:
                raise ValueError(f"Unknown LLM profile referenced by {label}")
            profile = profiles[profile_name]
            if profile.provider not in providers:
                raise ValueError(f"Unknown provider referenced by {label}")
            provider = providers[profile.provider]
            if not provider.base_url or not provider.base_url.strip():
                raise ValueError(f"Configure base_url for provider {profile.provider} before selecting it")
            if profile.provider in {"claude", "deepseek"} and provider.api_mode != "chat_completions":
                raise ValueError(f"Provider {profile.provider} requires chat_completions in this adapter")
            if profile.provider == "claude" and profile.reasoning_effort is not None:
                raise ValueError("Claude compatibility API ignores reasoning_effort; set it to null")
            key = provider.api_key_env
            values = profile.model_dump(exclude={"provider"})
            if context_override is not None:
                values["context"].update(context_override)
            values.update(base_url=provider.base_url, api_mode=provider.api_mode,
                          proxy_port=self.settings.proxy_port,
                          api_key=env[key] if key in env else secrets.get(key),
                          default_provider=profile.provider)
            return Config(**Config.resolve(overrides=values, environ={}).model_dump())

        for role in required:
            binding = agents[role]
            configs[role] = resolve_profile(binding.llm, role, binding.context)
        result = {"agent_configs": configs}
        guidelines = ToolsSettings.model_validate_json(self._tools_json).guidelines
        if guidelines.enabled and guidelines.vision_profile is not None:
            vision = resolve_profile(guidelines.vision_profile, "guideline vision")
            if vision.api_mode != "chat_completions":
                raise ValueError("Guideline vision currently requires chat_completions with image support")
            result["guideline_vision_config"] = vision
        return result
