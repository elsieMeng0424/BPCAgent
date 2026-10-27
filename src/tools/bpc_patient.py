"""内置 BPCStateExtractor 的 Agent 工具适配；调用方提供患者配置和工作目录。"""

from __future__ import annotations

from itertools import count
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, create_model

from .tool import Tool, ToolArgs

if TYPE_CHECKING:
    from bpc_state import BoundedPatientTool


class StateArgs(ToolArgs):
    cutoff_day: int = Field(description="Inclusive cutoff day relative to the first BPC cancer diagnosis.")


class EvidenceArgs(StateArgs):
    evidence_refs: list[str] = Field(
        min_length=1, description="Evidence references returned in the patient state for the same cutoff day.",
    )


def _bound_schema(base: type[StateArgs], max_cutoff_day: int):
    return create_model(
        f"Bound{base.__name__}",
        __base__=base,
        cutoff_day=(int, Field(
            le=max_cutoff_day,
            description="Inclusive cutoff day relative to the first BPC cancer diagnosis; negative days are allowed.",
        )),
    )


class PatientArtifactStore:
    """同次运行的两个工具共享此对象，以共用目录和文件序号。

    供同步工具循环使用；目录由调用方选择，不向模型暴露路径参数。
    """

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self._sequence = count(1)

    def save(self, kind: Literal["state", "evidence"], cutoff_day: int, data: Any) -> Path:
        text = json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        path = self.directory / f"patient_{kind}_day_{cutoff_day}_{next(self._sequence):03d}.json"
        with path.open("x", encoding="utf-8") as file:
            file.write(text)
        print(f"Saved: {path}")
        return path


class PatientStateTool(Tool[StateArgs]):
    """提取并保存状态，成功后向模型返回原始状态字典。"""

    def __init__(self, bound: BoundedPatientTool, store: PatientArtifactStore):
        super().__init__(
            name="get_patient_state",
            description=f"Retrieve known facts for patient {bound.patient_id}. The cutoff day must not exceed {bound.max_cutoff_day}.",
        )
        self.bound = bound
        self.store = store
        self.args_schema = _bound_schema(StateArgs, bound.max_cutoff_day)

    def run(self, parameters: StateArgs) -> dict[str, Any]:
        state = self.bound.get_patient_state(parameters.cutoff_day)
        self.store.save("state", parameters.cutoff_day, state)
        return state


class PatientEvidenceTool(Tool[EvidenceArgs]):
    """保存证据及查询上下文，成功后向模型返回原始证据字典。"""

    def __init__(self, bound: BoundedPatientTool, store: PatientArtifactStore):
        super().__init__(
            name="get_patient_evidence",
            description=(
                "Retrieve evidence sources and allowed field values using the same cutoff day as the original references. "
                f"The cutoff must not exceed {bound.max_cutoff_day}. Full raw medical records are not returned."
            ),
        )
        self.bound = bound
        self.store = store
        self.args_schema = _bound_schema(EvidenceArgs, bound.max_cutoff_day)

    def run(self, parameters: EvidenceArgs) -> dict[str, Any]:
        evidence = self.bound.get_evidence(parameters.cutoff_day, parameters.evidence_refs)
        self.store.save("evidence", parameters.cutoff_day, {
            "patient_id": self.bound.patient_id,
            "cancer_id": self.bound.cancer_id,
            "cutoff_day": parameters.cutoff_day,
            "evidence_refs": parameters.evidence_refs,
            "data": evidence,
        })
        return evidence


def create_patient_tools(
    *,
    patient_id: str,
    max_cutoff_day: int,
    data_dir: str | Path,
    work_dir: str | Path,
    rules_path: str | Path | None = None,
    cancer_id: str | None = None,
) -> tuple[PatientStateTool, PatientEvidenceTool]:
    """创建共享提取器、患者绑定和保存器的两个工具。

    bpc_state 随 BPCAgent 一并安装，不依赖外部项目的源码路径。
    工作目录由调用方按任务隔离；规则路径省略时使用内置默认资源。
    函数不读取环境文件，也不提供未来评估数据工具。
    """
    from bpc_state import BPCStateExtractor, BoundedPatientTool

    extractor = BPCStateExtractor(data_dir, rules_path=rules_path)
    bound = BoundedPatientTool(extractor, patient_id, max_cutoff_day, cancer_id)
    store = PatientArtifactStore(work_dir)
    return PatientStateTool(bound, store), PatientEvidenceTool(bound, store)
