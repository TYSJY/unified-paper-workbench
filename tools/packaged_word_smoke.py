"""Test the packaged Word command in an isolated, synthetic paper workspace."""
import argparse
import json
import os
import subprocess
from pathlib import Path
from undergraduate_word_smoke import prepare
from unified_paper_orchestrator.undergraduate_word import inspect_docx

parser=argparse.ArgumentParser()
parser.add_argument('--exe',required=True)
parser.add_argument('--output',required=True)
args=parser.parse_args()
root=Path(args.output).resolve()
if root.exists():
    raise SystemExit('Use a new isolated output directory')
paper_id=prepare(root)
env=os.environ.copy()
env.pop('PYTHONPATH',None)
env.pop('PYTHONHOME',None)
env.update({'UNIFIED_PAPER_ORCHESTRATOR_ROOT':str(root/'app-state'),
            'UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT':str(root/'app-state'),
            'UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY':'1', 'UNIFIED_PAPER_WORKSPACE':'',
            'UNIFIED_WRITING_ROOT':str(root/'writing'), 'UNIFIED_FIGURE_ROOT':str(root),
            'UNIFIED_JOURNAL_AUTHOR_ROOT':str(root/'journal'), 'UNIFIED_SUBMISSION_ROOT':str(root/'submission'),
            'CODEX_HOME':str(root/'empty-codex')})
result=subprocess.run([str(Path(args.exe).resolve()),'undergraduate-word-export','--root',str(root),'--paper-id',paper_id],
                      env=env,cwd=root,capture_output=True,encoding='utf-8',errors='replace',timeout=180,
                      creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
(root/'packaged-export.log').write_text(result.stdout+result.stderr,encoding='utf-8')
assert result.returncode==0, (result.stdout+result.stderr)[-3000:]
receipt=json.loads((root/'papers'/paper_id/'delivery/word_delivery.json').read_text(encoding='utf-8'))
counts=inspect_docx(root/receipt['docx']['path'],receipt['expected'],'本科论文 Word 交付验证样稿')
output={'ok':True,'exe':str(Path(args.exe).resolve()),'docx':str(root/receipt['docx']['path']),
        'pdf':str(root/receipt['pdf']['path']), 'pages':[str(root/p['path']) for p in receipt['pages']],
        'counts':counts,'renderer':receipt['renderer'],'model_turns':0,'visual_review':'pending'}
(root/'result.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(output,ensure_ascii=False))
