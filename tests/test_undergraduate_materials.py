from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path
import pytest

from unified_paper_orchestrator.undergraduate_materials import (
    import_materials, verify_attachments, normalize_attachments, extract_text,
)
from unified_paper_orchestrator.writing_prompt_schemes import normalize_thesis_brief, undergraduate_prompt
from unified_paper_orchestrator.writing_workflow import WritingWorkflowStore, normalize_workflow_snapshot


def docx(path):
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>学校要求：正文一万字，图按章编号。</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>表中文字也须读取</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>')


def test_package_copies_documents_and_pins_content(tmp_path):
    source = tmp_path / "学校资料"
    source.mkdir()
    docx(source / "规范.docx")
    (source / "说明.txt").write_text("院系要求：不得使用模拟调查数据", encoding="utf-8")
    archive = tmp_path / "学校资料.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for file in source.iterdir():
            package.write(file, file.name)
    result = import_materials([str(archive)], tmp_path / "imports")
    assert len(result["items"]) == 2 and not result["warnings"]
    assert all(i["status"] == "text" for i in result["items"])
    extracted = "\n".join(Path(i["text_path"]).read_text(encoding="utf-8") for i in result["items"])
    assert "一万字" in extracted and "表中文字" in extracted
    (source / "说明.txt").write_text("外部文件变化", encoding="utf-8")
    verify_attachments(result["items"], tmp_path / "imports")
    brief = normalize_thesis_brief({"title": "固定题目", "attachments": result["items"]}, required=True)
    store = WritingWorkflowStore(tmp_path / "workflow.json", tmp_path / "custom")
    workflow = normalize_workflow_snapshot({**store.snapshot_for_scheme("undergraduate_humanities"), "thesis_brief": brief})
    assert not store.path.exists()  # Dedicated entry does not switch journal settings.
    prompt = undergraduate_prompt(workflow["steps"][0], workflow, "paper", 1)
    assert "固定题目" in prompt and result["items"][0]["text_path"].replace("\\", "\\\\") in prompt
    Path(result["items"][0]["path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="副本缺失或被修改"):
        undergraduate_prompt(workflow["steps"][0], workflow, "paper", 1)


def test_zip_traversal_symlinks_nested_archives_and_credentials_are_not_imported(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as package:
        for name in ["../escape.txt", "/absolute.txt", "C:/drive.txt", "folder/auth.json", "payload.exe", "nested.zip"]:
            package.writestr(name, "not imported")
        link = zipfile.ZipInfo("link.txt")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(link, "target")
        package.writestr("valid/学校.txt", "学校正文")
    result = import_materials([str(archive)], tmp_path / "imports")
    assert len(result["items"]) == 1 and len(result["warnings"]) == 7
    assert not (tmp_path / "escape.txt").exists()


def test_folder_import_skip_and_manual_status_and_duplicates(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "node_modules").mkdir()
    (source / "node_modules" / "private.json").write_text("not imported")
    (source / "photo.png").write_bytes(b"image fixture")
    (source / "old.doc").write_bytes(b"old fixture")
    (source / "broken.pdf").write_bytes(b"bad pdf")
    (source / "a.md").write_text("same")
    (source / "b.md").write_text("same")
    result = import_materials([str(source)], tmp_path / "imports", "template")
    assert len(result["items"]) == 4
    assert sum(i["status"] == "manual" for i in result["items"]) == 3
    assert any("重复" in warning for warning in result["warnings"])
    assert all(i["role"] == "template" for i in result["items"])


def test_limits_and_invalid_metadata_are_rejected(tmp_path, monkeypatch):
    import unified_paper_orchestrator.undergraduate_materials as module
    monkeypatch.setattr(module, "MAX_FILE_BYTES", 5)
    source = tmp_path / "large.txt"
    source.write_text("larger than five bytes")
    result = import_materials([str(source)], tmp_path / "imports")
    assert not result["items"] and result["warnings"]
    with pytest.raises(ValueError):
        normalize_attachments([{"path": "outside"}])


def test_pdf_text_and_scan_status(tmp_path):
    # Minimal PDF fixture built locally, no external document or model.
    def pdf_bytes(content):
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
        result = b"%PDF-1.4\n"
        offsets = []
        for index, obj in enumerate(objects, 1):
            offsets.append(len(result)); result += str(index).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
        xref = len(result)
        result += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
        return result + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    path = tmp_path / "requirements.pdf"
    path.write_bytes(pdf_bytes(b"BT /F1 12 Tf 20 200 Td (School requirements) Tj ET"))
    text, status, _ = extract_text(path)
    assert "School requirements" in text and status == "text"
    path.write_bytes(pdf_bytes(b""))
    assert extract_text(path)[1] == "manual"
