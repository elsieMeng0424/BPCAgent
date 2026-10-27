"""Keep real Agent log writes in disposable directories under pytest or unittest."""

from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bpcagent.log_paths import new_log_path


def isolate_agent_logs(case):
    temporary = TemporaryDirectory(prefix="bpc-agent-test-")
    case.addCleanup(temporary.cleanup)
    root = Path(temporary.name) / "workspace"
    patcher = patch("bpcagent.agents.agents.new_log_path", side_effect=partial(new_log_path, root=root))
    patcher.start()
    case.addCleanup(patcher.stop)
    return root
