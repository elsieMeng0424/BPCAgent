"""Plan-driven task/review loops. One complete Agent invocation is one turn."""

from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

from ..agents import Config
from ..log_paths import create_run_directory
from .agents import AGENTS
from .demo import DemoLLM
from .definition import WorkflowPlan
from .state import SharedPatientState, digest, save_json


class AgentWorkflow:
    def __init__(self, *, data_dir, workspace_root, rules_path=None, mode="demo",
                 agent_configs=None, plan=None,
                 top_m=5, top_k=5, max_stage_attempts=2, llm_factory=None):
        if mode not in {"demo", "live"}:
            raise ValueError("mode must be demo or live")
        for name, value in (("top_m", top_m), ("top_k", top_k), ("max_stage_attempts", max_stage_attempts)):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self.mode = mode
        self.data_dir = Path(data_dir)
        self.workspace_root = Path(workspace_root)
        self.rules_path = rules_path
        self.top_m, self.top_k = top_m, top_k
        self.max_stage_attempts = max_stage_attempts
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
        self.llm_factory = llm_factory or (DemoLLM if mode == "demo" else None)

    @property
    def plan(self):
        return WorkflowPlan.model_validate_json(self._plan_json)

    def run(self, *, patient_id, cutoff_day, cancer_id=None, start_day=None):
        directory = create_run_directory(self.workspace_root).resolve()
        self.last_run_dir = directory
        log_path = directory / "logs" / "console.log"
        # stdout/stderr redirection is process-wide: concurrent experiments must
        # use separate processes, not threads, to keep console logs isolated.
        with log_path.open("x", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
            print("Workflow started", flush=True)
            try:
                result = self._run(directory=directory, patient_id=patient_id, cutoff_day=cutoff_day,
                                   cancer_id=cancer_id, start_day=start_day)
            except BaseException as exc:
                # Do not copy raw exceptions (which may contain credentials).
                print(f"Workflow aborted: {type(exc).__name__}", flush=True)
                raise
            print(f"Workflow finished: {result['status']}", flush=True)
            return result

    def _run(self, *, directory, patient_id, cutoff_day, cancer_id, start_day):
        plan = self.plan
        shared = SharedPatientState(patient_id=patient_id, cutoff_day=cutoff_day, cancer_id=cancer_id,
                                    data_dir=self.data_dir, rules_path=self.rules_path, start_day=start_day)
        result = {
            "schema_version": "2.0", "mode": self.mode, "status": "running", "finished": False,
            "clinical_validation": "not_performed", "scientific_tools": "placeholders",
            "patient_id": patient_id, "cutoff_day": cutoff_day, "start_day": start_day,
            "patient_state_sha256": None, "task_dir": str(directory.resolve()),
            "log_path": str(directory / "logs" / "console.log"),
            "models": {role: cfg.model for role, cfg in self.configs.items()},
            "llm_settings": {role: {"provider": cfg.default_provider, "model": cfg.model,
                                    "api_mode": cfg.api_mode, "reasoning_effort": cfg.reasoning_effort}
                             for role, cfg in self.configs.items()},
            "stages": [], "turns": [], "accepted_outputs": {},
            "rsi_applied": False, "meta_enabled": False,
            "workflow_plan": plan.model_dump(),
        }
        save_json(directory / "workflow_plan.json", plan.model_dump())
        save_json(directory / "request.json", {
            "patient_id": patient_id, "cutoff_day": cutoff_day, "start_day": start_day,
            "cancer_id": cancer_id, "mode": self.mode, "top_m": self.top_m, "top_k": self.top_k,
            "max_stage_attempts": self.max_stage_attempts,
        })

        def invoke(role, context):
            number = len(result["turns"]) + 1
            context = {**context, "turn": number}
            step_dir = directory / f"{number:02d}_{role}"
            step_dir.mkdir()
            save_json(step_dir / "context.json", context)
            output, error, artifacts = None, None, {}
            try:
                llm = self.llm_factory(role) if self.llm_factory else None
                agent = AGENTS[role](shared=shared, context=context, directory=step_dir,
                                     config=self.configs[role], llm=llm)
                artifacts = agent.artifacts
                run = agent.run("Read shared patient state and stage context, perform your role, then submit_output.")
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
                "artifacts": {key: ([str(Path(p).relative_to(directory)) for p in value]
                                    if isinstance(value, list) else str(Path(value).relative_to(directory)))
                              for key, value in artifacts.items()},
            }
            save_json(step_dir / "output.json", envelope)
            result["turns"].append(envelope)
            return envelope

        for spec in plan.stages:
            stage = spec.agent
            feedback = None
            attempts = spec.max_attempts or self.max_stage_attempts
            for attempt in range(1, attempts + 1):
                context = {"stage": stage, "attempt": attempt, "top_m": self.top_m, "top_k": self.top_k,
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
            # Compatible with the separate bpc-state evaluation exporter hash.
            save_json(directory / "patient_state.json", snapshot["patient_state"])
        save_json(directory / "workflow_result.json", result)
        return result
