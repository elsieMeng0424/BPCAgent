from __future__ import annotations

import json
from pathlib import Path

from bpc_state.models import PatientState, EvaluationData


root = Path(__file__).resolve().parents[1]
(root / "schemas" / "patient-state.schema.json").write_text(
    json.dumps(PatientState.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
(root / "schemas" / "evaluation-data.schema.json").write_text(
    json.dumps(EvaluationData.model_json_schema(), ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
