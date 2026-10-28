from __future__ import annotations

from unified_paper_orchestrator.ui.presentation import (
    STATUS_LABELS,
    author_status_label,
    author_status_value,
    filter_logical_groups,
    group_account_summaries,
    group_journal_records,
    group_submission_sites,
    parallel_limit_label,
    parallel_limit_value,
    responsive_window_size,
    scheduler_mode_label,
    scheduler_mode_value,
    status_label,
)


def test_internal_statuses_have_chinese_display_labels() -> None:
    assert status_label("discovered") == "已发现"
    assert status_label("needs_discovery") == "待补充"
    assert status_label("adapter_planned") == "适配计划中"
    assert len(STATUS_LABELS.values()) == len(set(STATUS_LABELS.values()))


def test_unknown_internal_tokens_do_not_leak_as_english_ui_copy() -> None:
    assert status_label("new_backend_status") == "需核对"
    assert scheduler_mode_label("experimental_mode") == "需核对，详见审计"
    assert author_status_label("INTERNAL_ONLY") == "需核对"


def test_scheduler_modes_round_trip_without_changing_internal_values() -> None:
    expected = {
        "manual": "手动暂停",
        "run_once": "运行一轮",
        "scheduled": "定时运行",
        "auto": "自动调度",
    }
    for internal, display in expected.items():
        assert scheduler_mode_label(internal) == display
        assert scheduler_mode_value(display) == internal


def test_author_status_and_parallel_all_round_trip() -> None:
    assert author_status_label("READY") == "就绪"
    assert author_status_label("DRAFT") == "草稿"
    assert author_status_value("就绪") == "READY"
    assert author_status_value("草稿") == "DRAFT"
    assert parallel_limit_label("all") == "全部"
    assert parallel_limit_value("全部") == "all"


def test_account_view_groups_matching_vault_and_smart_rows_without_mutating_input() -> None:
    rows = [
        {
            "record_key": "vault:legacy-one",
            "account_id": "legacy-one",
            "scope": "journal",
            "provider": "Example Submit",
            "username": "author@example.test",
            "login_url": "https://submit.example.test/login",
            "status": "verified",
            "source_kind": "vault",
            "password_ciphertext": "must-not-enter-view-model",
            "read_only": True,
        },
        {
            "record_key": "smart:smart-one",
            "account_id": "smart-one",
            "scope": "journal_system",
            "provider": "Example Submit",
            "username": "author@example.test",
            "login_url": "https://submit.example.test/author",
            "status": "configured_unverified",
            "source_kind": "smart",
            "password": "must-not-enter-view-model",
            "credential_ref": "vault:legacy-one",
            "read_only": False,
        },
        {
            "record_key": "smart:other-user",
            "account_id": "other-user",
            "scope": "journal_system",
            "provider": "Example Submit",
            "username": "other@example.test",
            "login_url": "https://submit.example.test/login",
            "status": "unknown",
            "source_kind": "smart",
            "read_only": False,
        },
    ]
    original = [dict(row) for row in rows]

    groups = group_account_summaries(rows)

    assert len(groups) == 2
    merged = next(group for group in groups if len(group["records"]) == 2)
    assert merged["scope"] == "journal_system"
    assert merged["subtitle"].endswith("2 条底层记录")
    assert {record["record_key"] for record in merged["records"]} == {
        "vault:legacy-one",
        "smart:smart-one",
    }
    serialized = repr(groups)
    assert "must-not-enter-view-model" not in serialized
    assert "credential_ref" not in serialized
    assert rows == original


def test_grouped_account_with_conflicting_source_statuses_never_promotes_one_source() -> None:
    groups = group_account_summaries(
        [
            {
                "record_key": "legacy:legacy-one",
                "scope": "journal_system",
                "provider": "Example Submit",
                "username": "author@example.test",
                "login_url": "https://submit.example.test/login",
                "status": "verified",
                "source_kind": "legacy",
            },
            {
                "record_key": "smart:smart-one",
                "scope": "journal_system",
                "provider": "Example Submit",
                "username": "author@example.test",
                "login_url": "https://submit.example.test/author",
                "status": "configured_unverified",
                "source_kind": "smart",
            },
        ]
    )
    assert len(groups) == 1
    assert groups[0]["status"] == "source_status_conflict"
    assert status_label(groups[0]["status"]) == "来源状态不一致"


def test_grouped_account_with_missing_and_verified_status_requires_review() -> None:
    groups = group_account_summaries(
        [
            {
                "record_key": "legacy:legacy-one",
                "scope": "journal_system",
                "provider": "Example Submit",
                "username": "author@example.test",
                "login_url": "https://submit.example.test/login",
                "status": "verified",
                "source_kind": "legacy",
            },
            {
                "record_key": "smart:smart-one",
                "scope": "journal_system",
                "provider": "Example Submit",
                "username": "author@example.test",
                "login_url": "https://submit.example.test/author",
                "status": "",
                "source_kind": "smart",
            },
        ]
    )
    assert groups[0]["status"] == "source_status_conflict"
    assert status_label(groups[0]["status"]) == "来源状态不一致"


def test_responsive_window_size_stays_inside_small_and_scaled_screens() -> None:
    assert responsive_window_size(
        1366,
        768,
        preferred=(1480, 840),
        minimum=(860, 540),
    ) == (1306, 668, 860, 540)
    width, height, minimum_width, minimum_height = responsive_window_size(
        1024,
        768,
        preferred=(1100, 680),
        minimum=(760, 500),
    )
    assert width <= 964
    assert height <= 668
    assert minimum_width <= width
    assert minimum_height <= height


def test_account_view_does_not_merge_same_username_across_unrelated_providers() -> None:
    groups = group_account_summaries(
        [
            {
                "record_key": "smart:first",
                "scope": "external_service",
                "provider": "Overleaf",
                "username": "same-user",
                "login_url": "https://overleaf.com/login",
            },
            {
                "record_key": "smart:second",
                "scope": "external",
                "provider": "GitHub",
                "username": "same-user",
                "login_url": "https://github.com/login",
            },
        ]
    )
    assert len(groups) == 2


def test_thirty_five_physical_accounts_can_render_as_twenty_logical_accounts() -> None:
    rows = []
    for index in range(20):
        copies = 2 if index < 15 else 1
        for copy_index in range(copies):
            rows.append(
                {
                    "record_key": f"smart:fixture-{index}-{copy_index}",
                    "account_id": f"fixture-{index}-{copy_index}",
                    "scope": ("email", "journal_system", "external")[index % 3],
                    "provider": f"示例站点 {index}",
                    "username": f"author-{index}@example.test",
                    "login_url": f"https://service-{index}.example.test/login/{copy_index}",
                    "source_kind": "smart",
                }
            )

    groups = group_account_summaries(rows)

    assert len(rows) == 35
    assert len(groups) == 20
    assert sum(len(group["records"]) for group in groups) == 35


def test_site_and_journal_views_group_display_rows_without_changing_registries() -> None:
    systems = [
        {
            "system_id": "em-one",
            "label": "Editorial Manager",
            "login_url": "https://editorialmanager.com/login",
            "status": "active",
        },
        {
            "system_id": "em-copy",
            "label": "Editorial Manager",
            "submission_url": "https://editorialmanager.com/submit",
            "status": "active",
        },
    ]
    journals = [
        {
            "journal_key": "journal-one",
            "journal_name": "Journal One",
            "system_id": "em-one",
            "status": "active",
        },
        {
            "journal_key": "journal-one-copy",
            "journal_name": "Journal One",
            "system_id": "em-one",
            "status": "active",
        },
    ]
    original_systems = [dict(row) for row in systems]
    original_journals = [dict(row) for row in journals]

    site_groups = group_submission_sites(systems, journals)
    journal_groups = group_journal_records(journals)

    assert len(site_groups) == 1
    assert len(site_groups[0]["records"]) == 2
    assert len(site_groups[0]["journals"]) == 2
    assert len(journal_groups) == 1
    assert len(journal_groups[0]["records"]) == 2
    assert "em-one" not in journal_groups[0]["subtitle"]
    assert systems == original_systems
    assert journals == original_journals


def test_logical_group_search_uses_hidden_backing_metadata_without_exposing_it_as_title() -> None:
    groups = group_account_summaries(
        [
            {
                "record_key": "smart:technical-id",
                "account_id": "technical-id",
                "scope": "email",
                "provider": "School Mail",
                "username": "author@school.test",
                "login_url": "https://mail.school.test",
            }
        ]
    )
    filtered = filter_logical_groups(groups, "technical-id")
    assert len(filtered) == 1
    assert "technical-id" not in filtered[0]["title"]
