"""Exercise the real GUI controller's Word actions without opening real files."""
import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
parser.add_argument("--fixture", required=True)
args = parser.parse_args()
output = Path(args.output).resolve()
if output.exists():
    raise SystemExit("Use a new isolated output directory")
output.mkdir(parents=True)
fixture = Path(args.fixture).resolve()
os.environ.update({
    "UNIFIED_PAPER_ORCHESTRATOR_ROOT":str(output), "UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT":str(output),
    "UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY":"1", "UNIFIED_PAPER_WORKSPACE":"",
    "UNIFIED_WRITING_ROOT":str(output/'writing'), "UNIFIED_FIGURE_ROOT":str(fixture),
    "UNIFIED_JOURNAL_AUTHOR_ROOT":str(output/'journal'), "UNIFIED_SUBMISSION_ROOT":str(output/'submission'),
    "QT_QPA_PLATFORM":"offscreen", "QT_QUICK_BACKEND":"software", "QT_QUICK_CONTROLS_STYLE":"Basic",
})
from PySide6.QtGui import QGuiApplication
from unified_paper_orchestrator.ui_next.controller import WorkbenchController
from unified_paper_orchestrator.ui_next.read_model import selected_paper
app = QGuiApplication([])
with patch.object(WorkbenchController, "_bootstrap_commercial", lambda _: None):
    controller = WorkbenchController()
controller._projection_timer.stop()
controller._sync_timer.stop()
paper_id = 'word-delivery-fixture'
record = {"paper_id":paper_id,"title":"Word delivery UI fixture","current_stage":"done","next_action":"none",
          "stages":{"writing":{"status":"complete","prompt_scheme":"undergraduate_software"},
                    "figures":{"status":"complete","word_delivery_ok":True}}}
controller._state = {"papers":{paper_id:record}}
controller._selected_id = paper_id
controller._page = "figures"
controller._selected = selected_paper(controller._state,paper_id)
opened=[]
messages=[]
controller.toastRequested.connect(lambda *args:messages.append(args))
controller.errorRequested.connect(lambda *args:messages.append(args))
try:
    actions = controller.selectedActions
    with patch('unified_paper_orchestrator.ui_next.controller.os.startfile', side_effect=lambda path:opened.append(path)):
        controller.prepareSelectedAction('open_word_delivery')
        controller.prepareSelectedAction('open_word_preview')
    assert len(opened)==2, {"opened":opened,"messages":messages,"actions":actions}
    assert Path(opened[0]).suffix=='.docx' and Path(opened[1]).suffix=='.pdf'
    assert all(Path(path).is_file() for path in opened)
    result={"ok":True,"word_actions":[a for a in actions if 'word' in str(a)],"opened_suffixes":[Path(p).suffix for p in opened],"model_turns":0,"actual_external_opens":0}
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
finally:
    controller.shutdown()
    app.processEvents()
