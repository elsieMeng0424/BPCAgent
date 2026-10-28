"""Plan-driven task/review loops. One complete Agent invocation is one turn."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys

from ..agents import Config
from ..log_paths import create_run_directory
from ..tools.patient_shell import PatientShellConfig
from ..tools.guidelines import GuidelineToolConfig
from ..tools.guideline_library import GuidelineLibrary
from ..tools.jev import JevEvaluator, JevSettings
from .agents import AGENTS
from .demo import DemoLLM
from .definition import WorkflowPlan
from .state import SharedPatientState, digest, save_json
from .request import resolve_request
from .reporting import ConsoleTee, print_turn_report, validate_print_limit


class AgentWorkflow:
    """Run each configured task Agent, then review its output before advancing.

    ``llm_factory(role)`` is an optional offline dependency-injection hook. It
    returns a model double exposing ``invoke(messages, **kwargs)`` for that
    role, and is called once per Agent turn, including reviews and retries.
    Demo mode defaults to a fresh DemoLLM per turn. Live mode forbids this hook
    and lets each Agent construct a real LLM from its role-specific Config.
    """

    def __init__(self, *, data_dir, workspace_root, rules_path=None, mode="demo",
                 agent_configs=None, plan=None,
                 top_m=5, top_k=5, max_stage_attempts=2, llm_factory=None, patient_shell=None,
                 guidelines=None, guideline_vision_config=None, jev=None, jev_api_key=None,
                 jev_client_factory=None, guideline_debug_tools=False):
        if mode not in {"demo", "live"}:
            raise ValueError("mode must be demo or live")
        for name, value in (("top_m", top_m), ("top_k", top_k), ("max_stage_attempts", max_stage_attempts)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.mode = mode
        if type(guideline_debug_tools) is not bool:
            raise ValueError("guideline_debug_tools must be a boolean")
        self.guideline_debug_tools = guideline_debug_tools
        self.data_dir = Path(data_dir)
        self.workspace_root = Path(workspace_root)
        self.rules_path = rules_path
        self.top_m, self.top_k = top_m, top_k
        self.max_stage_attempts = max_stage_attempts
        self.patient_shell = patient_shell if isinstance(patient_shell, PatientShellConfig) else PatientShellConfig.model_validate(patient_shell or {})
        self.guidelines = guidelines if isinstance(guidelines, GuidelineToolConfig) else GuidelineToolConfig.model_validate(guidelines or {})
        self.jev = jev if isinstance(jev, JevSettings) else JevSettings.model_validate(jev or {})
        if mode == "live" and jev_client_factory is not None:
            raise ValueError("jev_client_factory is only for offline tests/demo")
        self._jev_key = jev_api_key
        self._jev_client_factory = jev_client_factory
        self.guideline_vision_config = None
        if guideline_vision_config is not None:
            if mode != "live":
                raise ValueError("Demo mode must not use a live guideline vision model")
            if not self.guidelines.enabled or not self.guidelines.vision_profile:
                raise ValueError("Enable guidelines and select a vision_profile before providing its model configuration")
            self.guideline_vision_config = Config.resolve(guideline_vision_config, environ={})
            self.guideline_vision_config.require_model()
            if self.guideline_vision_config.api_mode != "chat_completions":
                raise ValueError("Guideline vision requires chat_completions with image support")
        # Serialize and revalidate to isolate this run configuration from caller mutations.
        definition = plan.model_dump() if isinstance(plan, WorkflowPlan) else plan
        self._plan_json = WorkflowPlan.model_validate({} if definition is None else definition).model_dump_json()
        active_roles = self.plan.active_roles
        if mode == "live":
            if llm_factory is not None:
                raise ValueError("llm_factory is only for offline tests/demo")
            if agent_configs is None or not set(active_roles) <= set(agent_configs):
                raise ValueError("agent_configs must configure every active task agent and ReviewAgent")
            if set(agent_configs) - set(AGENTS):
                raise ValueError("agent_configs contains unknown roles")
            configs = {role: Config.resolve(agent_configs[role], environ={}) for role in active_roles}
            for cfg in configs.values():
                cfg.require_model()
            if any(configs[stage.agent].model == configs["ReviewAgent"].model for stage in self.plan.stages):
                raise ValueError("ReviewAgent must use a different model ID from the task agents")
        else:
            task_config = Config(model="offline-task", max_tool_iterations=8)
            review_config = Config(model="offline-review", max_tool_iterations=8)
            configs = {stage.agent: task_config for stage in self.plan.stages}
            configs.update(ReviewAgent=review_config)
        self.configs = configs
        # Store the callable, not a model instance; each turn gets fresh model state.
        self.llm_factory = llm_factory or (DemoLLM if mode == "demo" else None)

    @property
    def plan(self):
        """Return a validated copy so callers cannot mutate the stored plan."""
        return WorkflowPlan.model_validate_json(self._plan_json)

    def run(self, input=None, *, patient_id=None, cutoff_day=None, cancer_id=None, start_day=None,
            verbose=False, max_print_chars=4000):
        """Run with optional live console mirroring and detailed per-turn reports.

        ``verbose=True`` prints every task/review turn as it finishes, including
        failed turns. ``max_print_chars=None`` displays complete tool payloads.
        Display settings never change the model's context budget or saved data.
        """
        if type(verbose) is not bool:
            raise ValueError("verbose must be a boolean")
        validate_print_limit(max_print_chars)
        request = resolve_request(input, data_dir=self.data_dir, patient_id=patient_id,
                                  cutoff_day=cutoff_day, start_day=start_day, cancer_id=cancer_id)
        directory = create_run_directory(self.workspace_root).resolve()
        self.last_run_dir = directory
        log_path = directory / "logs" / "console.log"
        # stdout/stderr redirection is process-wide: concurrent experiments must
        # use separate processes, not threads, to keep console logs isolated.
        caller_stdout, caller_stderr = sys.stdout, sys.stderr
        with log_path.open("x", encoding="utf-8") as log:
            stdout = ConsoleTee(log, caller_stdout) if verbose else log
            stderr = ConsoleTee(log, caller_stderr) if verbose else log
            with redirect_stdout(stdout), redirect_stderr(stderr):
                return self._run_logged(directory, request, verbose, max_print_chars)

    def _run_logged(self, directory, request, verbose, max_print_chars):
        """Keep all console output, including turn reports, in the experiment log."""
        print("Workflow started", flush=True)
        try:
            result = self._run(directory=directory, **request,
                               verbose=verbose, max_print_chars=max_print_chars)
        except BaseException as exc:
            # Do not copy raw exceptions (which may contain credentials).
            print(f"Workflow aborted: {type(exc).__name__}", flush=True)
            raise
        print(f"Workflow finished: {result['status']}", flush=True)
        return result

    def _run(self, *, directory, patient_id, cutoff_day, cancer_id, start_day, input,
             verbose=False, max_print_chars=4000):
        """Execute task/review pairs with bounded retries and one shared snapshot."""
        plan = self.plan
        jev_evaluator = JevEvaluator(self.jev, api_key=self._jev_key, allow_network=self.mode == "live",
                                     client=self._jev_client_factory() if self.jev.enabled and self._jev_client_factory else None)
        shared = SharedPatientState(patient_id=patient_id, cutoff_day=cutoff_day, cancer_id=cancer_id,
                                    data_dir=self.data_dir, rules_path=self.rules_path, start_day=start_day)
        guideline_library, guideline_snapshot = None, None
        if self.guidelines.enabled:
            guideline_library = GuidelineLibrary(self.guidelines.library_config)
            try:
                guideline_snapshot = guideline_library.snapshot_info()
            except (OSError, ValueError):
                guideline_snapshot = {"status": "unavailable", "reason": "Build or repair the configured document library."}
        result = {
            "schema_version": "2.0", "mode": self.mode, "status": "running", "finished": False,
            "clinical_validation": "not_performed", "scientific_tools": "guideline_debug_fixtures" if self.guideline_debug_tools else "placeholders",
            "patient_id": patient_id, "cutoff_day": cutoff_day, "start_day": start_day,
            "input": input, "patient_shell": self.patient_shell.model_dump(mode="json"),
            "guidelines": self.guidelines.model_dump(mode="json"),
            "guideline_debug_tools": self.guideline_debug_tools,
            "guideline_vision_model": self.guideline_vision_config.model if self.guideline_vision_config else None,
            "guideline_snapshot": guideline_snapshot,
            "jev": jev_evaluator.metadata(),
            "patient_state_sha256": None, "task_dir": str(directory.resolve()),
            "log_path": str(directory / "logs" / "console.log"),
            "models": {role: cfg.model for role, cfg in self.configs.items()},
            "llm_settings": {role: {"provider": cfg.default_provider, "model": cfg.model,
                                    "api_mode": cfg.api_mode, "reasoning_effort": cfg.reasoning_effort,
                                    "proxy_port": cfg.proxy_port,
                                    "context": cfg.context.model_dump()}
                             for role, cfg in self.configs.items()},
            "stages": [], "turns": [], "accepted_outputs": {},
            "rsi_applied": False, "meta_enabled": False,
            "workflow_plan": plan.model_dump(),
        }
        save_json(directory / "workflow_plan.json", plan.model_dump())
        save_json(directory / "request.json", {
            "patient_id": patient_id, "cutoff_day": cutoff_day, "start_day": start_day,
            "input": input, "patient_shell": self.patient_shell.model_dump(mode="json"),
            "cancer_id": cancer_id, "mode": self.mode, "top_m": self.top_m, "top_k": self.top_k,
            "max_stage_attempts": self.max_stage_attempts,
        })

        def invoke(role, context):
            """Execute and persist ONE complete Agent turn, not one LLM request.

            Create the role's Agent, let its model/tool loop finish, and record a
            structured success/failure envelope. The outer loop decides whether
            to proceed, retry or stop after the ReviewAgent's separate turn.
            """
            number = len(result["turns"]) + 1
            context = {**context, "turn": number}
            # Minimum width of three digits; larger turn numbers are not truncated.
            step_dir = directory / f"{number:03d}_{role}"
            step_dir.mkdir()
            save_json(step_dir / "context.json", context)
            if verbose:
                print(f"\n>>> Starting turn {number:03d}: {role} | stage={context['stage']} "
                      f"| attempt={context['attempt']} | reviews_turn={context.get('reviews_turn')}", flush=True)
            output, error, artifacts = None, None, {}
            agent = None
            try:
                # Offline: obtain a model double for this role. Live: passing None
                # makes BasicAgent build the real LLM from self.configs[role].
                llm = self.llm_factory(role) if self.llm_factory else None
                agent = AGENTS[role](shared=shared, context=context, directory=step_dir,
                                     config=self.configs[role], llm=llm,
                                     guidelines=self.guidelines, guideline_vision_config=self.guideline_vision_config,
                                     guideline_library=guideline_library,
                                     jev_evaluator=jev_evaluator,
                                     guideline_debug_tools=self.guideline_debug_tools,
                                     **({"shell_config": self.patient_shell} if role == "PatientStateAgent" else {}))
                artifacts = agent.artifacts
                run = agent.run(input)
                success = run.finished and agent.submitted is not None
                output = agent.submitted if success else None
                error = None if success else run.info.get("stop_reason", "missing_structured_output")
            except Exception as exc:
                # No raw exception text: provider/client errors can contain credentials.
                success, error = False, f"agent_exception:{type(exc).__name__}"
            envelope = {
                "turn": number, "kind": "review" if role == "ReviewAgent" else "task",
                "reviews_turn": context.get("reviews_turn"),
                "agent": role, "stage": context["stage"], "attempt": context["attempt"],
                "status": "completed" if success else "failed",
                "patient_state_sha256": shared.read()["patient_state_sha256"] if shared.ready else None,
                "output": output, "error": error,
                "evidence_assessments": agent.jev_tool.results if agent is not None and agent.jev_tool else [],
                "artifacts": {key: ([str(Path(p).relative_to(directory)) for p in value]
                                    if isinstance(value, list) else str(Path(value).relative_to(directory)))
                              for key, value in artifacts.items()},
            }
            save_json(step_dir / "output.json", envelope)
            result["turns"].append(envelope)
            result["jev"] = jev_evaluator.metadata()
            if verbose:
                print_turn_report(envelope, directory, max_chars=max_print_chars)
            return envelope

        for spec in plan.stages:
            stage = spec.agent
            feedback = None
            attempts = spec.max_attempts or self.max_stage_attempts
            for attempt in range(1, attempts + 1):
                context = {"stage": stage, "attempt": attempt, "top_m": self.top_m, "top_k": self.top_k,
                           "user_request": input,
                           "patient_scope": {"patient_id": patient_id, "cutoff_day": cutoff_day,
                                             "start_day": start_day, "cancer_id": cancer_id},
                           "instructions": spec.instructions, "max_attempts": attempts,
                           "accepted_outputs": result["accepted_outputs"], "previous_feedback": feedback}
                stage_result = invoke(stage, context)
                review = invoke("ReviewAgent", {**context, "stage_result": stage_result,
                                "reviews_turn": stage_result["turn"],
                                "stage_instructions": spec.instructions,
                                "instructions": plan.review_instructions})
                # Future RSI hook (disabled): invoke("MetaAgent", stage/review feedback).
                # Propose a new validated WorkflowPlan for a NEXT run; never mutate this run.
                feedback = {"stage_result": stage_result, "review_result": review}
                result["stages"].append(feedback)
                if review["status"] != "completed":
                    result["status"] = "failed"
                    break
                disposition = review["output"]["disposition"]
                if disposition == "proceed":
                    if stage_result["status"] != "completed":
                        result["status"] = "failed"
                        break
                    result["accepted_outputs"][stage] = stage_result["output"]
                    break
                if disposition == "block":
                    result["status"] = "blocked" if stage_result["status"] == "completed" else "failed"
                    break
                if attempt == attempts:
                    result["status"] = "revision_limit"
            if result["status"] != "running":
                break
        if result["status"] == "running":
            result["status"] = "completed"
            result["finished"] = True
        if shared.ready:
            snapshot = shared.read()
            assert snapshot["patient_state_sha256"] == digest(snapshot["patient_state"])
            result["patient_state_sha256"] = snapshot["patient_state_sha256"]
            save_json(directory / "shared_patient_state.json", snapshot)
            # Compatible with the separate bpc_state_extractor evaluation exporter hash.
            save_json(directory / "patient_state.json", snapshot["patient_state"])
            # Inspect exactly the compact projection returned by patient-state tools.
            save_json(directory / "patient_model_view.json", shared.model_view())
        save_json(directory / "workflow_result.json", result)
        return result
