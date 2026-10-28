"""Deterministic submission-site contracts used before any SDK browser turn."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .browser_session import normalize_origin


def _origin(value: Any) -> str:
    return normalize_origin(str(value or "").strip())


ORCID_AUTH_ORIGINS = (_origin("https://orcid.org"),)


def has_explicit_terms_confirmation(snapshot: Mapping[str, Any]) -> bool:
    """Detect a real terms-consent action, not footer or cookie controls."""

    controls = snapshot.get("controls")
    if not isinstance(controls, list):
        return False
    consent_verbs = ("accept", "agree", "consent")
    terms_objects = (
        "terms",
        "conditions",
        "privacy policy",
        "submission policy",
    )
    for item in controls:
        if not isinstance(item, Mapping) or bool(item.get("disabled")):
            continue
        tag = str(item.get("tag") or "").casefold()
        control_type = str(item.get("type") or "").casefold()
        if tag not in {"button", "input"}:
            continue
        if tag == "input" and control_type not in {"checkbox", "button", "submit"}:
            continue
        label = str(item.get("label") or "").casefold()
        if "cookie" in label:
            continue
        if any(verb in label for verb in consent_verbs) and any(
            obj in label for obj in terms_objects
        ):
            return True
    return False


@dataclass(frozen=True)
class SubmissionSiteAdapter:
    system_id: str
    label: str
    login_start_url: str
    dashboard_url: str
    auth_urls: tuple[str, ...]
    supports_registration: bool = False
    submission_start_url_template: str = ""
    journal_identifier_patterns: tuple[str, ...] = ()
    authenticated_markers: tuple[str, ...] = ()
    password_rejected_markers: tuple[str, ...] = ()
    credential_recovery_url: str = ""
    registration_url: str = ""
    registration_field_markers: tuple[str, ...] = ()
    registration_success_markers: tuple[str, ...] = ()
    closed_submission_markers: tuple[str, ...] = ()

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        values = (
            self.login_start_url,
            self.dashboard_url,
            self.credential_recovery_url,
            self.registration_url,
            self.submission_start_url_template,
            *self.auth_urls,
        )
        return tuple(dict.fromkeys(origin for origin in map(_origin, values) if origin))

    def allows_url(self, value: Any) -> bool:
        origin = _origin(value)
        return bool(origin and origin in self.allowed_origins)

    def trusts_registry_record(self, record: Mapping[str, Any]) -> bool:
        urls = [
            str(record.get(key) or "").strip()
            for key in ("login_url", "submission_url")
            if str(record.get(key) or "").strip()
        ]
        return bool(urls and all(self.allows_url(value) for value in urls))

    def origins_for_records(
        self,
        *records: Mapping[str, Any] | None,
    ) -> tuple[str, ...]:
        origins = list(self.allowed_origins)
        for record in records:
            if not isinstance(record, Mapping):
                continue
            for key in ("login_url", "submission_url"):
                origin = _origin(record.get(key))
                if origin and origin in self.allowed_origins and origin not in origins:
                    origins.append(origin)
        return tuple(origins)

    def target_url(
        self,
        system: Mapping[str, Any] | None,
        journal: Mapping[str, Any] | None,
    ) -> str:
        for record, key in (
            (journal, "submission_url"),
            (system, "submission_url"),
            (system, "login_url"),
        ):
            if isinstance(record, Mapping):
                value = str(record.get(key) or "").strip()
                if value and self.allows_url(value):
                    return value
        return self.dashboard_url

    def extract_journal_identifier(self, *values: Any) -> str:
        """Extract a site-specific journal id from verified local metadata."""

        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", text):
                return text
            for pattern in self.journal_identifier_patterns:
                match = re.search(pattern, text, flags=re.IGNORECASE)
                if match is None:
                    continue
                journal_id = str(match.group("journal_id") or "").strip()
                if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", journal_id):
                    return journal_id
        return ""

    def submission_start_url(self, journal_identifier: Any) -> str:
        """Build an adapter-owned journal-specific new-submission URL."""

        journal_id = str(journal_identifier or "").strip()
        if self.system_id == "wiley":
            journal_id = journal_id.upper()
        if (
            not self.submission_start_url_template
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", journal_id)
        ):
            return ""
        try:
            value = self.submission_start_url_template.format(journal_id=journal_id)
        except (KeyError, ValueError):
            return ""
        return value if self.allows_url(value) else ""

    @staticmethod
    def visible_controls(snapshot: Mapping[str, Any]) -> list[dict[str, Any]]:
        controls = snapshot.get("controls")
        if not isinstance(controls, list):
            return []
        return [
            dict(item)
            for item in controls
            if isinstance(item, Mapping) and not bool(item.get("disabled"))
        ]

    def classify_page(self, snapshot: Mapping[str, Any]) -> str:
        controls = self.visible_controls(snapshot)
        types = [str(item.get("type") or "").casefold() for item in controls]
        labels = " ".join(str(item.get("label") or "") for item in controls).casefold()
        text = str(snapshot.get("text") or "").casefold()
        origin = _origin(snapshot.get("origin"))
        if any(
            token in f"{labels} {text}"
            for token in (
                "captcha",
                "recaptcha",
                "two-factor",
                "two factor",
                "verification code",
                "one-time code",
                "security code",
            )
        ):
            return "human_verification"
        if has_explicit_terms_confirmation(snapshot):
            return "terms_confirmation"
        if self.closed_submission_markers and any(
            marker.casefold() in f"{labels} {text}"
            for marker in self.closed_submission_markers
        ):
            return "submission_portal_closed"
        control_markers = {
            str(value or "").strip().casefold()
            for item in controls
            for value in (item.get("id"), item.get("name"))
            if str(value or "").strip()
        }
        if self.registration_field_markers and all(
            marker.casefold() in control_markers
            for marker in self.registration_field_markers
        ):
            return "registration"
        password_count = types.count("password")
        combined = f"{labels} {text}"
        path = str(snapshot.get("path") or "").casefold()
        if password_count == 1 and any(
            marker.casefold() in combined
            for marker in self.password_rejected_markers
        ):
            return "password_rejected"
        if any(
            marker in combined
            for marker in (
                "password has been updated",
                "password has been reset",
                "password changed successfully",
                "\u60a8\u7684\u5bc6\u7801\u5df2\u66f4\u65b0",
                "\u5bc6\u7801\u5df2\u66f4\u65b0",
            )
        ):
            return "password_reset_completed"
        if (
            "reset-password-success" in path
            or "please check your e-mail for instructions" in combined
            or "please check your email for instructions" in combined
        ):
            return "password_reset_sent"
        exact_reset_change_control = any(
            str(item.get("action") or "").casefold() == "password_reset_continue"
            or str(item.get("id") or "").casefold() == "createnewpassword"
            or str(item.get("name") or "").casefold().startswith(
                "reset_password_new_password"
            )
            for item in controls
        )
        if password_count >= 2 and (
            exact_reset_change_control
            or "reset-password" in path
            or any(
                marker in combined
                for marker in (
                    "reset password",
                    "change password",
                    "new password",
                )
            )
        ):
            return "password_reset_change"
        if "email" in types and any(
            marker in combined
            for marker in (
                "send reset link",
                "reset password",
                "forgot password",
            )
        ):
            return "password_reset_request"
        if any(
            str(item.get("name") or "").casefold()
            == "consent_demographic_question"
            for item in controls
        ) and any(
            str(item.get("label") or "").strip().casefold()
            in {"next_button", "finish_button"}
            for item in controls
        ):
            return "profile_update"
        name_field_markers = (
            "first name",
            "given name",
            "last name",
            "family name",
            "名字",
            "姓氏",
        )
        editable_labels = " ".join(
            str(item.get("label") or "").casefold()
            for item in controls
            if str(item.get("tag") or "").casefold() in {"input", "select"}
        )
        registration_step = str(snapshot.get("registration_step") or "").casefold()
        registration_text = f"{labels} {text}"
        if registration_step.startswith("registration_step_") or (
            any(
                marker in registration_text
                for marker in (
                    "account creation",
                    "create account process",
                    "create an account",
                    "registration process",
                    "创建账户流程",
                    "创建帐户流程",
                    "创建账号流程",
                )
            )
            and any(
                marker in editable_labels
                for marker in (
                    "first name",
                    "firstname",
                    "last name",
                    "lastname",
                    "surname",
                    "user_firstname",
                    "user_lastname",
                )
            )
        ):
            return "registration"
        # A normal login page often contains a "Register" link.  That link is
        # not evidence that the visible form is a registration form.  Require
        # either two password fields or actual editable name fields.
        if password_count >= 2 or (
            password_count >= 1
            and any(marker in editable_labels for marker in name_field_markers)
        ):
            return "registration"
        if password_count == 1:
            return "password"
        # Authenticated submission forms can contain author email fields.  They
        # must not be interpreted as login forms merely because of input type.
        # Portals whose login start and dashboard share an origin must also
        # present an authenticated UI marker; an empty pre-redirect page is not
        # a session receipt.
        if self.authenticated_markers and any(
            marker.casefold() in f"{labels} {text}"
            for marker in self.authenticated_markers
        ):
            return "authenticated"
        if origin == _origin(self.dashboard_url):
            if not self.authenticated_markers:
                return "authenticated"
            return "unknown"
        if "email" in types:
            return "email"
        # CDP snapshots intentionally mark an entire credential form subtree as
        # sensitive, so its submit button and recovery link may inherit the
        # flag.  Only an editable non-password field is evidence of an unknown
        # secret/challenge after the known email/password forms were handled.
        if any(
            item.get("sensitive")
            and str(item.get("tag") or "").casefold()
            in {"input", "textarea", "select"}
            and str(item.get("type") or "").casefold()
            not in {"email", "password", "submit", "button", "hidden"}
            for item in controls
        ):
            return "human_verification"
        return "unknown"

    def registration_succeeded(self, snapshot: Mapping[str, Any]) -> bool:
        combined = " ".join(
            (
                str(snapshot.get("text") or ""),
                " ".join(
                    str(item.get("label") or "")
                    for item in self.visible_controls(snapshot)
                ),
            )
        ).casefold()
        return bool(
            self.registration_success_markers
            and any(
                marker.casefold() in combined
                for marker in self.registration_success_markers
            )
        )

    @staticmethod
    def first_control(
        snapshot: Mapping[str, Any],
        *,
        types: Iterable[str] = (),
        tags: Iterable[str] = (),
    ) -> dict[str, Any] | None:
        wanted_types = {str(item).casefold() for item in types}
        wanted_tags = {str(item).casefold() for item in tags}
        controls = SubmissionSiteAdapter.visible_controls(snapshot)
        for control in controls:
            control_type = str(control.get("type") or "").casefold()
            control_tag = str(control.get("tag") or "").casefold()
            if wanted_types and control_type not in wanted_types:
                continue
            if wanted_tags and control_tag not in wanted_tags:
                continue
            if str(control.get("ref") or ""):
                return control
        return None

    @staticmethod
    def submit_control(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        controls = SubmissionSiteAdapter.visible_controls(snapshot)
        # ScholarOne's legacy reset page renders its primary Continue action as
        # ``<a id="createNewPassword">`` without href/role/button semantics.
        # The local browser snapshot assigns this exact value-free action id;
        # arbitrary links must never become form submit controls.
        for control in controls:
            if str(control.get("action") or "").casefold() == "password_reset_continue":
                return control
        for control in controls:
            if (
                str(control.get("tag") or "").casefold() == "button"
                and str(control.get("type") or "").casefold() == "submit"
            ):
                return control
        for control in controls:
            if str(control.get("type") or "").casefold() in {"button", "submit"}:
                return control
        return None

    @staticmethod
    def registration_control(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return an explicit account-creation control from a login page."""

        phrases = (
            "create an account",
            "create account",
            "register account",
            "register now",
            "sign up",
            "创建一个帐户",
            "创建帐户",
            "创建账户",
            "注册账户",
            "注册账号",
        )
        for control in SubmissionSiteAdapter.visible_controls(snapshot):
            tag = str(control.get("tag") or "").casefold()
            control_type = str(control.get("type") or "").casefold()
            if tag not in {"a", "button", "input"}:
                continue
            if tag == "input" and control_type not in {"button", "submit"}:
                continue
            label = re.sub(
                r"\s+",
                " ",
                str(control.get("label") or "").casefold(),
            ).strip()
            if str(control.get("ref") or "") and any(
                phrase in label for phrase in phrases
            ):
                return control
        return None

    @staticmethod
    def orcid_login_control(snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
        """Return a visible ORCID sign-in/link control, never an informational link."""

        excluded = (
            "learn about",
            "what is orcid",
            "orcid policy",
            "orcid privacy",
            "orcid help",
        )
        intent = (
            "sign in",
            "signin",
            "log in",
            "login",
            "register",
            "connect",
            "link",
            "continue",
            "authenticate",
            "使用",
            "登录",
            "注册",
            "关联",
            "连接",
        )
        for control in SubmissionSiteAdapter.visible_controls(snapshot):
            tag = str(control.get("tag") or "").casefold()
            control_type = str(control.get("type") or "").casefold()
            if tag not in {"a", "button", "input"}:
                continue
            if tag == "input" and control_type not in {"button", "submit"}:
                continue
            label = re.sub(
                r"\s+",
                " ",
                " ".join(
                    str(control.get(key) or "")
                    for key in ("label", "title", "aria_label")
                ).casefold(),
            ).strip()
            if not str(control.get("ref") or "") or "orcid" not in label:
                continue
            if any(marker in label for marker in excluded):
                continue
            # ScienceConnect exposes its social-login choices as icon buttons.
            # Under translated/localized pages the accessible label can be
            # reduced to ``ORCID button`` (or ``ORCID`` followed by mojibake),
            # without a separate "sign in" verb.  A visible BUTTON whose label
            # starts with the exact provider name is still an unambiguous login
            # action; informational ORCID links remain excluded above.
            if tag == "button" and re.match(r"^orcid(?:\b|[^a-z0-9])", label):
                return control
            if any(marker in label for marker in intent) or label in {
                "orcid",
                "orcid iD".casefold(),
            }:
                return control
        return None

    @staticmethod
    def orcid_authorization_control(
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Return the affirmative control on the official ORCID OAuth page."""

        if _origin(snapshot.get("origin")) not in ORCID_AUTH_ORIGINS:
            return None
        for control in SubmissionSiteAdapter.visible_controls(snapshot):
            tag = str(control.get("tag") or "").casefold()
            control_type = str(control.get("type") or "").casefold()
            if tag not in {"a", "button", "input"}:
                continue
            if tag == "input" and control_type not in {"button", "submit"}:
                continue
            label = re.sub(
                r"\s+",
                " ",
                str(control.get("label") or "").casefold(),
            ).strip()
            if not str(control.get("ref") or ""):
                continue
            if any(token in label for token in ("deny", "cancel", "拒绝", "取消")):
                continue
            if any(
                token in label
                for token in (
                    "authorize",
                    "authorise",
                    "grant access",
                    "allow access",
                    "connect my orcid",
                    "授权",
                    "允许访问",
                )
            ):
                return control
        return None

    @staticmethod
    def registration_next_control(
        snapshot: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        current = str(snapshot.get("registration_step") or "").casefold()
        match = re.fullmatch(r"registration_step_(\d+)", current)
        wanted = f"registration_step_{int(match.group(1)) + 1}" if match else ""
        controls = SubmissionSiteAdapter.visible_controls(snapshot)
        if wanted:
            matching = [
                control
                for control in controls
                if (
                    str(control.get("ref") or "")
                    and str(control.get("action") or "").casefold() == wanted
                )
            ]
            for control in reversed(matching):
                label = str(control.get("label") or "").casefold()
                if any(
                    token in label
                    for token in ("next", "continue")
                ):
                    return control
            # ScholarOne exposes the breadcrumb and the footer Next image with
            # the same javascript action.  The actual progression controls are
            # later in DOM order, while the breadcrumb appears first.
            if matching:
                return matching[-1]
        return SubmissionSiteAdapter.submit_control(snapshot)


SPRINGER_NATURE_ADAPTER = SubmissionSiteAdapter(
    system_id="springer_nature",
    label="Springer Nature",
    login_start_url="https://submission.springernature.com/",
    dashboard_url="https://submission.springernature.com/your-submissions",
    auth_urls=(
        "https://idp.springernature.com/",
        "https://idp-personal-authenticator.springernature.com/",
        "https://my-profile.springernature.com/",
    ),
    supports_registration=True,
    submission_start_url_template=(
        "https://submission.nature.com/new-submission/{journal_id}/3"
    ),
    journal_identifier_patterns=(
        r"https?://link\.springer\.com/journal/(?P<journal_id>\d+)(?:[/?#]|\b)",
        r"https?://submission\.nature\.com/new-submission/(?P<journal_id>\d+)(?:[/?#]|\b)",
        r"(?:[?&]context_code=)(?P<journal_id>\d+)(?:&|$)",
    ),
)


SCHOLAR_ONE_ADAPTER = SubmissionSiteAdapter(
    system_id="scholar_one",
    label="ScholarOne Manuscripts",
    login_start_url="https://mc.manuscriptcentral.com/",
    dashboard_url="https://mc.manuscriptcentral.com/",
    auth_urls=("https://mc.manuscriptcentral.com/",),
    supports_registration=True,
    submission_start_url_template="https://mc.manuscriptcentral.com/{journal_id}",
    journal_identifier_patterns=(
        r"https?://mc\.manuscriptcentral\.com/(?P<journal_id>[A-Za-z0-9._-]+)(?:[/?#]|\b)",
    ),
    authenticated_markers=(
        "author center",
        "author dashboard",
        "start new submission",
        "create a new manuscript",
        "manuscripts with decisions",
        "\u9000\u51fa\u767b\u5f55",
    ),
    closed_submission_markers=(
        "closed to new submissions",
        "no longer accepting new submissions",
    ),
)


TAYLOR_FRANCIS_ADAPTER = SubmissionSiteAdapter(
    system_id="taylor_francis",
    label="Taylor & Francis Submission Portal",
    login_start_url="https://rp.tandfonline.com/",
    dashboard_url="https://rp.tandfonline.com/dashboard/",
    auth_urls=("https://accounts.taylorfrancis.com/",),
    supports_registration=True,
    authenticated_markers=("my submissions", "我的投稿"),
)


WILEY_ADAPTER = SubmissionSiteAdapter(
    system_id="wiley",
    label="Wiley Research Exchange",
    login_start_url="https://authors.wiley.com/dashboard",
    dashboard_url="https://authors.wiley.com/dashboard",
    auth_urls=(
        "https://submission.wiley.com/",
        "https://wiley.atyponrex.com/",
        "https://connect.wiley.com/",
        "https://wiley.scienceconnect.io/",
        "https://scienceconnect.io/",
    ),
    supports_registration=True,
    submission_start_url_template="https://authors.wiley.com/journal/{journal_id}",
    journal_identifier_patterns=(
        r"https?://authors\.wiley\.com/(?:dashboard/)?journal/"
        r"(?P<journal_id>[A-Za-z0-9._-]+)(?:[/?#]|\b)",
        r"https?://(?:onlinelibrary\.)?wiley\.com/doi/(?:abs/|full/)?"
        r"10\.1002/(?P<journal_id>[A-Za-z][A-Za-z0-9_-]{0,15})(?:[./?#]|\b)",
        r"\bC(?P<journal_id>CPE)\b",
    ),
    authenticated_markers=(
        "wiley authors",
        "my dashboard",
        "wiley 作者",
        "我的仪表盘",
        "开始提交",
        "稿件标题或id",
        "我的仪表盘",
        "manuscript title or id",
        "complete my submission",
        "final review",
        "additional information",
        "author details",
        "file upload",
        "稿件标题或id",
    ),
)


ELSEVIER_ADAPTER = SubmissionSiteAdapter(
    system_id="elsevier",
    label="Elsevier Editorial Manager",
    login_start_url="https://submit.elsevier.com/",
    dashboard_url="https://submit.elsevier.com/",
    auth_urls=(
        "https://id.elsevier.com/",
        "https://www.editorialmanager.com/",
    ),
    supports_registration=True,
)


INDERSCIENCE_ADAPTER = SubmissionSiteAdapter(
    system_id="inderscience",
    label="Inderscience Submissions",
    login_start_url=(
        "https://www.indersciencesubmissions.com/index.php?action=login"
    ),
    dashboard_url="https://www.indersciencesubmissions.com/session/index.php",
    auth_urls=(
        "https://www.indersciencesubmissions.com/",
        "https://indersciencesubmissions.com/",
    ),
    supports_registration=True,
    credential_recovery_url=(
        "https://www.indersciencesubmissions.com/index.php?action=forgotpassword"
    ),
    registration_url=(
        "https://www.indersciencesubmissions.com/index.php?action=authorreg"
    ),
    registration_field_markers=(
        "txtemail",
        "txtfirstname",
        "txtlastname",
        "txtusername",
        "txtpassword",
    ),
    registration_success_markers=(
        "successfully registered",
        "registration successful",
        "registration has been completed",
        "注册成功",
        "登记成功",
    ),
    authenticated_markers=(
        "author dashboard",
        "your draft submissions",
        "new submission",
        "log out",
        "logout",
        "作者仪表板",
        "草稿投稿",
        "新投稿",
        "退出登录",
        "登出",
    ),
    password_rejected_markers=(
        "we could not recognise the username and/or password",
        "we could not recognize the username and/or password",
        "cannot recognise the username and/or password",
        "cannot recognize the username and/or password",
        "无法识别您输入的用户名和/或密码",
        "无法识别你输入的用户名和/或密码",
    ),
)


_ADAPTERS = {
    SPRINGER_NATURE_ADAPTER.system_id: SPRINGER_NATURE_ADAPTER,
    SCHOLAR_ONE_ADAPTER.system_id: SCHOLAR_ONE_ADAPTER,
    TAYLOR_FRANCIS_ADAPTER.system_id: TAYLOR_FRANCIS_ADAPTER,
    WILEY_ADAPTER.system_id: WILEY_ADAPTER,
    ELSEVIER_ADAPTER.system_id: ELSEVIER_ADAPTER,
    INDERSCIENCE_ADAPTER.system_id: INDERSCIENCE_ADAPTER,
}


def submission_site_adapter(system_id: Any) -> SubmissionSiteAdapter | None:
    key = str(system_id or "").strip().casefold().replace("-", "_").replace(" ", "_")
    return _ADAPTERS.get(key)
