"""Run a list of cell sources in a real Jupyter kernel. Returns (outputs, errors).
Kernel: env TEST_KERNEL, default python3 (the kernel of the Python that runs this file)."""
import asyncio, nbformat, os, sys
from nbclient import NotebookClient

def run_cells(cells, cwd, timeout=300, extra_sys_path=()):
    if sys.platform == "win32":      # pyzmq cannot use the default Windows event loop
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    os.makedirs(cwd, exist_ok=True)
    pre = "import sys\n" + "".join(f"sys.path.insert(0, {p!r})\n" for p in extra_sys_path)
    nb = nbformat.v4.new_notebook(cells=[nbformat.v4.new_code_cell(pre)] + [nbformat.v4.new_code_cell(c) for c in cells])
    client = NotebookClient(nb, timeout=timeout, kernel_name=os.environ.get("TEST_KERNEL", "python3"), resources={"metadata": {"path": cwd}}, allow_errors=True)
    client.execute()
    outs, errs = [], []
    for i, c in enumerate(nb.cells[1:]):
        text = ""
        for o in c.outputs:
            if o.output_type == "stream":
                text += o.text
            elif o.output_type == "error":
                errs.append((i, o.ename, o.evalue))
                text += f"[{o.ename}: {o.evalue}]\n"
            elif o.output_type in ("execute_result", "display_data"):
                text += str(o.get("data", {}).get("text/plain", ""))
        outs.append(text)
    return outs, errs
