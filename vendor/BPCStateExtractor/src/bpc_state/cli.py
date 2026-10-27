from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .extractor import BPCStateExtractor, DEFAULT_DATA_DIR
from .models import PatientState
from .models import EvaluationData
from .tool_schema import evidence_tool_schema, state_tool_schema


def _write(payload: object, output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpc-state", description="Extract cutoff-safe GENIE BPC patient state"
    )
    parser.add_argument(
        "--data-dir",
        default=str(DEFAULT_DATA_DIR),
        help="Directory containing the BPC release files",
    )
    parser.add_argument(
        "--rules-path", help="Optional schema 1.1 field/time rules JSON"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    state = sub.add_parser("state", help="Extract patient state")
    state.add_argument("--patient-id", required=True)
    state.add_argument("--cutoff-day", required=True, type=int)
    state.add_argument("--cancer-id")
    state.add_argument("--output")
    state.add_argument(
        "--audit-out", help="Optional sidecar with excluded record refs and reasons"
    )

    evidence = sub.add_parser("evidence", help="Resolve visible evidence references")
    evidence.add_argument("--patient-id", required=True)
    evidence.add_argument("--cutoff-day", required=True, type=int)
    evidence.add_argument("--cancer-id")
    evidence.add_argument("--ref", action="append", required=True, dest="refs")
    evidence.add_argument("--output")

    schema = sub.add_parser("schema", help="Print or save the PatientState JSON Schema")
    schema.add_argument("--output")

    evaluation = sub.add_parser(
        "evaluation", help="OFFLINE ONLY: export actual follow-up into a separate file"
    )
    evaluation.add_argument("--patient-id", required=True)
    evaluation.add_argument("--cutoff-day", type=int, required=True)
    evaluation.add_argument("--end-day", type=int, required=True)
    evaluation.add_argument("--cancer-id")
    evaluation.add_argument(
        "--output",
        required=True,
        help="Separate offline file; do not expose this directory to the Agent",
    )

    evaluation_schema = sub.add_parser(
        "evaluation-schema", help="Print the separate offline evaluation JSON Schema"
    )
    evaluation_schema.add_argument("--output")

    tool = sub.add_parser("tool-schema", help="Generate bound Agent tool schemas")
    tool.add_argument("--patient-id", required=True)
    tool.add_argument("--max-cutoff-day", required=True, type=int)
    tool.add_argument("--output")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "schema":
            _write(PatientState.model_json_schema(), args.output)
            return 0
        if args.command == "evaluation-schema":
            _write(EvaluationData.model_json_schema(), args.output)
            return 0
        if args.command == "evaluation":
            from .evaluation import OfflineEvaluationExporter

            exporter = OfflineEvaluationExporter(args.data_dir, args.rules_path)
            _write(
                exporter.export(
                    args.patient_id, args.cutoff_day, args.end_day, args.cancer_id
                ),
                args.output,
            )
            return 0
        if args.command == "tool-schema":
            _write(
                [
                    state_tool_schema(args.patient_id, args.max_cutoff_day),
                    evidence_tool_schema(args.patient_id, args.max_cutoff_day),
                ],
                args.output,
            )
            return 0
        extractor = BPCStateExtractor(args.data_dir, args.rules_path)
        if args.command == "state":
            payload = extractor.get_patient_state(
                args.patient_id, args.cutoff_day, args.cancer_id
            )
            _write(payload, args.output)
            if args.audit_out:
                _write(extractor.get_audit_report(), args.audit_out)
            return 0
        if args.command == "evidence":
            _write(
                extractor.get_evidence(
                    args.patient_id, args.cutoff_day, args.refs, args.cancer_id
                ),
                args.output,
            )
            return 0
    except (FileNotFoundError, KeyError, PermissionError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
