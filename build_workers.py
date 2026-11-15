"""Pack the same lease, render and delivery logic into the two Colab entry points."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parent

def build():
    path=ROOT/'colab-worker.ipynb'
    nb=json.loads(path.read_text())
    nb['cells'][0]['source']=['# 连接映室工作台\n','在 Colab 选择 A100、H100 或 G4，运行代码单元格，看到「工作台已连接」即可返回网页提交创意。']
    code=(ROOT/'colab-worker.py').read_text()
    cell=next(c for c in nb['cells'] if c['cell_type']=='code')
    cell.update(source=code.splitlines(True),outputs=[],execution_count=None)
    path.write_text(json.dumps(nb,ensure_ascii=False,indent=1)+'\n')
    backend=(ROOT/'h3_bf16.py').read_text()
    # The package is self-contained; no secrets or mutable remote startup code in either notebook.
    start="from pathlib import Path\nimport sys\nPath('/content/h3_bf16.py').write_text("+repr(backend)+",encoding='utf-8')\nif '/content' not in sys.path:sys.path.insert(0,'/content')\nH3_STUDIO_MODEL='bf16'\n"
    cell['source']=(start+code).splitlines(True)
    nb['cells'][0]['source']=['# 映室 · 原始 BF16 执行端\n','只接原始 BF16 任务。选择空闲的 A100/H100 80 GB 或 G4 96 GB，并确保主机 RAM 满足要求；Drive 缓存官方约 144 GB 权重。运行后使用既有工作台配对信息。和量化端同时在线时请使用不同的运行时。']
    (ROOT/'colab-worker-bf16.ipynb').write_text(json.dumps(nb,ensure_ascii=False,indent=1)+'\n')

if __name__=='__main__':build()
