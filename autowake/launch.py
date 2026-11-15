# Executed by Colab CLI on our own named VM, never in a visitor's browser.
from pathlib import Path
import subprocess,sys,fcntl
p=next(p for p in [Path('/content/h3managedworker.py'),Path('/h3managedworker.py'),Path.cwd()/'h3managedworker.py'] if p.is_file())
with open('/content/h3-managed.lock','a') as lock:
    try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);running=False
    except BlockingIOError:running=True
if not running:
    log=open('/content/h3-managed-start.log','ab')
    subprocess.Popen([sys.executable,'-u',str(p)],stdout=log,stderr=log,start_new_session=True,cwd='/content',stdin=subprocess.DEVNULL)
    log.close()
print('H3_LAUNCHED')
