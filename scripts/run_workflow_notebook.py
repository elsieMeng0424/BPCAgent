"""Execute the workflow/Jev notebook cells and persist actual outputs, even errors.

This invokes paid APIs when the notebook selects live mode. Legacy calculator
cells are intentionally left untouched. Run from the repository's virtualenv.
"""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import nbformat
from nbclient import NotebookClient


def main():
    path = Path(__file__).resolve().parents[1] / 'test.ipynb'
    notebook = nbformat.read(path, as_version=4)
    indices = [i for i, cell in enumerate(notebook.cells)
               if cell.id.startswith(('workflow-', 'jev-smoke-'))]
    execution = nbformat.v4.new_notebook(cells=[notebook.cells[i] for i in indices], metadata=notebook.metadata)
    for cell in execution.cells:
        if cell.cell_type == 'code':
            cell.outputs = []
            cell.execution_count = None

    def save():
        for index, cell in zip(indices, execution.cells):
            notebook.cells[index] = cell
        notebook.metadata['live_execution'] = {
            'saved_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'kernel': 'bpcagent', 'scope': 'workflow and Jev smoke; legacy calculator not rerun',
        }
        # These are generated execution artifacts, not source-code replacements.
        nbformat.write(notebook, path)

    def completed(cell, cell_index, execute_reply):
        save()
        errors = [output.get('ename') for output in cell.get('outputs', []) if output.output_type == 'error']
        print(f'Saved {cell.id}; errors={errors}', flush=True)

    save()
    try:
        NotebookClient(execution, timeout=1800, kernel_name='bpcagent', allow_errors=True,
                       resources={'metadata': {'path': str(path.parent)}}, on_cell_executed=completed).execute()
    finally:
        save()
    print('Notebook outputs saved; inspect workflow status as well as cell errors.', flush=True)
    if any(output.output_type == 'error' for cell in execution.cells
           for output in cell.get('outputs', [])):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
