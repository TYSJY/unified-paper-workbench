from __future__ import annotations

from unified_paper_orchestrator.submission_site_adapters import (
    ELSEVIER_ADAPTER,
    INDERSCIENCE_ADAPTER,
    SCHOLAR_ONE_ADAPTER,
    SPRINGER_NATURE_ADAPTER,
    TAYLOR_FRANCIS_ADAPTER,
    WILEY_ADAPTER,
    submission_site_adapter,
)


def test_password_page_wins_over_inherited_sensitive_form_controls() -> None:
    snapshot = {
        "origin": "https://idp-personal-authenticator.springernature.com",
        "text": "Welcome back Enter password Terms and conditions",
        "controls": [
            {
                "ref": "password",
                "tag": "input",
                "type": "password",
                "label": "Enter your password",
                "sensitive": True,
            },
            {
                "ref": "continue",
                "tag": "button",
                "type": "submit",
                "label": "Continue",
                "sensitive": True,
            },
            {
                "ref": "forgot",
                "tag": "a",
                "type": "a",
                "label": "Forgot password?",
                "sensitive": True,
            },
        ],
    }

    assert SPRINGER_NATURE_ADAPTER.classify_page(snapshot) == "password"


def test_scholar_one_reset_pages_have_explicit_deterministic_states() -> None:
    request = {
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/site/jaise/auth/reset-password",
        "text": "Reset Password Send Reset Link",
        "controls": [
            {"tag": "input", "type": "email", "label": "Your Primary E-mail"},
            {"tag": "button", "type": "submit", "label": "Send Reset Link"},
        ],
    }
    sent = {
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/site/jaise/auth/reset-password-success",
        "text": "Please Check Your E-mail for Instructions",
        "controls": [],
    }
    change = {
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/site/jaise/auth/reset-password/token",
        "text": "Reset Password New Password Confirm Password",
        "controls": [
            {"tag": "input", "type": "password", "label": "New Password"},
            {"tag": "input", "type": "password", "label": "Confirm Password"},
        ],
    }

    assert SCHOLAR_ONE_ADAPTER.classify_page(request) == "password_reset_request"
    assert SCHOLAR_ONE_ADAPTER.classify_page(sent) == "password_reset_sent"
    assert SCHOLAR_ONE_ADAPTER.classify_page(change) == "password_reset_change"


def test_scholar_one_legacy_password_reset_anchor_is_exact_submit_control() -> None:
    snapshot = {
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise",
        "text": "Create a new password",
        "controls": [
            {"ref": "upo-0001", "tag": "input", "type": "password"},
            {"ref": "upo-0002", "tag": "input", "type": "password"},
            {
                "ref": "upo-0003",
                "tag": "a",
                "type": "a",
                "label": "Continue",
                "id": "createNewPassword",
                "action": "password_reset_continue",
            },
        ],
    }

    control = SCHOLAR_ONE_ADAPTER.submit_control(snapshot)

    assert SCHOLAR_ONE_ADAPTER.classify_page(snapshot) == "password_reset_change"
    assert control is not None
    assert control["ref"] == "upo-0003"


def test_scholar_one_localized_recovery_and_profile_pages_are_explicit() -> None:
    completed = {
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise",
        "text": "\u6210\u529f\uff01\u60a8\u7684\u5bc6\u7801\u5df2\u66f4\u65b0\u3002",
        "controls": [],
    }
    profile = {
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise",
        "text": "Update your profile",
        "controls": [
            {
                "ref": "consent-no",
                "tag": "input",
                "type": "radio",
                "id": "CONSENT_NO",
                "name": "CONSENT_DEMOGRAPHIC_QUESTION",
            },
            {
                "ref": "next",
                "tag": "a",
                "type": "a",
                "label": "next_button",
            },
        ],
    }
    authenticated = {
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/jaise",
        "text": "\u8a79\u5fd7\u8d85 \u9000\u51fa\u767b\u5f55 \u4f5c\u8005",
        "controls": [],
    }

    assert SCHOLAR_ONE_ADAPTER.classify_page(completed) == "password_reset_completed"
    assert SCHOLAR_ONE_ADAPTER.classify_page(profile) == "profile_update"
    assert SCHOLAR_ONE_ADAPTER.classify_page(authenticated) == "authenticated"


def test_authenticated_author_email_is_not_reclassified_as_login_or_terms() -> None:
    snapshot = {
        "origin": "https://submission.springernature.com",
        "text": "Authors Terms and conditions",
        "controls": [
            {
                "tag": "input",
                "type": "email",
                "label": "Email (Institutional email if you have one)",
                "disabled": False,
            },
            {
                "tag": "button",
                "type": "button",
                "label": "Your privacy choices/Manage cookies",
                "disabled": False,
            },
            {
                "tag": "a",
                "type": "a",
                "label": "Terms and conditions",
                "disabled": False,
            },
        ],
    }

    assert SPRINGER_NATURE_ADAPTER.classify_page(snapshot) == "authenticated"


def test_explicit_terms_consent_control_remains_a_human_checkpoint() -> None:
    snapshot = {
        "origin": "https://submission.springernature.com",
        "text": "Please review the submission terms.",
        "controls": [
            {
                "tag": "input",
                "type": "checkbox",
                "label": "I agree to the submission terms and conditions",
                "disabled": False,
            }
        ],
    }

    assert SPRINGER_NATURE_ADAPTER.classify_page(snapshot) == "terms_confirmation"


def test_unknown_sensitive_code_input_remains_human_verification() -> None:
    snapshot = {
        "origin": "https://idp-personal-authenticator.springernature.com",
        "text": "Enter the code",
        "controls": [
            {
                "ref": "code",
                "tag": "input",
                "type": "text",
                "label": "Code",
                "sensitive": True,
            }
        ],
    }

    assert (
        SPRINGER_NATURE_ADAPTER.classify_page(snapshot)
        == "human_verification"
    )


def test_springer_adapter_derives_journal_specific_submission_start_url() -> None:
    journal_id = SPRINGER_NATURE_ADAPTER.extract_journal_identifier(
        "Official source: https://link.springer.com/journal/11227/submission-guidelines"
    )

    assert journal_id == "11227"
    start_url = SPRINGER_NATURE_ADAPTER.submission_start_url(journal_id)
    assert start_url == "https://submission.nature.com/new-submission/11227/3"
    assert SPRINGER_NATURE_ADAPTER.allows_url(start_url) is True


def test_major_publisher_monitor_dashboards_have_explicit_adapter_contracts() -> None:
    assert submission_site_adapter("scholar_one") is SCHOLAR_ONE_ADAPTER
    assert submission_site_adapter("taylor_francis") is TAYLOR_FRANCIS_ADAPTER
    assert submission_site_adapter("wiley") is WILEY_ADAPTER
    assert submission_site_adapter("elsevier") is ELSEVIER_ADAPTER
    assert submission_site_adapter("inderscience") is INDERSCIENCE_ADAPTER
    assert TAYLOR_FRANCIS_ADAPTER.allows_url(
        "https://rp.tandfonline.com/dashboard/"
    )
    assert WILEY_ADAPTER.allows_url("https://submission.wiley.com/")
    assert WILEY_ADAPTER.allows_url("https://authors.wiley.com/dashboard")
    assert ELSEVIER_ADAPTER.allows_url("https://submit.elsevier.com/SCICO/")
    assert SCHOLAR_ONE_ADAPTER.allows_url(
        "https://mc.manuscriptcentral.com/jaise"
    )
    assert INDERSCIENCE_ADAPTER.allows_url(
        "https://www.indersciencesubmissions.com/index.php?action=authorreg"
    )


def test_wiley_new_author_dashboard_is_recognized_as_authenticated() -> None:
    snapshot = {
        "origin": "https://authors.wiley.com",
        "path": "/dashboard",
        "text": "Wiley Authors My Dashboard Manuscript title or ID",
        "controls": [],
    }

    assert WILEY_ADAPTER.classify_page(snapshot) == "authenticated"


def test_wiley_localized_author_dashboard_is_recognized_as_authenticated() -> None:
    snapshot = {
        "origin": "https://authors.wiley.com",
        "path": "/dashboard/journal/CPE",
        "text": (
            "詹志超 我的仪表盘 开始为《并发与计算：实践与经验》"
            "提交新稿件 开始提交 稿件标题或ID"
        ),
        "controls": [
            {"tag": "a", "type": "a", "label": "开始提交"},
            {"tag": "input", "type": "text", "label": "搜索"},
        ],
    }

    assert WILEY_ADAPTER.classify_page(snapshot) == "authenticated"


def test_wiley_submission_final_review_is_recognized_as_authenticated() -> None:
    snapshot = {
        "origin": "https://submission.wiley.com",
        "path": (
            "/submission/submissionBoard/"
            "45c19a2c-7910-4173-8f3f-029248569c8f/finalReview"
        ),
        "text": "Final review",
        "controls": [
            {
                "tag": "button",
                "type": "button",
                "label": "Complete my submission",
            }
        ],
    }

    assert WILEY_ADAPTER.classify_page(snapshot) == "authenticated"


def test_wiley_adapter_derives_current_journal_submission_portal() -> None:
    journal_id = WILEY_ADAPTER.extract_journal_identifier(
        "Official article: https://onlinelibrary.wiley.com/doi/10.1002/cpe.70744"
    )

    assert journal_id == "cpe"
    assert WILEY_ADAPTER.submission_start_url(journal_id) == (
        "https://authors.wiley.com/journal/CPE"
    )
    assert WILEY_ADAPTER.allows_url(
        "https://authors.wiley.com/dashboard/journal/CPE"
    )
    assert WILEY_ADAPTER.allows_url("https://wiley.scienceconnect.io/login")


def test_closed_scholar_one_journal_portal_is_not_treated_as_login() -> None:
    snapshot = {
        "origin": "https://mc.manuscriptcentral.com",
        "path": "/cpe",
        "text": (
            "The ScholarOne site for this journal is closed to new submissions. "
            "Please visit the journal homepage."
        ),
        "controls": [
            {"tag": "input", "type": "text", "label": "User ID"},
            {"tag": "input", "type": "password", "label": "Password"},
        ],
    }

    assert SCHOLAR_ONE_ADAPTER.classify_page(snapshot) == (
        "submission_portal_closed"
    )


def test_scholar_one_builds_journal_specific_submission_start_url() -> None:
    journal_id = SCHOLAR_ONE_ADAPTER.extract_journal_identifier(
        "Submission system: https://mc.manuscriptcentral.com/cpe"
    )

    assert journal_id == "cpe"
    assert SCHOLAR_ONE_ADAPTER.submission_start_url(journal_id) == (
        "https://mc.manuscriptcentral.com/cpe"
    )


def test_inderscience_login_and_dashboard_have_explicit_page_states() -> None:
    login = {
        "origin": "https://www.indersciencesubmissions.com",
        "path": "/index.php",
        "text": "Log in Username Password",
        "controls": [
            {"tag": "input", "type": "text", "label": "Username"},
            {"tag": "input", "type": "password", "label": "Password"},
        ],
    }
    dashboard = {
        "origin": "https://www.indersciencesubmissions.com",
        "path": "/session/index.php",
        "text": "Author dashboard Your draft submissions New submission Log out",
        "controls": [],
    }
    rejected = {
        "origin": "https://www.indersciencesubmissions.com",
        "path": "/index.php",
        "text": "无法识别您输入的用户名和/或密码。请重试。",
        "controls": [
            {"tag": "input", "type": "text", "label": "用户名"},
            {"tag": "input", "type": "password", "label": "密码"},
        ],
    }
    localized_dashboard = {
        "origin": "https://www.indersciencesubmissions.com",
        "path": "/session/index.php",
        "text": "作者仪表板 草稿投稿 新投稿 退出登录",
        "controls": [],
    }

    assert INDERSCIENCE_ADAPTER.classify_page(login) == "password"
    assert INDERSCIENCE_ADAPTER.classify_page(dashboard) == "authenticated"
    assert INDERSCIENCE_ADAPTER.classify_page(rejected) == "password_rejected"
    assert (
        INDERSCIENCE_ADAPTER.classify_page(localized_dashboard)
        == "authenticated"
    )


def test_inderscience_registration_form_is_not_mistaken_for_email_login() -> None:
    snapshot = {
        "origin": "https://www.indersciencesubmissions.com",
        "path": "/index.php",
        "text": "Register as an author",
        "controls": [
            {"ref": "email", "tag": "input", "type": "email", "id": "txtEmail"},
            {"ref": "first", "tag": "input", "type": "text", "id": "txtFirstName"},
            {"ref": "last", "tag": "input", "type": "text", "id": "txtLastName"},
            {"ref": "user", "tag": "input", "type": "text", "id": "txtUserName"},
            {
                "ref": "secret",
                "tag": "input",
                "type": "text",
                "id": "txtPassword",
                "sensitive": True,
            },
        ],
    }

    assert INDERSCIENCE_ADAPTER.classify_page(snapshot) == "registration"


def test_scholar_one_login_exposes_explicit_account_creation_control() -> None:
    snapshot = {
        "origin": "https://mc.manuscriptcentral.com",
        "text": "Login Create an Account",
        "controls": [
            {
                "ref": "user",
                "tag": "input",
                "type": "text",
                "label": "USERID",
            },
            {
                "ref": "password",
                "tag": "input",
                "type": "password",
                "label": "PASSWORD",
            },
            {
                "ref": "register",
                "tag": "a",
                "type": "a",
                "label": "创建一个帐户",
            },
        ],
    }

    assert SCHOLAR_ONE_ADAPTER.classify_page(snapshot) == "password"
    assert SCHOLAR_ONE_ADAPTER.registration_control(snapshot)["ref"] == "register"


def test_orcid_login_control_is_distinct_from_informational_orcid_link() -> None:
    snapshot = {
        "origin": "https://mc.manuscriptcentral.com",
        "text": "Choose how to sign in",
        "controls": [
            {
                "ref": "about",
                "tag": "a",
                "type": "a",
                "label": "Learn about ORCID",
            },
            {
                "ref": "orcid-login",
                "tag": "button",
                "type": "button",
                "label": "Sign in with ORCID iD",
            },
        ],
    }

    control = SCHOLAR_ONE_ADAPTER.orcid_login_control(snapshot)

    assert control is not None
    assert control["ref"] == "orcid-login"


def test_orcid_login_control_accepts_scienceconnect_icon_button_label() -> None:
    snapshot = {
        "origin": "https://wiley.scienceconnect.io",
        "text": "Log in to continue to Wiley Authors",
        "controls": [
            {
                "ref": "orcid-login",
                "tag": "button",
                "type": "button",
                "label": "ORCID��ť",
            },
            {
                "ref": "about",
                "tag": "a",
                "type": "a",
                "label": "Learn about ORCID",
            },
        ],
    }

    control = WILEY_ADAPTER.orcid_login_control(snapshot)

    assert control is not None
    assert control["ref"] == "orcid-login"


def test_orcid_authorization_control_only_accepts_official_affirmative_action() -> None:
    snapshot = {
        "origin": "https://orcid.org",
        "text": "The journal requests access to your authenticated ORCID iD",
        "controls": [
            {"ref": "deny", "tag": "button", "type": "button", "label": "Deny"},
            {
                "ref": "authorize",
                "tag": "button",
                "type": "submit",
                "label": "Authorize access",
            },
        ],
    }

    control = SPRINGER_NATURE_ADAPTER.orcid_authorization_control(snapshot)

    assert control is not None
    assert control["ref"] == "authorize"


def test_taylor_francis_login_is_not_mistaken_for_registration_link() -> None:
    snapshot = {
        "origin": "https://accounts.taylorfrancis.com",
        "text": "Sign in to continue. Don't have an account? Register",
        "controls": [
            {
                "ref": "email",
                "tag": "input",
                "type": "text",
                "label": "Email",
            },
            {
                "ref": "password",
                "tag": "input",
                "type": "password",
                "label": "Password",
            },
            {
                "ref": "login",
                "tag": "button",
                "type": "button",
                "label": "Sign in",
            },
            {
                "ref": "register",
                "tag": "a",
                "type": "a",
                "label": "Register",
            },
        ],
    }

    assert TAYLOR_FRANCIS_ADAPTER.classify_page(snapshot) == "password"


def test_taylor_francis_dashboard_origin_requires_authenticated_marker() -> None:
    redirecting = {
        "origin": "https://rp.tandfonline.com",
        "text": "Submission Portal",
        "controls": [],
    }
    authenticated = {
        "origin": "https://rp.tandfonline.com",
        "text": "My submissions Submit, track and manage your work",
        "controls": [],
    }

    assert TAYLOR_FRANCIS_ADAPTER.classify_page(redirecting) == "unknown"
    assert TAYLOR_FRANCIS_ADAPTER.classify_page(authenticated) == "authenticated"
