"""Run real work inside the kernel, so Colab does not see an idle notebook."""
from pathlib import Path
import contextlib,runpy
p=next(p for p in [Path('/content/h3managedworker.py'),Path('/h3managedworker.py'),Path.cwd()/'h3managedworker.py'] if p.is_file())
print('H3_LAUNCHED',flush=True)
with open('/content/h3-managed-start.log','a',buffering=1) as log:
    with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        runpy.run_path(str(p),run_name='__main__')
