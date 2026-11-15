"""The launcher must stay active for the lifetime of real GPU work."""
import tempfile,time,runpy,builtins,subprocess
from pathlib import Path
from unittest.mock import patch

def run():
 with tempfile.TemporaryDirectory() as tmp:
  worker=Path(tmp)/'h3managedworker.py';worker.write_text("import time\nprint('REAL_WORK_STARTED',flush=True)\ntime.sleep(0.35)\n")
  children=[];real_open=builtins.open;real_popen=subprocess.Popen
  def local_open(file,*a,**kw):return real_open(Path(tmp)/Path(file).name if str(file).startswith("/content/") else file,*a,**kw)
  def popen(*a,**kw):
   kw["cwd"]=tmp;p=real_popen(*a,**kw);children.append(p);return p
  original=Path.is_file
  def exists(p):return original(p) if str(p).startswith(tmp) else False
  start=time.monotonic()
  with patch('pathlib.Path.cwd',return_value=Path(tmp)),patch('pathlib.Path.is_file',exists),patch('builtins.open',local_open),patch('subprocess.Popen',popen):
   runpy.run_path(str(Path(__file__).parent/'autowake'/'launch.py'),run_name='__main__')
  elapsed=time.monotonic()-start
  for child in children:child.wait(timeout=2)
  assert elapsed>=.3,'Launcher returned while GPU work still ran; kernel becomes idle'
 print('PASS kernel execution lasts for actual work')
if __name__=='__main__':run()
