"""CLI with offline demo default and independently configured live models."""

import argparse
import json
from pathlib import Path
import sys

from ..agents.exceptions import ConfigException
from .config import WorkflowConfiguration
from .runner import AgentWorkflow


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the iterative BPC task/review workflow (demo uses no API).")
    parser.add_argument("--config", help="Workflow JSON/JSONC; defaults to ./config/workflow.jsonc when present.")
    parser.add_argument("--data-dir")
    parser.add_argument("--patient-id", required=True)
    parser.add_argument("--cutoff-day", type=int, required=True)
    parser.add_argument("--start-day", type=int, help="Recent observation view start; earlier treatment history is retained.")
    parser.add_argument("--cancer-id")
    parser.add_argument("--rules-path")
    parser.add_argument("--workspace-root")
    parser.add_argument("--mode", choices=["demo", "live"])
    parser.add_argument("--top-m", type=int)
    parser.add_argument("--top-k", type=int)
    parser.add_argument("--max-stage-attempts", type=int)
    args = parser.parse_args(argv)
    try:
        default = Path("config/workflow.jsonc")
        configuration = WorkflowConfiguration(args.config or (default if default.is_file() else None))
        options = configuration.workflow_options({key: getattr(args, key) for key in (
            "data_dir", "workspace_root", "rules_path", "mode", "top_m", "top_k", "max_stage_attempts"
        )})
        configs = configuration.model_configs() if options["mode"] == "live" else {}
        workflow = AgentWorkflow(**options, **configs)
        result = workflow.run(patient_id=args.patient_id, cutoff_day=args.cutoff_day,
                              start_day=args.start_day, cancer_id=args.cancer_id)
        print(json.dumps({"status": result["status"], "mode": result["mode"],
                          "result": str(Path(result["task_dir"]) / "workflow_result.json")}, ensure_ascii=False))
        return 0 if result["finished"] else 1
    except (ValueError, TypeError, OSError, ImportError, ConfigException) as exc:
        print(f"Workflow error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
