"""Run real work inside the kernel, so Colab does not see an idle notebook."""
from pathlib import Path
import contextlib,runpy,sys
p=next(p for p in [Path('/content/h3managedworker.py'),Path('/h3managedworker.py'),Path.cwd()/'h3managedworker.py'] if p.is_file())
print('H3_LAUNCHED',flush=True)
sys.path.insert(0,str(p.parent))
with open('/content/h3-managed-start.log','a',buffering=1) as log:
    with contextlib.redirect_stdout(log),contextlib.redirect_stderr(log):
        failed=False
        try:runpy.run_path(str(p),run_name='__main__')
        except BaseException:failed=True;raise
        finally:
            if progress:=sys.modules.get('h3_preparation'):progress.close_all(failed)
