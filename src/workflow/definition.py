"""Declarative workflow plans, separate from scheduling and offline model doubles."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import Stage


class StageSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    agent: Stage
    max_attempts: int | None = Field(None, gt=0)
    instructions: str = ""


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, hide_input_in_errors=True)

    version: str = Field("1", min_length=1)
    stages: list[StageSpec] = Field(default_factory=lambda: [
        StageSpec(agent="PatientStateAgent"),
        StageSpec(agent="TargetSelectionAgent"),
        StageSpec(agent="DrugSelectionAgent"),
    ], min_length=1)
    review_instructions: str = ""

    @model_validator(mode="after")
    def validate_dependencies(self):
        # Plans may stop early, but cannot bypass patient facts or target selection.
        dependencies = {
            "PatientStateAgent": set(),
            "TargetSelectionAgent": {"PatientStateAgent"},
            "DrugSelectionAgent": {"PatientStateAgent", "TargetSelectionAgent"},
        }
        seen = set()
        for stage in self.stages:
            if stage.agent in seen:
                raise ValueError("Duplicate task stage; use max_attempts for review-driven retries")
            if not dependencies[stage.agent] <= seen:
                raise ValueError(f"Missing or out-of-order dependencies for {stage.agent}")
            seen.add(stage.agent)
        return self

    @property
    def active_roles(self):
        return [stage.agent for stage in self.stages] + ["ReviewAgent"]
