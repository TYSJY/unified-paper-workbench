from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RepositoryHygieneTest(unittest.TestCase):
    def test_sensitive_runtime_state_and_generated_trees_are_ignored(self) -> None:
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("state/**", "node_modules/", "**/node_modules/", "build/", "dist/"):
            self.assertIn(pattern, text)
        self.assertIn("credential_store.local.json", text)

    def test_build_tests_before_cleanup_and_produces_one_named_exe(self) -> None:
        text = (ROOT / "tools" / "build_exe.ps1").read_text(encoding="utf-8")
        test_index = text.index(
            'Invoke-IsolatedPython -Arguments @("-S", "-P", "-B", "-m", "pytest"'
        )
        cleanup_index = text.index("Remove-GeneratedTree -Path $buildDir")
        build_index = text.index('"PyInstaller"', cleanup_index)
        self.assertLess(test_index, cleanup_index)
        self.assertLess(cleanup_index, build_index)
        self.assertIn("$startInfo.Environment.Clear()", text)
        self.assertIn('"-S",', text)
        self.assertIn('"-P",', text)
        self.assertIn("Refusing to remove path outside workspace", text)
        self.assertIn('$promptDir = Join-Path $root "prompts"', text)
        self.assertIn('"--add-data",', text)
        self.assertIn('"$promptDir;prompts",', text)
        self.assertIn('$entryPoint = Join-Path $root "unified_paper_orchestrator\\app.py"', text)
        self.assertIn('$executables.Count -ne 1', text)
        self.assertIn('UnifiedPaperOrchestrator.exe', text)

    def test_docs_describe_multilingual_smart_only_product_cutover(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        architecture = (ROOT / "docs" / "ARCHITECTURE.md").read_text(encoding="utf-8")
        smart_doc = (ROOT / "docs" / "SMART_SUBMISSION_ARCHITECTURE.md").read_text(encoding="utf-8")
        dual_doc = (ROOT / "docs" / "DUAL_SUBMISSION_STRATEGY.md").read_text(encoding="utf-8")
        self.assertIn("运行时切换简体中文和英文", readme)
        self.assertIn("顶部“紧急停止”", readme)
        self.assertIn("投稿只使用 Smart Submission", readme)
        self.assertIn("Qt Quick 是唯一正式桌面壳层", architecture)
        self.assertIn("旧稳定投稿文件只用于读取历史", architecture)
        self.assertIn("唯一可执行生产路线", smart_doc)
        self.assertIn("历史文档", dual_doc)


if __name__ == "__main__":
    unittest.main()
