"""Show the real account/site window with fictional, memory-only records.

Capture this window using the supported native desktop screenshot tool. No
operator account store is opened and no credential operation is performed.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
from types import SimpleNamespace


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    # Keep imported application defaults away from the operator's workspace.
    root_path = repo / "build" / "public-accounts-demo"
    os.environ.update({"UNIFIED_PAPER_ORCHESTRATOR_ROOT": str(root_path),
        "UNIFIED_PAPER_ORCHESTRATOR_V2_ROOT": str(root_path),
        "UNIFIED_PAPER_WORKSPACE_DISABLE_REGISTRY": "1", "UNIFIED_PAPER_WORKSPACE": ""})
    import tkinter as tk
    from tkinter import ttk
    from unified_paper_orchestrator.ui.account_site_manager import AccountSiteManager

    rows = [{"record_key": f"vault:demo-{i}", "account_id": f"demo-{i}",
        "username": username, "email": email, "provider": provider, "scope": scope,
        "source_kind": "vault", "status": "verified", "login_url": "https://example.test/login"}
        for i, (provider, scope, username, email) in enumerate([
            ("示例投稿系统 A", "journal_system", "demo_author", "author@example.test"),
            ("示例投稿系统 B", "journal_system", "demo_researcher", "researcher@example.test"),
            ("示例学校邮箱", "email", "demo_student", "student@example.test"),
            ("ORCID · 虚构演示", "external", "0000-0000-0000-0000", "author@example.test"),
        ])]
    systems = [{"system_id": f"site-{i}", "label": f"示例投稿系统 {label}",
        "login_url": "https://example.test/login", "submission_url": "https://example.test/submit",
        "status": "verified"} for i, label in enumerate(("A", "B"))]
    journals = [{"journal_id": f"journal-{i}", "name": name, "system_id": f"site-{i % 2}",
        "status": "verified"} for i, name in enumerate(("软件工程演示期刊", "公共管理演示期刊", "智能系统演示期刊"))]
    store = SimpleNamespace(vault_read_only=False, list_account_summaries=lambda: rows,
        list_systems=lambda: systems, list_journals=lambda: journals,
        is_account_read_only=lambda _: False)
    root = tk.Tk()
    root.withdraw()
    ttk.Style(root).configure(".", font=("Microsoft YaHei UI", 10))
    window = AccountSiteManager(root, store=store)
    window.title("账号与投稿站点 · 公开演示（全部为虚构资料）")
    window.minsize(1000, 590)
    window.geometry("1100x610+20+20")
    window.wm_transient("")
    window.deiconify()
    window.lift()
    window.protocol("WM_DELETE_WINDOW", root.destroy)
    root.after(max(10, args.seconds) * 1000, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()
