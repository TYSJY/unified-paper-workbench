from __future__ import annotations

import ast
import base64
import concurrent.futures
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from unified_paper_orchestrator import account_site_store as account_store_module
from unified_paper_orchestrator.account_site_store import (
    AccountSiteStore,
    DuplicateRecordError,
    PostCommitDriftError,
    RegistryValidationError,
)
from unified_paper_orchestrator.credential_vault import DpapiCredentialVault, VaultError
from unified_paper_orchestrator.io_utils import JsonReadError, read_json, write_json_atomic
from unified_paper_orchestrator.ui.account_site_manager import open_account_site_manager


def _protect_for_test(value: str) -> str:
    return base64.b64encode(value.encode("utf-8")[::-1]).decode("ascii")


def _unprotect_for_test(value: str) -> str:
    return base64.b64decode(value.encode("ascii"))[::-1].decode("utf-8")


def _raw_vault_upsert(
    path: Path,
    *,
    record_id: str,
    username: str,
    password: str,
) -> None:
    """Simulate a non-cooperating writer that deliberately bypasses FileLock."""

    data = read_json(path, DpapiCredentialVault.empty())
    records = data.setdefault("credentials", [])
    record = next(
        (item for item in records if str(item.get("id") or "") == record_id),
        None,
    )
    if record is None:
        record = {"id": record_id, "created_at": "external"}
        records.append(record)
    record.update(
        {
            "site_name": "External Writer",
            "username": username,
            "password_ciphertext": _protect_for_test(password),
            "password_scheme": "win32-dpapi-current-user",
            "source": "non-cooperating-test-writer",
            "updated_at": "external",
        }
    )
    data["updated_at"] = "external"
    write_json_atomic(path, data)


class _SimulatedCrash(BaseException):
    pass


class AccountSiteStoreTest(unittest.TestCase):
    def make_store(self, root: Path) -> AccountSiteStore:
        credential_path = root / "credential_store.local.json"
        site_path = root / "site_registry.json"
        vault_path = root / "credential_store.json"
        legacy_vault_path = root / "legacy_credential_store.json"
        if not credential_path.exists():
            write_json_atomic(
                credential_path,
                {
                    "version": 1,
                    "updated_at": "",
                    "accounts": [
                        {
                            "account_id": "smart-one",
                            "scope": "journal_system",
                            "provider": "Example Submit",
                            "username": "test-user",
                            "email": "",
                            "password": "legacy-test-password",
                            "login_url": "https://submit.example.test/login",
                            "status": "verified",
                            "notes": "compatibility fixture",
                            "migration_evidence": "must-survive-edits",
                        }
                    ],
                },
            )
        if not site_path.exists():
            write_json_atomic(site_path, {"version": 1, "updated_at": "", "systems": [], "journals": []})
        vault = DpapiCredentialVault(
            vault_path,
            protector=_protect_for_test,
            unprotector=_unprotect_for_test,
        )
        legacy_vault = DpapiCredentialVault(
            legacy_vault_path,
            protector=_protect_for_test,
            unprotector=_unprotect_for_test,
        )
        return AccountSiteStore(
            credential_path,
            site_path,
            vault_path=vault_path,
            legacy_vault_path=legacy_vault_path,
            runtime_vault_path=root / "smart_runtime_credential_store.json",
            smart_state_path=root / "smart_submission_state.json",
            unified_state_path=root / "unified_state.json",
            backup_dir=root / "backups",
            audit_path=root / "audit.json",
            acl_hardener=lambda _path: None,
            vault_backend=vault,
            legacy_vault_backend=legacy_vault,
        )

    def test_new_accounts_use_encrypted_vault_and_list_summaries_hide_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            secret = "new-test-password"
            saved = store.save_account(
                {
                    "account_id": "vault-one",
                    "scope": "email",
                    "provider": "Example Mail",
                    "login_url": "https://mail.example.test/",
                    "username": "mail-user",
                    "email": "mail@example.test",
                    "status": "verified",
                    "notes": "local fixture",
                },
                password=secret,
            )
            record_key = str(saved["record_key"])
            self.assertTrue(record_key.startswith("vault:"))
            self.assertEqual(store.reveal_password(record_key), secret)

            vault_text = store.vault_path.read_text(encoding="utf-8")
            audit_text = store.audit_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, vault_text)
            self.assertNotIn(secret, audit_text)
            summaries = store.list_account_summaries(source="vault")
            summary = next(item for item in summaries if item["record_key"] == record_key)
            self.assertEqual(summary["password_storage"], "dpapi")
            self.assertNotIn("password", summary)
            self.assertNotIn("password_ciphertext", summary)
            self.assertNotIn("password", store.get_account(record_key))

            edited = store.get_account(record_key)
            edited["notes"] = "metadata update"
            vault_before_edit = store.vault_path.read_bytes()
            store.save_account(edited, original_id=record_key)
            backups = list((root / "backups").rglob("*credential_store.json"))
            self.assertTrue(backups)
            self.assertEqual(store.read_backup_bytes(backups[-1]), vault_before_edit)
            self.assertNotIn(secret, backups[-1].read_text(encoding="utf-8"))
            self.assertEqual(store.reveal_password(record_key), secret)

    def test_smart_orcid_alias_reuses_existing_encrypted_secret_without_copying_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            secret = "orcid-test-password"
            source = store.save_account(
                {
                    "account_id": "orcid-primary",
                    "scope": "external",
                    "provider": "ORCID",
                    "login_url": "https://orcid.org/signin",
                    "username": "0000-0002-1825-0097",
                    "email": "author@example.test",
                    "status": "verified",
                },
                password=secret,
            )

            linked = store.create_smart_account_with_credential_reference(
                {
                    "account_id": "springer-via-orcid",
                    "scope": "journal_system",
                    "provider": "Springer Nature via ORCID",
                    "login_url": "https://submission.springernature.com/",
                    "username": "0000-0002-1825-0097",
                    "email": "author@example.test",
                    "system_id": "springer_nature",
                    "status": "registration_pending",
                    "auth_method": "orcid",
                },
                credential_record_key=str(source["record_key"]),
            )

            persisted = next(
                item
                for item in read_json(store.credential_path, {})["accounts"]
                if item.get("account_id") == "springer-via-orcid"
            )
            self.assertEqual(linked["auth_method"], "orcid")
            self.assertTrue(str(persisted["credential_ref"]).startswith("account-vault:"))
            self.assertNotIn("password", persisted)
            self.assertNotIn(secret, store.credential_path.read_text(encoding="utf-8"))
            self.assertEqual(
                store.reveal_password("smart:springer-via-orcid"),
                secret,
            )

    def test_smart_metadata_edit_preserves_plaintext_and_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            record = store.get_account("smart:smart-one")
            record["notes"] = "edited metadata"
            store.save_account(record, original_id="smart:smart-one")
            persisted = read_json(store.credential_path, {})["accounts"][0]
            self.assertEqual(persisted["password"], "legacy-test-password")
            self.assertEqual(persisted["migration_evidence"], "must-survive-edits")
            self.assertFalse(store.vault_path.exists())

    def test_scope_aliases_remain_editable_and_filter_as_one_category(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            data = read_json(store.credential_path, {})
            data["accounts"].append(
                {
                    "account_id": "external-one",
                    "scope": "external_service",
                    "provider": "Example External",
                    "username": "external-user",
                    "password": "external-password",
                }
            )
            write_json_atomic(store.credential_path, data)

            rows = store.list_account_summaries(scope="external")
            self.assertEqual([row["account_id"] for row in rows], ["external-one"])
            record = store.get_account("smart:external-one")
            record["notes"] = "still editable"
            store.save_account(record, original_id="smart:external-one")
            persisted = read_json(store.credential_path, {})["accounts"][1]
            self.assertEqual(persisted["scope"], "external_service")

    def test_smart_copy_encrypts_secret_then_removes_plaintext(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            before = store.credential_path.read_bytes()
            summaries = store.list_account_summaries(source="pending")
            self.assertEqual(len(summaries), 1)
            self.assertEqual(store.credential_path.read_bytes(), before)
            self.assertFalse(store.vault_path.exists())
            migrated = store.copy_smart_account_to_vault("smart:smart-one")
            persisted = read_json(store.credential_path, {})["accounts"][0]
            self.assertNotIn("password", persisted)
            self.assertTrue(
                str(persisted.get("credential_ref") or "").startswith(
                    "account-vault:"
                )
            )
            self.assertTrue(store.vault_path.exists())
            self.assertNotIn(
                "legacy-test-password",
                store.vault_path.read_text(encoding="utf-8"),
            )
            self.assertNotIn(
                "legacy-test-password",
                store.credential_path.read_text(encoding="utf-8"),
            )
            self.assertEqual(
                store.reveal_password("smart:smart-one"),
                "legacy-test-password",
            )
            self.assertEqual(migrated["password_storage"], "dpapi_account_vault")
            self.assertTrue(migrated["auto_fill_ready"])

    def test_smart_source_audit_failures_roll_back_save_and_delete_exactly(self) -> None:
        for operation in ("save", "delete"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store = self.make_store(root)
                source_before = store.credential_path.read_bytes()
                audit_before = store.audit_path.read_bytes() if store.audit_path.exists() else b""
                original_append = store._append_audit

                def write_then_fail(audit: dict[str, object], **kwargs: object) -> None:
                    original_append(audit, **kwargs)
                    raise RuntimeError("smart source audit fault")

                store._append_audit = write_then_fail
                try:
                    with self.assertRaisesRegex(RegistryValidationError, "已自动回滚"):
                        if operation == "save":
                            record = store.get_account("smart:smart-one")
                            record["notes"] = "must-not-persist"
                            store.save_account(record, original_id="smart:smart-one")
                        else:
                            store.delete_account("smart:smart-one")
                finally:
                    store._append_audit = original_append

                self.assertEqual(store.credential_path.read_bytes(), source_before)
                if audit_before:
                    self.assertEqual(store.audit_path.read_bytes(), audit_before)
                else:
                    self.assertFalse(store.audit_path.exists())
                self.assertEqual(list((root / "vault_transactions").glob("*.*.json")), [])

    def test_smart_account_and_site_references_block_renames_and_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            system = store.save_system(
                {
                    "system_id": "protected-system",
                    "label": "Protected System",
                    "status": "verified",
                }
            )
            vault_account = store.save_account(
                {
                    "account_id": "protected-vault-account",
                    "scope": "journal_system",
                    "provider": "Protected Vault",
                    "username": "protected-vault-user",
                },
                password="protected-vault-secret",
            )
            smart_state = {
                "paper_queue": [],
                "online_queue": ["paper-vault"],
                "papers": {
                    "paper-route": {
                        "status": "inactive",
                        "workflow_status": "inactive",
                        "credential_account_id": "smart-one",
                        "submission_system_id": "protected-system",
                        "target_journal": "Protected Journal",
                    },
                    "paper-vault": {
                        "status": "queued",
                        "workflow_status": "queued",
                        "credential_account_id": "protected-vault-account",
                        "submission_system_id": "protected-system",
                        "target_journal": "Protected Journal",
                    },
                },
            }
            write_json_atomic(store.smart_state_path, smart_state)
            write_json_atomic(
                store.unified_state_path,
                {
                    "submission_routes": {
                        "paper-route": {
                            "execution_owner": "smart",
                            "mode": "canary",
                            "revision": 1,
                        }
                    }
                },
            )

            account = store.get_account("smart:smart-one")
            account["account_id"] = "renamed-smart-one"
            with self.assertRaisesRegex(RegistryValidationError, "投稿任务或投稿路线引用"):
                store.save_account(account, original_id="smart:smart-one")
            with self.assertRaisesRegex(RegistryValidationError, "投稿任务或投稿路线引用"):
                store.delete_account("smart:smart-one")
            with self.assertRaisesRegex(RegistryValidationError, "投稿任务或投稿路线引用"):
                store.delete_account(str(vault_account["record_key"]))
            with self.assertRaisesRegex(RegistryValidationError, "投稿任务或投稿路线引用"):
                store.delete_system(system["system_id"])

            journal = store.save_journal(
                {
                    "journal_key": "protected-journal",
                    "journal_name": "Protected Journal",
                    "system_id": system["system_id"],
                    "status": "verified",
                }
            )
            changed_journal = dict(journal)
            changed_journal["journal_name"] = "Broken Binding"
            with self.assertRaisesRegex(RegistryValidationError, "不能更改绑定"):
                store.save_journal(changed_journal, original_id=journal["journal_key"])
            with self.assertRaisesRegex(RegistryValidationError, "投稿任务或投稿路线引用"):
                store.delete_journal(journal["journal_key"])

            write_json_atomic(store.smart_state_path, {"papers": {}, "paper_queue": [], "online_queue": []})
            write_json_atomic(store.unified_state_path, {"submission_routes": {}})
            account = store.get_account("smart:smart-one")
            account["account_id"] = "renamed-smart-one"
            store.save_account(account, original_id="smart:smart-one")
            store.delete_account("smart:renamed-smart-one")
            store.delete_account(str(vault_account["record_key"]))
            store.delete_journal(journal["journal_key"])
            store.delete_system(system["system_id"])

    def test_concurrent_smart_metadata_saves_preserve_secret_and_valid_json(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_store = self.make_store(root)
            second_store = self.make_store(root)
            first_record = first_store.get_account("smart:smart-one")
            second_record = second_store.get_account("smart:smart-one")
            barrier = threading.Barrier(2)

            def save_with_retry(
                store: AccountSiteStore,
                record: dict[str, object],
                note: str,
            ) -> str:
                record["notes"] = note
                barrier.wait()
                last_error = ""
                for _attempt in range(500):
                    try:
                        store.save_account(record, original_id="smart:smart-one")
                        return note
                    except RuntimeError as exc:
                        last_error = str(exc)
                        time.sleep(0.01)
                self.fail(f"shared Smart source lock did not become available: {last_error}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = [
                    future.result()
                    for future in (
                        executor.submit(save_with_retry, first_store, first_record, "first-note"),
                        executor.submit(save_with_retry, second_store, second_record, "second-note"),
                    )
                ]
            self.assertEqual(set(results), {"first-note", "second-note"})
            persisted = read_json(first_store.credential_path, {})["accounts"]
            self.assertEqual(len(persisted), 1)
            self.assertIn(persisted[0]["notes"], {"first-note", "second-note"})
            self.assertEqual(persisted[0]["password"], "legacy-test-password")
            self.assertEqual(persisted[0]["migration_evidence"], "must-survive-edits")
            self.assertNotIn("legacy-test-password", first_store.audit_path.read_text(encoding="utf-8"))
            self.assertEqual(list((root / "vault_transactions").glob("*.pending.json")), [])

    def test_corrupt_json_and_corrupt_audit_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.credential_path.write_text("{broken", encoding="utf-8")
            original = store.credential_path.read_bytes()
            with self.assertRaises(JsonReadError):
                store.list_account_summaries()
            with self.assertRaises(JsonReadError):
                store.save_account(
                    {"account_id": "x", "scope": "journal_system"},
                    original_id="smart:smart-one",
                )
            self.assertEqual(store.credential_path.read_bytes(), original)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            original = store.credential_path.read_bytes()
            store.audit_path.write_text("not-json", encoding="utf-8")
            record = store.get_account("smart:smart-one")
            record["notes"] = "must not persist"
            with self.assertRaises(RegistryValidationError):
                store.save_account(record, original_id="smart:smart-one")
            self.assertEqual(store.credential_path.read_bytes(), original)

    def test_system_and_journal_crud_backup_and_referential_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.save_system(
                {
                    "system_id": "example_system",
                    "label": "Example System",
                    "status": "active",
                    "login_url": "https://example.test/login",
                    "submission_url": "https://example.test/submit",
                    "email_senders": ["notifications@example.test"],
                    "adapter_evidence": "preserve-me",
                }
            )
            store.save_journal(
                {
                    "journal_key": "example_journal",
                    "journal_name": "Example Journal",
                    "system_id": "example_system",
                    "status": "active",
                    "submission_url": "https://example.test/submit/journal",
                    "linked_papers": ["paper-1"],
                }
            )
            with self.assertRaises(RegistryValidationError):
                store.delete_system("example_system")

            system = store.get_system("example_system")
            system["notes"] = "edited"
            store.save_system(system, original_id="example_system")
            self.assertEqual(store.get_system("example_system")["adapter_evidence"], "preserve-me")
            self.assertTrue(list((root / "backups").rglob("*site_registry.json")))

            with self.assertRaisesRegex(RegistryValidationError, "论文记录引用"):
                store.delete_journal("example_journal")
            journal = store.get_journal("example_journal")
            journal["linked_papers"] = []
            store.save_journal(journal, original_id="example_journal")
            store.delete_journal("example_journal")
            store.delete_system("example_system")
            self.assertEqual(store.list_systems(), [])
            self.assertEqual(store.list_journals(), [])

    def test_system_and_journal_ids_are_generated_and_stable_after_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            system = store.save_system(
                {
                    "label": "Generated System",
                    "status": "active",
                    "login_url": "https://generated.example.test/login",
                }
            )
            self.assertRegex(system["system_id"], r"^system-[0-9a-f]{16}$")
            journal = store.save_journal(
                {
                    "journal_name": "Generated Journal",
                    "system_id": system["system_id"],
                    "status": "active",
                }
            )
            self.assertRegex(journal["journal_key"], r"^journal-[0-9a-f]{16}$")

            system_id = system["system_id"]
            journal_key = journal["journal_key"]
            system["notes"] = "edited"
            journal["source"] = "edited"
            self.assertEqual(store.save_system(system, original_id=system_id)["system_id"], system_id)
            self.assertEqual(store.save_journal(journal, original_id=journal_key)["journal_key"], journal_key)

            changed_system = dict(system)
            changed_system["system_id"] = "changed-system-id"
            with self.assertRaisesRegex(RegistryValidationError, "内部标识只读"):
                store.save_system(changed_system, original_id=system_id)
            changed_journal = dict(journal)
            changed_journal["journal_key"] = "changed-journal-id"
            with self.assertRaisesRegex(RegistryValidationError, "内部标识只读"):
                store.save_journal(changed_journal, original_id=journal_key)
            orphaned_journal = dict(journal)
            orphaned_journal["system_id"] = "missing-system"
            with self.assertRaisesRegex(RegistryValidationError, "投稿系统不存在"):
                store.save_journal(orphaned_journal, original_id=journal_key)

    def test_writable_and_legacy_vaults_are_displayed_together_without_mutating_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            legacy_path = root / "legacy_credential_store.json"
            store.legacy_vault = DpapiCredentialVault(
                legacy_path,
                protector=_protect_for_test,
                unprotector=_unprotect_for_test,
            )
            store.legacy_vault.upsert(
                {
                    "id": "legacy-one",
                    "site_name": "Legacy System",
                    "site_url": "https://legacy.example.test/login",
                    "username": "legacy-user",
                    "login_status": "verified",
                },
                password="legacy-secret",
            )
            legacy_before = legacy_path.read_bytes()

            saved = store.save_account(
                {
                    "scope": "journal_system",
                    "provider": "New System",
                    "login_url": "https://new.example.test/login",
                    "username": "new-user",
                },
                password="new-secret",
            )
            rows = store.list_account_summaries()
            self.assertIn("legacy:legacy-one", {row["record_key"] for row in rows})
            self.assertIn(saved["record_key"], {row["record_key"] for row in rows})
            self.assertTrue(store.is_account_read_only("legacy:legacy-one"))
            self.assertFalse(store.is_account_read_only(saved["record_key"]))
            legacy_record = store.get_account("legacy:legacy-one")
            with self.assertRaisesRegex(RegistryValidationError, "旧稳定投稿加密账号库当前为只读"):
                store.save_account(legacy_record, original_id="legacy:legacy-one")
            with self.assertRaisesRegex(RegistryValidationError, "旧稳定投稿加密账号库当前为只读"):
                store.delete_account("legacy:legacy-one")
            self.assertEqual(legacy_path.read_bytes(), legacy_before)

    def test_duplicate_report_exposes_only_record_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.vault.upsert(
                {
                    "id": "vault-match",
                    "site_name": "Example Submit",
                    "site_url": "https://submit.example.test/login",
                    "username": "test-user",
                },
                password="encrypted-copy",
            )
            report = store.duplicate_report()
            self.assertEqual(report["accounts"], [["Smart:smart-one", "新加密库:vault-match"]])
            serialized = json.dumps(report, ensure_ascii=False)
            self.assertNotIn("legacy-test-password", serialized)
            self.assertNotIn("encrypted-copy", serialized)

    def test_current_vault_path_collisions_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            writable = self.make_store(root)
            current = writable.vault_path
            legacy = writable.legacy_vault_path
            runtime = root / "runtime-vault.json"
            credentials_before = writable.credential_path.read_bytes()

            with self.assertRaisesRegex(RegistryValidationError, "旧稳定投稿账号库"):
                AccountSiteStore(
                    writable.credential_path,
                    writable.site_registry_path,
                    vault_path=current,
                    legacy_vault_path=current,
                    runtime_vault_path=runtime,
                    acl_hardener=lambda _path: None,
                )
            with self.assertRaisesRegex(RegistryValidationError, "智能投稿运行凭据库"):
                AccountSiteStore(
                    writable.credential_path,
                    writable.site_registry_path,
                    vault_path=current,
                    legacy_vault_path=legacy,
                    runtime_vault_path=current,
                    acl_hardener=lambda _path: None,
                )

            self.assertEqual(writable.credential_path.read_bytes(), credentials_before)
            self.assertFalse(current.exists())

    def test_vault_id_normalization_rejects_duplicates_and_edit_keeps_password(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            secret = "original-id-secret"
            saved = store.save_account(
                {
                    "account_id": "Case ID",
                    "scope": "email",
                    "provider": "ID Test Mail",
                    "username": "id-test-user",
                },
                password=secret,
            )
            record_key = str(saved["record_key"])

            for duplicate_id in ("Case ID", "case id", "Ｃａｓｅ　ＩＤ", "Case\u200bID"):
                before = store.vault_path.read_bytes()
                with self.subTest(duplicate_id=duplicate_id):
                    with self.assertRaises(DuplicateRecordError):
                        store.save_account(
                            {
                                "account_id": duplicate_id,
                                "scope": "email",
                                "provider": "Duplicate Mail",
                                "username": "duplicate-user",
                            },
                            password="must-not-overwrite",
                        )
                    self.assertEqual(store.vault_path.read_bytes(), before)
                    self.assertEqual(store.vault.reveal("Case ID"), secret)

            edited = store.get_account(record_key)
            edited["account_id"] = "case id"
            before_read_only_edit = store.vault_path.read_bytes()
            with self.assertRaisesRegex(RegistryValidationError, "内部标识只读"):
                store.save_account(edited, original_id=record_key)
            self.assertEqual(store.vault_path.read_bytes(), before_read_only_edit)

            edited["account_id"] = "Case ID"
            edited["notes"] = "metadata-only edit"
            store.save_account(edited, original_id=record_key)
            self.assertEqual(store.vault.reveal("Case ID"), secret)

    def test_new_vault_ids_reject_reserved_source_prefix_variants(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = self.make_store(Path(temp_dir))
            for account_id in (
                "vault:plain-id",
                " LEGACY : plain-id ",
                "ＳＭＡＲＴ：plain-id",
                "v\u200ba u l t : plain-id",
            ):
                with self.subTest(account_id=account_id):
                    with self.assertRaisesRegex(RegistryValidationError, "保留来源前缀"):
                        store.save_account(
                            {
                                "account_id": account_id,
                                "scope": "email",
                                "provider": "Reserved Prefix",
                                "username": "reserved-user",
                            },
                            password="reserved-secret",
                        )

    def test_existing_prefixed_raw_id_routes_exactly_when_plain_id_coexists(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            store.vault.upsert(
                {
                    "id": "plain-id",
                    "site_name": "Plain Account",
                    "username": "plain-user",
                },
                password="plain-secret",
            )
            store.vault.upsert(
                {
                    "id": "vault:plain-id",
                    "site_name": "Prefixed Account",
                    "username": "prefixed-user",
                },
                password="prefixed-secret",
            )

            plain_key = "vault:plain-id"
            prefixed_key = "vault:vault:plain-id"
            self.assertEqual(store.get_account(plain_key)["provider"], "Plain Account")
            self.assertEqual(store.get_account(prefixed_key)["provider"], "Prefixed Account")
            self.assertEqual(store.reveal_password(plain_key), "plain-secret")
            self.assertEqual(store.reveal_password(prefixed_key), "prefixed-secret")

            prefixed = store.get_account(prefixed_key)
            prefixed["notes"] = "prefixed-only-update"
            store.save_account(prefixed, original_id=prefixed_key)
            self.assertEqual(store.vault.get_record_by_id("plain-id").get("notes", ""), "")
            self.assertEqual(
                store.vault.get_record_by_id("vault:plain-id")["notes"],
                "prefixed-only-update",
            )
            self.assertEqual(store.vault.reveal_by_id("plain-id"), "plain-secret")
            self.assertEqual(store.vault.reveal_by_id("vault:plain-id"), "prefixed-secret")

            store.delete_account(prefixed_key)
            self.assertEqual(store.vault.reveal_by_id("plain-id"), "plain-secret")
            self.assertFalse(
                any(record["id"] == "vault:plain-id" for record in store.vault.list_records())
            )

    def test_concurrent_normalized_duplicate_create_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_store = self.make_store(root)
            second_store = self.make_store(root)
            barrier = threading.Barrier(2)

            def create_with_retry(
                store: AccountSiteStore,
                account_id: str,
                password: str,
            ) -> tuple[str, str]:
                barrier.wait()
                for _attempt in range(100):
                    try:
                        saved = store.save_account(
                            {
                                "account_id": account_id,
                                "scope": "journal_system",
                                "provider": "Concurrent Submit",
                                "username": "concurrent-user",
                            },
                            password=password,
                        )
                        return "saved", str(saved["record_key"])
                    except DuplicateRecordError:
                        return "duplicate", ""
                    except RuntimeError:
                        # FileLock intentionally fails fast.  Retrying here lets
                        # this test verify the uniqueness decision after the
                        # competing transaction releases the shared lock.
                        time.sleep(0.005)
                self.fail("shared account lock did not become available")

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                futures = (
                    executor.submit(create_with_retry, first_store, "Concurrent ID", "secret-one"),
                    executor.submit(create_with_retry, second_store, "ｃｏｎｃｕｒｒｅｎｔ　ｉｄ", "secret-two"),
                )
                results = [future.result() for future in futures]

            self.assertEqual(sorted(result[0] for result in results), ["duplicate", "saved"])
            records = first_store.vault.list_records()
            self.assertEqual(len(records), 1)
            winner_id = str(records[0]["id"])
            self.assertIn(first_store.vault.reveal(winner_id), {"secret-one", "secret-two"})

    def test_reveal_password_audits_success_and_failure_without_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            account_id = "audit-account-value"
            username = "audit-user-value"
            secret = "audit-password-value"
            saved = store.save_account(
                {
                    "account_id": account_id,
                    "scope": "email",
                    "provider": "Audit Mail",
                    "username": username,
                },
                password=secret,
            )

            self.assertEqual(store.reveal_password(str(saved["record_key"])), secret)
            audit = read_json(store.audit_path, {})
            success = audit["events"][-1]
            self.assertEqual(success["action"], "reveal-password-success")
            self.assertEqual(success["record_type"], "account-secret-vault")
            self.assertRegex(success["record_id"], r"^[0-9a-f]{24}$")

            backend_error_value = "backend-secret-must-not-escape"
            original_reveal = store.vault.reveal_by_id

            def leaking_backend(_record_id: str) -> str:
                raise RuntimeError(backend_error_value)

            store.vault.reveal_by_id = leaking_backend
            try:
                with self.assertRaises(RegistryValidationError) as captured:
                    store.reveal_password(str(saved["record_key"]))
            finally:
                store.vault.reveal_by_id = original_reveal

            serialized = store.audit_path.read_text(encoding="utf-8")
            for forbidden in (account_id, username, secret, backend_error_value):
                self.assertNotIn(forbidden, serialized)
                self.assertNotIn(forbidden, str(captured.exception))
            failure = read_json(store.audit_path, {})["events"][-1]
            self.assertEqual(failure["action"], "reveal-password-failure")
            self.assertEqual(failure["record_id"], success["record_id"])

    def test_audit_write_failure_restores_exact_vault_and_audit_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            secret = "rollback-secret"
            saved = store.save_account(
                {
                    "account_id": "rollback-account",
                    "scope": "journal_system",
                    "provider": "Rollback Submit",
                    "username": "rollback-user",
                },
                password=secret,
            )
            vault_before = store.vault_path.read_bytes()
            audit_before = store.audit_path.read_bytes()
            edited = store.get_account(str(saved["record_key"]))
            edited["notes"] = "must roll back"
            original_append = store._append_audit

            def write_then_fail(audit: dict[str, object], **kwargs: object) -> None:
                original_append(audit, **kwargs)
                raise RuntimeError("simulated audit failure")

            store._append_audit = write_then_fail
            try:
                with self.assertRaisesRegex(RegistryValidationError, "已自动回滚"):
                    store.save_account(edited, original_id=str(saved["record_key"]))
            finally:
                store._append_audit = original_append

            self.assertEqual(store.vault_path.read_bytes(), vault_before)
            self.assertEqual(store.audit_path.read_bytes(), audit_before)
            self.assertEqual(store.vault.reveal("rollback-account"), secret)
            self.assertEqual(list((root / "vault_transactions").glob("*.pending.json")), [])

    def test_audit_commit_is_bound_to_exact_target_post_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            saved = store.save_account(
                {
                    "account_id": "hash-bound-account",
                    "scope": "journal_system",
                    "provider": "Hash Bound Submit",
                    "username": "hash-bound-user",
                },
                password="hash-bound-secret",
            )

            event = read_json(store.audit_path, {})["events"][-1]
            target_post_sha256 = hashlib.sha256(store.vault_path.read_bytes()).hexdigest()
            self.assertEqual(event["target_post_sha256"], target_post_sha256)
            self.assertRegex(event["operation_id"], r"^[0-9a-f]{32}$")
            self.assertEqual(str(saved["record_key"]), "vault:hash-bound-account")
            committed = list((root / "vault_transactions").glob("*.committed.json"))
            self.assertEqual(len(committed), 1)
            marker = read_json(committed[0], {})
            self.assertEqual(marker["operation_id"], event["operation_id"])
            self.assertEqual(marker["target_post_sha256"], target_post_sha256)
            self.assertEqual(
                marker["audit_post_sha256"],
                hashlib.sha256(store.audit_path.read_bytes()).hexdigest(),
            )
            self.assertTrue(Path(marker["post_image_path"]).exists())

    def test_success_path_never_calls_destructive_pending_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)

            def forbidden_cleanup(*_args: object, **_kwargs: object) -> None:
                raise AssertionError("success path must linearize by marker rename")

            store._remove_transaction_artifacts = forbidden_cleanup
            saved = store.save_account(
                {
                    "account_id": "no-cleanup-success",
                    "scope": "email",
                    "provider": "No Cleanup Mail",
                    "username": "no-cleanup-user",
                },
                password="no-cleanup-secret",
            )
            self.assertEqual(saved["record_key"], "vault:no-cleanup-success")
            self.assertEqual(
                len(list((root / "vault_transactions").glob("*.committed.json"))),
                1,
            )
            self.assertEqual(
                len(list((root / "vault_transactions").glob("*.post.wal.json"))),
                1,
            )

    def test_third_party_target_overwrites_during_audit_never_report_success(self) -> None:
        fault_points = (
            "before-audit",
            "inside-audit",
            "after-audit",
            "after-audit-written",
        )
        for fault_point in fault_points:
            with self.subTest(fault_point=fault_point), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store = self.make_store(root)
                saved = store.save_account(
                    {
                        "account_id": "audit-cas-account",
                        "scope": "journal_system",
                        "provider": "Audit CAS Submit",
                        "username": "audit-cas-user",
                    },
                    password="audit-cas-original-secret",
                )
                audit_before = store.audit_path.read_bytes()
                edited = store.get_account(str(saved["record_key"]))
                edited["notes"] = f"must fail at {fault_point}"
                external_id = f"external-{fault_point}"
                external_bytes = b""

                def overwrite_target() -> None:
                    nonlocal external_bytes
                    _raw_vault_upsert(
                        store.vault_path,
                        record_id=external_id,
                        username="external-audit-writer",
                        password="external-audit-secret",
                    )
                    external_bytes = store.vault_path.read_bytes()

                original_append = store._append_audit
                original_write_pending = store._write_vault_pending

                if fault_point == "before-audit":
                    def inject_before_audit(path: Path, journal: dict[str, object]) -> None:
                        original_write_pending(path, journal)
                        if journal.get("stage") == "target_written" and not external_bytes:
                            overwrite_target()

                    store._write_vault_pending = inject_before_audit
                elif fault_point == "inside-audit":
                    def inject_inside_audit(*args: object, **kwargs: object) -> None:
                        overwrite_target()
                        original_append(*args, **kwargs)

                    store._append_audit = inject_inside_audit
                elif fault_point == "after-audit":
                    def inject_after_audit(*args: object, **kwargs: object) -> None:
                        original_append(*args, **kwargs)
                        overwrite_target()

                    store._append_audit = inject_after_audit
                else:
                    def inject_after_audit_written(
                        path: Path,
                        journal: dict[str, object],
                    ) -> None:
                        original_write_pending(path, journal)
                        if journal.get("stage") == "audit_written" and not external_bytes:
                            overwrite_target()

                    store._write_vault_pending = inject_after_audit_written

                try:
                    with self.assertRaises(RegistryValidationError):
                        store.save_account(
                            edited,
                            original_id=str(saved["record_key"]),
                            password="audit-cas-new-secret",
                        )
                finally:
                    store._append_audit = original_append
                    store._write_vault_pending = original_write_pending

                self.assertTrue(external_bytes)
                self.assertEqual(store.vault_path.read_bytes(), external_bytes)
                self.assertEqual(store.audit_path.read_bytes(), audit_before)
                pending = list((root / "vault_transactions").glob("*.pending.json"))
                self.assertEqual(len(pending), 1)
                journal = read_json(pending[0], {})
                self.assertEqual(
                    journal["audit_intent"]["target_post_sha256"],
                    journal["target_post_sha256"],
                )
                self.assertTrue(Path(journal["post_image_path"]).exists())
                self.assertTrue(Path(journal["audit_pre_image_path"]).exists())

                with self.assertRaises(RegistryValidationError):
                    self.make_store(root)
                self.assertEqual(store.vault_path.read_bytes(), external_bytes)
                self.assertEqual(store.audit_path.read_bytes(), audit_before)
                self.assertTrue(pending[0].exists())

    def test_prepared_crash_with_unchanged_target_recovers_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            def crash_before_target(*_args: object, **_kwargs: object) -> None:
                raise _SimulatedCrash()

            with patch.object(account_store_module, "write_bytes_atomic", crash_before_target):
                with self.assertRaises(_SimulatedCrash):
                    store.save_account(
                        {
                            "account_id": "prepared-crash",
                            "scope": "email",
                            "provider": "Prepared Mail",
                            "username": "prepared-user",
                        },
                        password="prepared-secret",
                    )

            pending = list((root / "vault_transactions").glob("*.pending.json"))
            self.assertEqual(len(pending), 1)
            self.assertEqual(read_json(pending[0], {})["stage"], "prepared")
            recovered = self.make_store(root)
            self.assertFalse(recovered.vault_path.exists())
            self.assertEqual(list((root / "vault_transactions").glob("*.pending.json")), [])
            self.make_store(root)

    def test_target_written_crash_rolls_back_from_encrypted_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            saved = store.save_account(
                {
                    "account_id": "target-crash",
                    "scope": "journal_system",
                    "provider": "Target Submit",
                    "username": "target-user",
                },
                password="target-original-secret",
            )
            vault_before = store.vault_path.read_bytes()
            audit_before = store.audit_path.read_bytes()
            edited = store.get_account(str(saved["record_key"]))
            edited["notes"] = "crashed update"

            def crash_before_audit(*_args: object, **_kwargs: object) -> None:
                raise _SimulatedCrash()

            store._append_audit = crash_before_audit
            with self.assertRaises(_SimulatedCrash):
                store.save_account(
                    edited,
                    original_id=str(saved["record_key"]),
                    password="target-new-secret",
                )

            pending = list((root / "vault_transactions").glob("*.pending.json"))
            self.assertEqual(len(pending), 1)
            journal = read_json(pending[0], {})
            self.assertEqual(journal["stage"], "target_written")
            self.assertEqual(journal["target_pre_sha256"], hashlib.sha256(vault_before).hexdigest())
            self.assertEqual(journal["audit_pre_sha256"], hashlib.sha256(audit_before).hexdigest())
            self.assertNotEqual(store.vault_path.read_bytes(), vault_before)
            self.assertEqual(
                journal["target_post_sha256"],
                hashlib.sha256(store.vault_path.read_bytes()).hexdigest(),
            )
            pending_text = pending[0].read_text(encoding="utf-8")
            backup_text = Path(journal["backup_path"]).read_text(encoding="utf-8")
            for forbidden in (
                "target-crash",
                "target-user",
                "target-original-secret",
                "target-new-secret",
            ):
                self.assertNotIn(forbidden, pending_text)
                self.assertNotIn(forbidden, backup_text)
            self.assertRegex(journal["audit_intent"]["record_id"], r"^[0-9a-f]{24}$")

            recovered = self.make_store(root)
            self.assertEqual(recovered.vault_path.read_bytes(), vault_before)
            self.assertEqual(recovered.audit_path.read_bytes(), audit_before)
            self.assertEqual(recovered.vault.reveal("target-crash"), "target-original-secret")
            self.assertEqual(list((root / "vault_transactions").glob("*.pending.json")), [])

    def test_crash_after_atomic_marker_is_committed_and_reconciled_later(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            original_commit = store._commit_vault_pending_locked

            def crash_after_marker(*args: object, **kwargs: object) -> None:
                original_commit(*args, **kwargs)
                raise _SimulatedCrash()

            store._commit_vault_pending_locked = crash_after_marker
            with self.assertRaises(_SimulatedCrash):
                store.save_account(
                    {
                        "account_id": "committed-crash",
                        "scope": "email",
                        "provider": "Committed Mail",
                        "username": "committed-user",
                    },
                    password="committed-secret",
                )

            pending = list((root / "vault_transactions").glob("*.pending.json"))
            committed = list((root / "vault_transactions").glob("*.committed.json"))
            self.assertEqual(pending, [])
            self.assertEqual(len(committed), 1)
            journal = read_json(committed[0], {})
            self.assertEqual(journal["stage"], "audit_written")
            vault_committed = store.vault_path.read_bytes()
            audit_committed = store.audit_path.read_bytes()

            recovered = self.make_store(root)
            self.assertEqual(recovered.vault_path.read_bytes(), vault_committed)
            self.assertEqual(recovered.audit_path.read_bytes(), audit_committed)
            self.assertEqual(recovered.vault.reveal("committed-crash"), "committed-secret")
            self.assertEqual(list((root / "vault_transactions").glob("*.pending.json")), [])
            self.assertTrue(committed[0].exists())
            self.assertTrue(Path(journal["post_image_path"]).exists())

            self.assertEqual(recovered.reveal_password("vault:committed-crash"), "committed-secret")
            self.assertFalse(committed[0].exists())
            self.assertEqual(
                len(list((root / "vault_transactions").glob("*.reconciled.json"))),
                1,
            )
            self.assertFalse(Path(journal["post_image_path"]).exists())

    def test_crash_after_audit_append_before_marker_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            original_write_pending = store._write_vault_pending
            writes = 0

            def crash_on_third_pending_write(path: Path, journal: dict[str, object]) -> None:
                nonlocal writes
                writes += 1
                if writes == 3:
                    raise _SimulatedCrash()
                original_write_pending(path, journal)

            store._write_vault_pending = crash_on_third_pending_write
            with self.assertRaises(_SimulatedCrash):
                store.save_account(
                    {
                        "account_id": "audit-window-crash",
                        "scope": "external",
                        "provider": "Audit Window",
                        "username": "window-user",
                    },
                    password="window-secret",
                )

            pending = list((root / "vault_transactions").glob("*.pending.json"))
            self.assertEqual(len(pending), 1)
            self.assertEqual(read_json(pending[0], {})["stage"], "target_written")
            recovered = self.make_store(root)
            self.assertFalse(recovered.vault_path.exists())
            self.assertFalse(recovered.audit_path.exists())
            self.assertEqual(list((root / "vault_transactions").glob("*.pending.json")), [])
            self.assertEqual(list((root / "vault_transactions").glob("*.committed.json")), [])

    def test_real_subprocess_instruction_crashes_recover_after_stale_lock(self) -> None:
        crash_script = textwrap.dedent(
            r"""
            import base64
            import os
            import sys
            from pathlib import Path

            import unified_paper_orchestrator.account_site_store as store_module
            from unified_paper_orchestrator.account_site_store import AccountSiteStore
            from unified_paper_orchestrator.credential_vault import DpapiCredentialVault

            def protect(value: str) -> str:
                return base64.b64encode(value.encode("utf-8")[::-1]).decode("ascii")

            def unprotect(value: str) -> str:
                return base64.b64decode(value.encode("ascii"))[::-1].decode("utf-8")

            root = Path(sys.argv[1])
            point = sys.argv[2]
            target_kind = sys.argv[3]
            vault_path = root / "credential_store.json"
            legacy_path = root / "legacy_credential_store.json"
            store = AccountSiteStore(
                root / "credential_store.local.json",
                root / "site_registry.json",
                vault_path=vault_path,
                legacy_vault_path=legacy_path,
                runtime_vault_path=root / "smart_runtime_credential_store.json",
                smart_state_path=root / "smart_submission_state.json",
                unified_state_path=root / "unified_state.json",
                backup_dir=root / "backups",
                audit_path=root / "audit.json",
                acl_hardener=lambda _path: None,
                vault_backend=DpapiCredentialVault(vault_path, protector=protect, unprotector=unprotect),
                legacy_vault_backend=DpapiCredentialVault(
                    legacy_path,
                    protector=protect,
                    unprotector=unprotect,
                ),
            )

            if point == "before_target":
                def crash_before_target(_path, _data):
                    os._exit(91)
                store_module.write_bytes_atomic = crash_before_target
            elif point == "after_target":
                original_write = store_module.write_bytes_atomic
                def crash_after_target(path, data):
                    original_write(path, data)
                    os._exit(92)
                store_module.write_bytes_atomic = crash_after_target
            elif point == "before_audit":
                def crash_before_audit(*_args, **_kwargs):
                    os._exit(93)
                store._append_audit = crash_before_audit
            elif point == "after_audit":
                original_append = store._append_audit
                def crash_after_audit(*args, **kwargs):
                    original_append(*args, **kwargs)
                    os._exit(94)
                store._append_audit = crash_after_audit
            elif point == "after_marker":
                original_commit = store._commit_vault_pending_locked
                def crash_after_marker(*args, **kwargs):
                    original_commit(*args, **kwargs)
                    os._exit(95)
                store._commit_vault_pending_locked = crash_after_marker

            if target_kind == "smart":
                record = store.get_account("smart:smart-one")
                record["notes"] = "smart-post-crash-note"
                store.save_account(record, original_id="smart:smart-one")
            else:
                record = store.get_account("vault:process-crash-account")
                record["notes"] = "vault-post-crash-note"
                store.save_account(
                    record,
                    original_id="vault:process-crash-account",
                    password="process-post-secret",
                )
            raise SystemExit(0)
            """
        )
        cases = (
            ("before_target", 91, False, "vault", "prepared", "pending"),
            ("after_target", 92, False, "vault", "prepared", "pending"),
            ("before_audit", 93, False, "vault", "target_written", "pending"),
            ("after_audit", 94, False, "vault", "target_written", "pending"),
            ("after_marker", 95, True, "vault", "audit_written", "committed"),
            ("after_target", 92, False, "smart", "prepared", "pending"),
        )
        for point, exit_code, committed, target_kind, expected_stage, marker_kind in cases:
            with self.subTest(point=point, target_kind=target_kind), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store = self.make_store(root)
                if target_kind == "vault":
                    store.save_account(
                        {
                            "account_id": "process-crash-account",
                            "scope": "journal_system",
                            "provider": "Process Crash Submit",
                            "username": "process-crash-user",
                        },
                        password="process-pre-secret",
                    )
                    target_path = store.vault_path
                else:
                    target_path = store.credential_path
                target_before = target_path.read_bytes()
                audit_before = store.audit_path.read_bytes() if store.audit_path.exists() else b""

                completed = subprocess.run(
                    [sys.executable, "-c", crash_script, str(root), point, target_kind],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(completed.returncode, exit_code, completed.stderr)
                self.assertNotIn("process-post-secret", completed.stdout + completed.stderr)
                markers = list((root / "vault_transactions").glob(f"*.{marker_kind}.json"))
                self.assertEqual(len(markers), 1)
                journal = read_json(markers[0], {})
                self.assertEqual(journal["stage"], expected_stage)
                self.assertTrue(Path(journal["post_image_path"]).exists())
                self.assertRegex(journal["target_post_sha256"], r"^[0-9a-f]{64}$")
                transaction_text = markers[0].read_text(encoding="utf-8") + Path(
                    journal["post_image_path"]
                ).read_text(encoding="utf-8")
                for forbidden in (
                    "process-crash-account",
                    "process-post-secret",
                    "legacy-test-password",
                    "smart-post-crash-note",
                ):
                    self.assertNotIn(forbidden, transaction_text)

                self.assertTrue(store.lock_path.exists())
                os.utime(store.lock_path, (1, 1))
                recovered = self.make_store(root)
                if committed:
                    self.assertNotEqual(target_path.read_bytes(), target_before)
                    self.assertNotEqual(recovered.audit_path.read_bytes(), audit_before)
                    self.assertEqual(
                        recovered.vault.reveal_by_id("process-crash-account"),
                        "process-post-secret",
                    )
                else:
                    self.assertEqual(target_path.read_bytes(), target_before)
                    if audit_before:
                        self.assertEqual(recovered.audit_path.read_bytes(), audit_before)
                    else:
                        self.assertFalse(recovered.audit_path.exists())
                self.assertEqual(list((root / "vault_transactions").glob("*.pending.json")), [])
                if committed:
                    committed_markers = list(
                        (root / "vault_transactions").glob("*.committed.json")
                    )
                    self.assertEqual(len(committed_markers), 1)
                    self.assertTrue(Path(journal["post_image_path"]).exists())
                    self.assertEqual(
                        recovered.reveal_password("vault:process-crash-account"),
                        "process-post-secret",
                    )
                    self.assertEqual(
                        list((root / "vault_transactions").glob("*.committed.json")),
                        [],
                    )
                    self.assertEqual(
                        len(list((root / "vault_transactions").glob("*.reconciled.json"))),
                        2,
                    )
                self.assertEqual(list((root / "vault_transactions").glob("*.post.wal.json")), [])
                self.make_store(root)

    def test_real_subprocess_third_party_overwrites_fail_closed_and_preserve_pending(self) -> None:
        crash_script = textwrap.dedent(
            r"""
            import base64
            import os
            import sys
            from pathlib import Path

            from unified_paper_orchestrator.account_site_store import AccountSiteStore
            from unified_paper_orchestrator.credential_vault import DpapiCredentialVault
            from unified_paper_orchestrator.io_utils import read_json, write_json_atomic

            def protect(value: str) -> str:
                return base64.b64encode(value.encode("utf-8")[::-1]).decode("ascii")

            def unprotect(value: str) -> str:
                return base64.b64decode(value.encode("ascii"))[::-1].decode("utf-8")

            root = Path(sys.argv[1])
            point = sys.argv[2]
            vault_path = root / "credential_store.json"
            legacy_path = root / "legacy_credential_store.json"
            store = AccountSiteStore(
                root / "credential_store.local.json",
                root / "site_registry.json",
                vault_path=vault_path,
                legacy_vault_path=legacy_path,
                runtime_vault_path=root / "smart_runtime_credential_store.json",
                smart_state_path=root / "smart_submission_state.json",
                unified_state_path=root / "unified_state.json",
                backup_dir=root / "backups",
                audit_path=root / "audit.json",
                acl_hardener=lambda _path: None,
                vault_backend=DpapiCredentialVault(
                    vault_path,
                    protector=protect,
                    unprotector=unprotect,
                ),
                legacy_vault_backend=DpapiCredentialVault(
                    legacy_path,
                    protector=protect,
                    unprotector=unprotect,
                ),
            )

            def external_overwrite() -> None:
                data = read_json(vault_path, DpapiCredentialVault.empty())
                data.setdefault("credentials", []).append(
                    {
                        "id": f"subprocess-external-{point}",
                        "site_name": "External Subprocess Writer",
                        "username": "subprocess-external-user",
                        "password_ciphertext": protect("subprocess-external-secret"),
                        "password_scheme": "win32-dpapi-current-user",
                        "source": "non-cooperating-test-writer",
                        "created_at": "external",
                        "updated_at": "external",
                    }
                )
                data["updated_at"] = "external"
                write_json_atomic(vault_path, data)

            original_append = store._append_audit
            original_write_pending = store._write_vault_pending
            original_prepare = store._prepare_transaction_commit_locked
            original_commit = store._commit_vault_pending_locked
            if point == "before_audit":
                def overwrite_before_audit(*_args, **_kwargs):
                    external_overwrite()
                    os._exit(96)
                store._append_audit = overwrite_before_audit
            elif point == "after_audit":
                def overwrite_after_audit(*args, **kwargs):
                    original_append(*args, **kwargs)
                    external_overwrite()
                    os._exit(97)
                store._append_audit = overwrite_after_audit
            elif point == "after_audit_written":
                def overwrite_after_audit_written(path, journal):
                    original_write_pending(path, journal)
                    if journal.get("stage") == "audit_written":
                        external_overwrite()
                        os._exit(98)
                store._write_vault_pending = overwrite_after_audit_written
            elif point == "before_cleanup":
                def overwrite_before_cleanup(journal):
                    external_overwrite()
                    os._exit(99)
                store._prepare_transaction_commit_locked = overwrite_before_cleanup
            elif point == "during_cleanup":
                def overwrite_during_cleanup(journal):
                    original_prepare(journal)
                    external_overwrite()
                    os._exit(100)
                store._prepare_transaction_commit_locked = overwrite_during_cleanup
            elif point == "before_marker":
                def overwrite_before_marker(*_args, **_kwargs):
                    external_overwrite()
                    os._exit(101)
                store._commit_vault_pending_locked = overwrite_before_marker
            else:
                def overwrite_after_marker(*args, **kwargs):
                    original_commit(*args, **kwargs)
                    external_overwrite()
                    os._exit(102)
                store._commit_vault_pending_locked = overwrite_after_marker

            record = store.get_account("vault:subprocess-cas-account")
            record["notes"] = f"child update at {point}"
            store.save_account(
                record,
                original_id="vault:subprocess-cas-account",
                password="subprocess-new-secret",
            )
            raise SystemExit(0)
            """
        )
        cases = (
            ("before_audit", 96, "target_written", False, "pending", True),
            ("after_audit", 97, "target_written", True, "pending", True),
            ("after_audit_written", 98, "audit_written", True, "pending", True),
            ("before_cleanup", 99, "audit_written", True, "pending", True),
            ("during_cleanup", 100, "audit_written", True, "pending", True),
            ("before_marker", 101, "audit_written", True, "pending", True),
            ("after_marker", 102, "audit_written", True, "committed", False),
        )
        for (
            point,
            exit_code,
            expected_stage,
            audit_was_written,
            marker_kind,
            audit_rolls_back,
        ) in cases:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store = self.make_store(root)
                store.save_account(
                    {
                        "account_id": "subprocess-cas-account",
                        "scope": "journal_system",
                        "provider": "Subprocess CAS Submit",
                        "username": "subprocess-cas-user",
                    },
                    password="subprocess-original-secret",
                )
                audit_before = store.audit_path.read_bytes()

                completed = subprocess.run(
                    [sys.executable, "-c", crash_script, str(root), point],
                    cwd=Path(__file__).resolve().parents[1],
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(completed.returncode, exit_code, completed.stderr)
                for forbidden in (
                    "subprocess-new-secret",
                    "subprocess-external-secret",
                ):
                    self.assertNotIn(forbidden, completed.stdout + completed.stderr)

                markers = list((root / "vault_transactions").glob(f"*.{marker_kind}.json"))
                self.assertEqual(len(markers), 1)
                journal = read_json(markers[0], {})
                self.assertEqual(journal["stage"], expected_stage)
                external_target = store.vault_path.read_bytes()
                self.assertIn(
                    f"subprocess-external-{point}",
                    external_target.decode("utf-8"),
                )
                if audit_was_written:
                    self.assertNotEqual(store.audit_path.read_bytes(), audit_before)
                else:
                    self.assertEqual(store.audit_path.read_bytes(), audit_before)
                audit_at_crash = store.audit_path.read_bytes()
                transaction_text = "".join(
                    path.read_text(encoding="utf-8")
                    for path in (
                        markers[0],
                        Path(journal["post_image_path"]),
                        Path(journal["audit_pre_image_path"]),
                    )
                )
                for forbidden in (
                    "subprocess-cas-account",
                    "subprocess-new-secret",
                    "subprocess-external-secret",
                ):
                    self.assertNotIn(forbidden, transaction_text)

                self.assertTrue(store.lock_path.exists())
                os.utime(store.lock_path, (1, 1))
                expected_error = RegistryValidationError if audit_rolls_back else PostCommitDriftError
                with self.assertRaises(expected_error):
                    self.make_store(root)
                self.assertEqual(store.vault_path.read_bytes(), external_target)
                self.assertEqual(
                    store.audit_path.read_bytes(),
                    audit_before if audit_rolls_back else audit_at_crash,
                )
                self.assertTrue(markers[0].exists())
                self.assertTrue(Path(journal["post_image_path"]).exists())
                self.assertTrue(Path(journal["audit_pre_image_path"]).exists())

                with self.assertRaises(expected_error):
                    self.make_store(root)
                self.assertEqual(store.vault_path.read_bytes(), external_target)
                self.assertTrue(markers[0].exists())

    def test_precommit_cleanup_and_marker_windows_never_report_success(self) -> None:
        for fault_point in ("before-cleanup", "during-cleanup", "before-marker"):
            with self.subTest(fault_point=fault_point), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store = self.make_store(root)
                saved = store.save_account(
                    {
                        "account_id": "linearization-account",
                        "scope": "journal_system",
                        "provider": "Linearization Submit",
                        "username": "linearization-user",
                    },
                    password="linearization-original-secret",
                )
                audit_before = store.audit_path.read_bytes()
                edited = store.get_account(str(saved["record_key"]))
                edited["notes"] = fault_point
                original_prepare = store._prepare_transaction_commit_locked
                original_commit = store._commit_vault_pending_locked
                external_bytes = b""

                def overwrite_target() -> None:
                    nonlocal external_bytes
                    _raw_vault_upsert(
                        store.vault_path,
                        record_id=f"external-{fault_point}",
                        username="linearization-external-user",
                        password="linearization-external-secret",
                    )
                    external_bytes = store.vault_path.read_bytes()

                if fault_point == "before-cleanup":
                    def overwrite_before_cleanup(journal: dict[str, object]) -> None:
                        overwrite_target()
                        original_prepare(journal)

                    store._prepare_transaction_commit_locked = overwrite_before_cleanup
                elif fault_point == "during-cleanup":
                    def overwrite_during_cleanup(journal: dict[str, object]) -> None:
                        original_prepare(journal)
                        overwrite_target()

                    store._prepare_transaction_commit_locked = overwrite_during_cleanup
                else:
                    def overwrite_before_marker(*args: object, **kwargs: object) -> None:
                        overwrite_target()
                        original_commit(*args, **kwargs)

                    store._commit_vault_pending_locked = overwrite_before_marker

                try:
                    with self.assertRaises(RegistryValidationError):
                        store.save_account(
                            edited,
                            original_id=str(saved["record_key"]),
                            password="linearization-new-secret",
                        )
                finally:
                    store._prepare_transaction_commit_locked = original_prepare
                    store._commit_vault_pending_locked = original_commit

                self.assertTrue(external_bytes)
                self.assertEqual(store.vault_path.read_bytes(), external_bytes)
                self.assertEqual(store.audit_path.read_bytes(), audit_before)
                pending = list((root / "vault_transactions").glob("*.pending.json"))
                self.assertEqual(len(pending), 1)
                journal = read_json(pending[0], {})
                self.assertEqual(journal["stage"], "audit_written")
                self.assertTrue(Path(journal["post_image_path"]).exists())
                self.assertTrue(Path(journal["audit_pre_image_path"]).exists())
                self.assertEqual(
                    list((root / "vault_transactions").glob("*.committed.json")),
                    [],
                )

                with self.assertRaises(RegistryValidationError):
                    self.make_store(root)
                self.assertEqual(store.vault_path.read_bytes(), external_bytes)
                self.assertEqual(store.audit_path.read_bytes(), audit_before)
                self.assertTrue(pending[0].exists())

    def test_write_after_atomic_marker_is_reported_as_post_commit_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            saved = store.save_account(
                {
                    "account_id": "post-commit-account",
                    "scope": "journal_system",
                    "provider": "Post Commit Submit",
                    "username": "post-commit-user",
                },
                password="post-commit-original-secret",
            )
            edited = store.get_account(str(saved["record_key"]))
            edited["notes"] = "committed before external drift"
            original_commit = store._commit_vault_pending_locked
            external_bytes = b""

            def overwrite_after_marker(*args: object, **kwargs: object) -> None:
                nonlocal external_bytes
                original_commit(*args, **kwargs)
                _raw_vault_upsert(
                    store.vault_path,
                    record_id="external-after-marker",
                    username="post-commit-external-user",
                    password="post-commit-external-secret",
                )
                external_bytes = store.vault_path.read_bytes()

            store._commit_vault_pending_locked = overwrite_after_marker
            try:
                store.save_account(
                    edited,
                    original_id=str(saved["record_key"]),
                    password="post-commit-new-secret",
                )
            finally:
                store._commit_vault_pending_locked = original_commit

            audit_committed = store.audit_path.read_bytes()
            committed = list((root / "vault_transactions").glob("*.committed.json"))
            self.assertEqual(len(committed), 1)
            marker = read_json(committed[0], {})
            self.assertEqual(
                marker["audit_post_sha256"],
                hashlib.sha256(audit_committed).hexdigest(),
            )
            self.assertNotEqual(
                marker["target_post_sha256"],
                hashlib.sha256(external_bytes).hexdigest(),
            )

            with self.assertRaisesRegex(PostCommitDriftError, "POST_COMMIT_DRIFT"):
                self.make_store(root)
            self.assertEqual(store.vault_path.read_bytes(), external_bytes)
            self.assertEqual(store.audit_path.read_bytes(), audit_committed)
            self.assertTrue(committed[0].exists())
            self.assertTrue(Path(marker["post_image_path"]).exists())

    def test_marker_rename_boundary_never_returns_unverified_success(self) -> None:
        for fault_point in ("between-cas-and-rename", "after-rename-before-verify"):
            with self.subTest(fault_point=fault_point), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store = self.make_store(root)
                saved = store.save_account(
                    {
                        "account_id": "rename-boundary-account",
                        "scope": "journal_system",
                        "provider": "Rename Boundary Submit",
                        "username": "rename-boundary-user",
                    },
                    password="rename-boundary-original-secret",
                )
                edited = store.get_account(str(saved["record_key"]))
                edited["notes"] = fault_point
                original_replace = account_store_module.os.replace
                original_verify = store._verify_committed_state_locked
                external_bytes = b""

                def overwrite_target_directly() -> None:
                    nonlocal external_bytes
                    data = read_json(store.vault_path, {})
                    data.setdefault("credentials", []).append(
                        {
                            "id": f"external-{fault_point}",
                            "site_name": "Rename Boundary External",
                            "username": "rename-boundary-external-user",
                            "password_ciphertext": _protect_for_test(
                                "rename-boundary-external-secret"
                            ),
                            "password_scheme": "win32-dpapi-current-user",
                            "source": "non-cooperating-test-writer",
                            "created_at": "external",
                            "updated_at": "external",
                        }
                    )
                    external_bytes = (
                        json.dumps(data, ensure_ascii=False, indent=2) + "\n"
                    ).encode("utf-8")
                    store.vault_path.write_bytes(external_bytes)

                if fault_point == "between-cas-and-rename":
                    def replace_with_intervening_write(source: object, target: object) -> None:
                        target_path = Path(target)
                        if target_path.name.endswith(".committed.json") and not external_bytes:
                            overwrite_target_directly()
                        original_replace(source, target)

                    replace_patch = patch.object(
                        account_store_module.os,
                        "replace",
                        replace_with_intervening_write,
                    )
                    verify_patch = patch.object(
                        store,
                        "_verify_committed_state_locked",
                        original_verify,
                    )
                else:
                    def verify_after_intervening_write(
                        committed_path: Path,
                        journal: dict[str, object],
                    ) -> None:
                        if not external_bytes:
                            overwrite_target_directly()
                        original_verify(committed_path, journal)

                    replace_patch = patch.object(
                        account_store_module.os,
                        "replace",
                        original_replace,
                    )
                    verify_patch = patch.object(
                        store,
                        "_verify_committed_state_locked",
                        verify_after_intervening_write,
                    )

                with replace_patch, verify_patch:
                    with self.assertRaisesRegex(PostCommitDriftError, "POST_COMMIT_DRIFT"):
                        store.save_account(
                            edited,
                            original_id=str(saved["record_key"]),
                            password="rename-boundary-new-secret",
                        )

                self.assertTrue(external_bytes)
                self.assertEqual(store.vault_path.read_bytes(), external_bytes)
                self.assertEqual(
                    list((root / "vault_transactions").glob("*.pending.json")),
                    [],
                )
                committed = list((root / "vault_transactions").glob("*.committed.json"))
                self.assertEqual(len(committed), 1)
                marker = read_json(committed[0], {})
                self.assertTrue(Path(marker["post_image_path"]).exists())
                self.assertNotEqual(
                    marker["target_post_sha256"],
                    hashlib.sha256(external_bytes).hexdigest(),
                )
                with self.assertRaisesRegex(PostCommitDriftError, "POST_COMMIT_DRIFT"):
                    self.make_store(root)

    def test_standalone_vault_writer_refuses_active_transaction_markers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            saved = store.save_account(
                {
                    "account_id": "marker-guard-account",
                    "scope": "external",
                    "provider": "Marker Guard",
                    "username": "marker-guard-user",
                },
                password="marker-guard-secret",
            )
            target_before = store.vault_path.read_bytes()
            with self.assertRaises(VaultError):
                store.vault.upsert(
                    {
                        "id": "must-not-bypass-marker",
                        "site_name": "Bypass Attempt",
                        "username": "bypass-user",
                    },
                    password="bypass-secret",
                )
            self.assertEqual(store.vault_path.read_bytes(), target_before)
            self.assertEqual(
                len(list((root / "vault_transactions").glob("*.committed.json"))),
                1,
            )

            self.assertEqual(
                store.reveal_password(str(saved["record_key"])),
                "marker-guard-secret",
            )
            store.vault.upsert(
                {
                    "id": "after-reconciliation",
                    "site_name": "Allowed Standalone",
                    "username": "allowed-user",
                },
                password="allowed-secret",
            )
            self.assertEqual(store.vault.reveal_by_id("after-reconciliation"), "allowed-secret")

    def test_custom_lock_directory_uses_explicit_store_transaction_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            custom_lock = root / "locks" / "shared-account.lock"
            vault_path = root / "credential_store.json"
            transaction_dir = root / "vault_transactions"
            vault = DpapiCredentialVault(
                vault_path,
                protector=_protect_for_test,
                unprotector=_unprotect_for_test,
                lock_path=custom_lock,
                transaction_dir=transaction_dir,
            )
            legacy_path = root / "legacy_credential_store.json"
            legacy = DpapiCredentialVault(
                legacy_path,
                protector=_protect_for_test,
                unprotector=_unprotect_for_test,
                lock_path=custom_lock,
            )
            store = AccountSiteStore(
                root / "credential_store.local.json",
                root / "site_registry.json",
                vault_path=vault_path,
                legacy_vault_path=legacy_path,
                runtime_vault_path=root / "smart_runtime_credential_store.json",
                smart_state_path=root / "smart_submission_state.json",
                unified_state_path=root / "unified_state.json",
                backup_dir=root / "backups",
                audit_path=root / "audit.json",
                lock_path=custom_lock,
                acl_hardener=lambda _path: None,
                vault_backend=vault,
                legacy_vault_backend=legacy,
            )
            saved = store.save_account(
                {
                    "account_id": "custom-lock-account",
                    "scope": "journal_system",
                    "provider": "Custom Lock Submit",
                    "username": "custom-lock-user",
                },
                password="custom-lock-secret",
            )
            self.assertEqual(vault.transaction_dir, transaction_dir)
            self.assertNotEqual(vault.transaction_dir, custom_lock.parent / "vault_transactions")
            self.assertEqual(
                len(list(transaction_dir.glob("*.committed.json"))),
                1,
            )

            blocked_writes = (
                lambda: vault.upsert_password("blocked-password", "blocked-secret"),
                lambda: vault.upsert(
                    {
                        "id": "blocked-upsert",
                        "site_name": "Blocked Upsert",
                        "username": "blocked-user",
                    },
                    password="blocked-secret",
                ),
                lambda: vault.delete(str(saved["record_key"])),
                lambda: vault.delete_by_id("custom-lock-account"),
            )
            for index, blocked_write in enumerate(blocked_writes):
                with self.subTest(write_api=index):
                    target_before = vault_path.read_bytes()
                    with self.assertRaises(VaultError):
                        blocked_write()
                    self.assertEqual(vault_path.read_bytes(), target_before)

            default_vault = DpapiCredentialVault(
                root / "standalone" / "vault.json",
                protector=_protect_for_test,
                unprotector=_unprotect_for_test,
            )
            self.assertEqual(
                default_vault.transaction_dir,
                default_vault.path.parent / "vault_transactions",
            )

            mismatched = DpapiCredentialVault(
                vault_path,
                protector=_protect_for_test,
                unprotector=_unprotect_for_test,
                lock_path=custom_lock,
                transaction_dir=root / "wrong_transactions",
            )
            with self.assertRaisesRegex(RegistryValidationError, "transaction directory"):
                AccountSiteStore(
                    root / "credential_store.local.json",
                    root / "site_registry.json",
                    vault_path=vault_path,
                    legacy_vault_path=legacy_path,
                    runtime_vault_path=root / "smart_runtime_credential_store.json",
                    smart_state_path=root / "smart_submission_state.json",
                    unified_state_path=root / "unified_state.json",
                    backup_dir=root / "backups",
                    audit_path=root / "audit.json",
                    lock_path=custom_lock,
                    acl_hardener=lambda _path: None,
                    vault_backend=mismatched,
                )

    def test_corrupt_recovery_backup_and_third_party_target_write_fail_closed(self) -> None:
        for fault in ("corrupt-backup", "corrupt-post-wal", "third-party-write"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                store = self.make_store(root)
                saved = store.save_account(
                    {
                        "account_id": f"recovery-{fault}",
                        "scope": "journal_system",
                        "provider": "Recovery Submit",
                        "username": "recovery-user",
                    },
                    password="recovery-original-secret",
                )
                edited = store.get_account(str(saved["record_key"]))
                edited["notes"] = "pending recovery"

                def crash_before_audit(*_args: object, **_kwargs: object) -> None:
                    raise _SimulatedCrash()

                store._append_audit = crash_before_audit
                with self.assertRaises(_SimulatedCrash):
                    store.save_account(edited, original_id=str(saved["record_key"]))

                pending = list((root / "vault_transactions").glob("*.pending.json"))
                self.assertEqual(len(pending), 1)
                journal = read_json(pending[0], {})
                if fault == "corrupt-backup":
                    write_json_atomic(Path(journal["backup_path"]), {"corrupt": True})
                elif fault == "corrupt-post-wal":
                    write_json_atomic(Path(journal["post_image_path"]), {"corrupt": True})
                else:
                    _raw_vault_upsert(
                        store.vault_path,
                        record_id="third-party-record",
                        username="third-party-user",
                        password="third-party-secret",
                    )
                target_before_recovery = store.vault_path.read_bytes()

                with self.assertRaises(RegistryValidationError):
                    self.make_store(root)
                self.assertEqual(store.vault_path.read_bytes(), target_before_recovery)
                self.assertTrue(pending[0].exists())

    def test_live_third_party_write_before_replace_is_preserved_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            saved = store.save_account(
                {
                    "account_id": "live-cas-account",
                    "scope": "journal_system",
                    "provider": "Live CAS",
                    "username": "live-cas-user",
                },
                password="live-cas-secret",
            )
            audit_before = store.audit_path.read_bytes()
            edited = store.get_account(str(saved["record_key"]))
            edited["notes"] = "must-not-overwrite-third-party"
            original_pending_write = store._write_vault_pending
            injected = False

            def inject_external_write(path: Path, journal: dict[str, object]) -> None:
                nonlocal injected
                original_pending_write(path, journal)
                if injected:
                    return
                injected = True
                _raw_vault_upsert(
                    store.vault_path,
                    record_id="external-live-write",
                    username="external-writer",
                    password="external-live-secret",
                )

            store._write_vault_pending = inject_external_write
            try:
                with self.assertRaisesRegex(RegistryValidationError, "写入前发生并发变化"):
                    store.save_account(edited, original_id=str(saved["record_key"]))
            finally:
                store._write_vault_pending = original_pending_write

            external_bytes = store.vault_path.read_bytes()
            self.assertTrue(any(item["id"] == "external-live-write" for item in store.vault.list_records()))
            self.assertEqual(store.audit_path.read_bytes(), audit_before)
            self.assertEqual(list((root / "vault_transactions").glob("*.pending.json")), [])
            self.assertEqual(store.vault_path.read_bytes(), external_bytes)

    def test_all_built_in_account_site_vault_write_paths_share_lock_contract(self) -> None:
        vault_tree = ast.parse(inspect.getsource(DpapiCredentialVault))
        vault_methods = {
            node.name: node
            for node in ast.walk(vault_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def is_mutation_guard(node: ast.With) -> bool:
            return any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and isinstance(item.context_expr.func.value, ast.Name)
                and item.context_expr.func.value.id == "self"
                and item.context_expr.func.attr == "_mutation_transaction"
                for item in node.items
            )

        for method_name in ("upsert_password", "upsert", "delete", "delete_by_id"):
            method = vault_methods[method_name]
            guards = [
                node
                for node in ast.walk(method)
                if isinstance(node, ast.With) and is_mutation_guard(node)
            ]
            self.assertEqual(len(guards), 1, method_name)
            writes = [
                node
                for node in ast.walk(method)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "write_json_atomic"
            ]
            self.assertTrue(writes, method_name)
            for write in writes:
                self.assertTrue(
                    any(
                        guard.lineno <= write.lineno <= (guard.end_lineno or guard.lineno)
                        for guard in guards
                    ),
                    method_name,
                )

        mutation_transaction = vault_methods["_mutation_transaction"]
        self.assertNotIn(
            "lock_path.parent",
            inspect.getsource(DpapiCredentialVault._mutation_transaction),
        )
        file_lock_calls = [
            node
            for node in ast.walk(mutation_transaction)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "FileLock"
        ]
        self.assertEqual(len(file_lock_calls), 1)
        self.assertTrue(
            any(
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
                and node.attr == "lock_path"
                for node in ast.walk(file_lock_calls[0])
            )
        )

        store_tree = ast.parse(inspect.getsource(AccountSiteStore))
        store_methods = {
            node.name: node
            for node in ast.walk(store_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        def is_store_transaction_guard(node: ast.With) -> bool:
            return any(
                isinstance(item.context_expr, ast.Call)
                and isinstance(item.context_expr.func, ast.Attribute)
                and isinstance(item.context_expr.func.value, ast.Name)
                and item.context_expr.func.value.id == "self"
                and item.context_expr.func.attr == "_transaction"
                for item in node.items
            )

        for method_name in (
            "record_browser_session_evidence",
            "reveal_password",
            "save_account",
            "delete_account",
            "save_system",
            "delete_system",
            "save_journal",
            "delete_journal",
        ):
            self.assertTrue(
                any(
                    isinstance(node, ast.With) and is_store_transaction_guard(node)
                    for node in ast.walk(store_methods[method_name])
                ),
                method_name,
            )

        forbidden_vault_mutators = {"upsert_password", "upsert", "delete", "delete_by_id"}
        for node in ast.walk(store_tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in forbidden_vault_mutators:
                continue
            owner = node.func.value
            self.assertFalse(
                isinstance(owner, ast.Attribute)
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "self"
                and owner.attr in {"vault", "legacy_vault"},
                node.func.attr,
            )

        managed_attrs = {"credential_path", "site_registry_path", "vault_path"}
        for node in ast.walk(store_tree):
            if (
                not isinstance(node, ast.Call)
                or not isinstance(node.func, ast.Name)
                or node.func.id not in {"write_json_atomic", "write_bytes_atomic"}
            ):
                continue
            direct_managed_args = [
                child
                for argument in node.args
                for child in ast.walk(argument)
                if isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id == "self"
                and child.attr in managed_attrs
            ]
            self.assertEqual(direct_managed_args, [], node.func.id)

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            store = self.make_store(root)
            self.assertEqual(store.vault.lock_path, store.lock_path)
            self.assertEqual(store.vault.transaction_dir, store.vault_transaction_dir)
            self.assertEqual(store.legacy_vault.lock_path, store.lock_path)
            mismatched = DpapiCredentialVault(
                store.vault_path,
                protector=_protect_for_test,
                unprotector=_unprotect_for_test,
                lock_path=root / "different.lock",
            )
            with self.assertRaises(RegistryValidationError):
                AccountSiteStore(
                    store.credential_path,
                    store.site_registry_path,
                    vault_path=store.vault_path,
                    legacy_vault_path=store.legacy_vault_path,
                    runtime_vault_path=root / "smart_runtime_credential_store.json",
                    smart_state_path=root / "smart_submission_state.json",
                    unified_state_path=root / "unified_state.json",
                    backup_dir=root / "backups",
                    audit_path=root / "audit.json",
                    acl_hardener=lambda _path: None,
                    vault_backend=mismatched,
                )

    def test_public_ui_api_is_importable_without_opening_real_state(self) -> None:
        self.assertTrue(callable(open_account_site_manager))


if __name__ == "__main__":
    unittest.main()
