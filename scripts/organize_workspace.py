"""Archive recognized test-only Agent runs; never remove or move workflow results.

Dry-run by default. --apply moves logs into _test_runs and writes a run index.
No model calls or network access. Existing log file contents are unchanged.
"""

import argparse
import json
from pathlib import Path
import re
from uuid import uuid4


TEST_LOG = re.compile(r"(?:test|calculator)_\d{8}_\d{6}_\d{6}_[0-9a-f]{32}\.json")


def test_runs(date_dir):
    result = []
    for directory in sorted(date_dir.iterdir()):
        if directory.is_symlink() or not directory.is_dir() or not re.fullmatch(r"run_[0-9a-f]{32}", directory.name):
            continue
        if {p.name for p in directory.iterdir()} != {"logs"}:
            continue
        logs = directory / "logs"
        if logs.is_symlink() or not logs.is_dir():
            continue
        paths = list(logs.iterdir())
        if not paths or any(p.is_symlink() or not p.is_file() or not TEST_LOG.fullmatch(p.name) for p in paths):
            continue
        try:
            records = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
        except (OSError, ValueError):
            continue
        if all(isinstance(r, dict) and isinstance(r.get("history"), list)
               and isinstance(r.get("transcript"), list) and "GENIE-" not in json.dumps(r) for r in records):
            result.append(directory)
    return result


def organize(date_dir, *, apply=False):
    date_dir = Path(date_dir).resolve(strict=True)
    selected = test_runs(date_dir)
    if not apply:
        return {"mode": "dry-run", "test_runs": len(selected)}
    archive = date_dir / "_test_runs"
    index = date_dir / "RUN_INDEX.md"
    if archive.is_symlink():
        raise ValueError("Archive directory must not be a symlink")
    if index.is_symlink():
        raise ValueError("Index must not be a symlink")
    if selected:
        # Check every destination before any move; never overwrite an old run.
        if any((archive / p.name).exists() or (archive / p.name).is_symlink() for p in selected):
            raise FileExistsError("A destination run already exists")
        archive.mkdir(exist_ok=True)
        plan = [{"from": p.name, "to": str(Path("_test_runs") / p.name)} for p in selected]
        manifest = archive / f"move_plan_{uuid4().hex}.json"
        with manifest.open("x", encoding="utf-8") as handle:
            json.dump({"note": "Planned moves; verify destinations if execution was interrupted.",
                       "moves": plan}, handle, ensure_ascii=False, indent=2)
        for source in selected:
            source.rename(archive / source.name)
    lines = [f"# {date_dir.name} 实验索引", "",
             "每个 run 目录对应一次 workflow 调用；内部多个 Agent/Review 阶段不是独立实验。", "",
             "| 实验目录 | 模式 | 患者 | 截止日 | 状态 | Agent/Review turn 数 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for directory in sorted(date_dir.glob("run_*")):
        result_file = directory / "workflow_result.json"
        if not result_file.is_file() or directory.is_symlink():
            continue
        result = json.loads(result_file.read_text(encoding="utf-8"))
        values = [result.get(key, "unknown") for key in ("mode", "patient_id", "cutoff_day", "status")]
        cells = [str(value).replace("|", "\\|").replace("\n", " ") for value in values]
        lines.append(f"| [{directory.name}]({directory.name}/workflow_result.json) | "
                     + " | ".join(cells) + f" | {len(result.get('turns', []))} |")
    count = len(list(archive.glob("run_*"))) if archive.exists() else 0
    lines += ["", f"`_test_runs/` 保留 {count} 个旧回归测试日志目录，未删除；移动清单也在该目录内。",
              "新测试日志改用系统临时目录，测试结束后清理，不再混入实验目录。", ""]
    index.write_text("\n".join(lines), encoding="utf-8")
    return {"mode": "applied", "moved": len(selected), "archived_total": count, "index": str(index)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("date_dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(organize(args.date_dir, apply=args.apply), ensure_ascii=False))
