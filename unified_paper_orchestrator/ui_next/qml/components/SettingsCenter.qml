import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: root
    property var controllerRef
    property var settings: controllerRef ? controllerRef.settingsData : ({})
    property var codexStatus: controllerRef ? controllerRef.codexAccountStatus : ({})
    property var catalog: controllerRef ? (controllerRef.modelCatalogs[String(stageData.key || "")] || ({})) : ({})
    property var modelChoices: (catalog.models || []).map(function(item) {
        return {value: String(item.model), label: String(item.display_name || item.model)}
    })
    property string unavailableModelLabel: root.tr(root.catalog.state === "ready"
        ? "settings.model_unavailable" : "settings.model_unverified")
    function reasoningChoices(model) {
        var models = root.catalog.models || []
        var selected = models.find(function(item) { return item.model === model || item.id === model })
        return selected ? (selected.supported_reasoning_efforts || []).map(function(effort) {
            return {value: String(effort), label: String(effort)}
        }) : []
    }
    function refreshModels() {
        if (root.controllerRef) root.controllerRef.refreshModels(String(root.stageData.key || ""))
    }
    function catalogStatusText() {
        var state = root.catalog.state
        var key = state === "ready" ? "settings.models_ready"
            : state === "loading" ? "settings.models_loading"
            : state === "stale" ? "settings.models_stale" : "settings.models_error"
        var text = root.tr(key)
        if (root.catalog.runtimeVersion) {
            text += "\nCodex " + root.catalog.runtimeVersion + " · SDK " + root.catalog.sdkVersion
        }
        if (root.catalog.cacheFetchedAt) {
            text += " · " + root.tr("settings.models_updated") + " "
                + new Date(root.catalog.cacheFetchedAt).toLocaleString(Qt.locale())
        }
        return text
    }
    property int stageIndex: 0
    Popup {
        id: undergraduateFigurePreview
        anchors.centerIn: parent; width: Math.min(880, parent.width - 40); height: parent.height - 60; modal: true; padding: 18
        background: Rectangle { color: Theme.surface; radius: Theme.radius; border.color: Theme.border }
        contentItem: ColumnLayout {
            Text { text: "本科配图方案预览 · 随论文类型绑定，不改变当前期刊设置"; color: Theme.text; font.family: Theme.fontFamily }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true
                TextArea { id: undergraduateFigureText; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: Theme.text; font.family: Theme.fontFamily }
            }
            ModernButton { text: "关闭"; Layout.alignment: Qt.AlignRight; onClicked: undergraduateFigurePreview.close() }
        }
    }
    property string commercialFeedback: ""
    property string codexFeedback: ""
    property var stageData: {
        var stages = settings.stages || []
        return stages.length > stageIndex ? stages[stageIndex] : ({})
    }

    function tr(key) { return i18n.text(key, i18n.revision) }
    function codexStatusText() {
        if (Boolean(root.codexStatus.loginPending))
            return root.tr("settings.codex_status_pending")
        if (!Boolean(root.codexStatus.statusKnown))
            return root.tr("settings.codex_status_unknown")
        if (Boolean(root.codexStatus.authenticated))
            return root.tr("settings.codex_status_connected")
        if (root.codexStatus.ok === false)
            return root.tr("settings.codex_status_unavailable")
        return root.tr("settings.codex_status_disconnected")
    }
    function codexStatusTone() {
        if (Boolean(root.codexStatus.loginPending))
            return "info"
        if (Boolean(root.codexStatus.authenticated))
            return "success"
        if (root.codexStatus.ok === false)
            return "danger"
        return "secondary"
    }
    function syncStageControls() {
        var item = root.stageData
        parallelBox.text = String(Number(item.parallel || 1))
        defaultModel.selectedValue = String(item.defaultModel || "")
        defaultReasoning.selectedValue = String(item.defaultReasoning || "")
        var schemes = item.promptSchemes || []
        promptScheme.currentIndex = Math.max(0, schemes.findIndex(function(value) {
            return value.id === String(item.promptScheme || "journal")
        }))
    }
    function syncPolicyControls() {
        var gate = root.settings.writingGate || ({})
        var monitor = root.settings.smartMonitor || ({})
        var policy = root.settings.smartPolicy || ({})
        gateMode.currentIndex = String(gate.mode || "strict") === "auto_trust" ? 1 : 0
        monitorTime.text = String(monitor.time || "09:00")
        smartAccountPolicy.currentIndex = Math.max(
            0,
            root.accountPolicyValues.indexOf(String(policy.accountMissingPolicy || "auto_register"))
        )
        smartPreferOrcid.checked = policy.preferOrcid === undefined ? true : Boolean(policy.preferOrcid)
        smartAuthorizeOrcid.checked = policy.autoAuthorizeOrcid === undefined
                                     ? true : Boolean(policy.autoAuthorizeOrcid)
        smartTerms.checked = policy.requireTermsConfirmation === undefined
                             ? true : Boolean(policy.requireTermsConfirmation)
        smartAutoSubmit.checked = policy.autoFinalSubmit === undefined
                                  ? true : Boolean(policy.autoFinalSubmit)
    }
    function syncAllControls() {
        syncStageControls()
        syncPolicyControls()
        var commercial = root.settings.commercial || ({})
        commercialEnabled.checked = Boolean(commercial.enabled)
        commercialUrl.text = String(commercial.controlPlaneUrl || "")
        commercialChannel.currentIndex = String(commercial.releaseChannel || "stable") === "preview" ? 1 : 0
        commercialDiagnostics.checked = Boolean(commercial.diagnosticsOptIn)
        commercialCustomerDiagnostics.checked = Boolean(commercial.diagnosticsOptIn)
        commercialDevelopment.checked = Boolean(commercial.developmentMode)
        commercialKeyPath.text = String(commercial.developmentPublicKeyPath || "")
    }

    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(980, parent ? parent.width - 56 : 980)
    height: Math.min(760, parent ? parent.height - 56 : 760)
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape
    padding: 0
    property var accountPolicyValues: ["auto_register", "ask", "block"]

    onOpened: {
        root.commercialFeedback = ""
        root.codexFeedback = ""
        syncAllControls()
        if (root.controllerRef)
            root.controllerRef.refreshCodexAccount()
        root.refreshModels()
        if (Boolean(root.settings.commercial
                    ? root.settings.commercial.signedIn
                    : false)) {
            root.controllerRef.refreshCommercialAccount()
        }
    }
    Connections {
        target: root.controllerRef
        function onSettingsDataChanged() { root.syncAllControls() }
        function onCommercialActionCompleted(action, result) {
            if (String(action) === "commercial:request_code") {
                if (result.development_code) {
                    commercialCode.text = String(result.development_code)
                }
                root.commercialFeedback = result.development_code
                    ? root.tr("settings.commercial_code_ready")
                    : root.tr("settings.commercial_code_sent")
                return
            }
            if (String(action) === "commercial:check_update") {
                root.commercialFeedback = result.available
                    ? root.tr("settings.commercial_update_available") + " " + String(result.version || "")
                    : String(result.reason || root.tr("settings.commercial_no_update"))
                return
            }
            if (String(action) === "commercial:flush_diagnostics") {
                root.commercialFeedback = root.tr("settings.commercial_diagnostics_result")
                    .replace("{sent}", String(result.sent || 0))
                    .replace("{remaining}", String(result.remaining || 0))
                return
            }
            root.commercialFeedback = String(result.reason || root.tr("settings.commercial_action_done"))
        }
        function onCodexAccountActionCompleted(action, result) {
            var key = String(action || "")
            if (key === "codex:login_started") {
                root.codexFeedback = root.tr(root.controllerRef.codexLoginCode
                    ? "settings.codex_device_opened" : "settings.codex_login_opened")
            } else if (key === "codex:login_succeeded") {
                root.codexFeedback = root.tr("settings.codex_login_success")
            } else if (key === "codex:login_failed") {
                root.codexFeedback = root.tr(result.error_code === "CODEX_LOGIN_TIMED_OUT"
                    ? "settings.codex_login_timeout" : "settings.codex_login_failed")
            } else if (key === "codex:login_cancelled") {
                root.codexFeedback = root.tr("settings.codex_login_cancelled")
            } else if (key === "codex:logout") {
                root.codexFeedback = root.tr("settings.codex_logout_success")
            }
            if (key === "codex:login_succeeded" || (key === "codex:status" && result.authenticated)) root.refreshModels()
        }
    }

    background: Rectangle {
        radius: Theme.radiusLarge
        color: Theme.surface
        border.width: 1
        border.color: Theme.border
    }

    contentItem: ColumnLayout {
        spacing: 0
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 70
            Layout.leftMargin: 22
            Layout.rightMargin: 18
            Text {
                Layout.fillWidth: true
                text: root.tr("settings.title")
                color: Theme.text
                font.family: Theme.fontFamily
                font.pixelSize: 19
                font.weight: Font.DemiBold
            }
            ModernButton {
                text: root.tr("action.close")
                compact: true
                quiet: true
                onClicked: root.close()
            }
        }
        Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.border }
        TabBar {
            id: settingsTabs
            objectName: "settingsTabs"
            implicitWidth: 0
            Layout.fillWidth: true
            Layout.preferredHeight: 46
            background: Rectangle { color: Theme.surfaceSoft }
            Repeater {
                model: [
                    root.tr("settings.stage_tab"),
                    root.tr("settings.smart_tab"),
                    root.tr("settings.codex_tab"),
                    root.tr("settings.gate_tab"),
                    root.tr("settings.monitor_tab"),
                    root.tr("settings.author_tab"),
                    root.tr("settings.commercial_tab")
                ]
                TabButton {
                    required property var modelData
                    width: settingsTabs.width / 7
                    text: String(modelData)
                    font.family: Theme.fontFamily
                    font.pixelSize: 13
                }
            }
        }
        StackLayout {
            Layout.fillWidth: true
            Layout.fillHeight: true
            currentIndex: settingsTabs.currentIndex

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 18
                    spacing: 12
                    RowLayout {
                        visible: String(root.stageData.key || "") === "figures"
                        Layout.fillWidth: true
                        Text {
                            Layout.fillWidth: true; wrapMode: Text.WordWrap; font.family: Theme.fontFamily; font.pixelSize: 12; color: Theme.textSecondary
                            text: "本科配图随每篇论文的类型、学校要求和材料自动绑定。下方编辑的是原期刊配置；本科方案可在此预览。"
                        }
                        ModernButton {
                            text: "本科人文社科"; compact: true
                            onClicked: { undergraduateFigureText.text = root.controllerRef.previewUndergraduateFigures("undergraduate_humanities"); undergraduateFigurePreview.open() }
                        }
                        ModernButton {
                            text: "本科计算机软件"; compact: true
                            onClicked: { undergraduateFigureText.text = root.controllerRef.previewUndergraduateFigures("undergraduate_software"); undergraduateFigurePreview.open() }
                        }
                    }
                    RowLayout {
                        visible: String(root.stageData.key || "") === "writing"
                        Layout.fillWidth: true
                        spacing: 10
                        Text { text: root.tr("undergraduate.scheme"); color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        ModernComboBox {
                            id: promptScheme
                            objectName: "writingPromptScheme"
                            Layout.preferredWidth: 230
                            model: (root.stageData.promptSchemes || []).map(function(item) { return item.name })
                        }
                        ModernButton {
                            objectName: "applyWritingPromptScheme"
                            text: root.tr("undergraduate.apply")
                            compact: true
                            tone: "primary"
                            enabled: (root.stageData.promptSchemes || []).length > 0
                            onClicked: root.controllerRef.runWorkflowAction("writing", "scheme", "",
                                String(root.stageData.promptSchemes[promptScheme.currentIndex].id))
                        }
                        Text {
                            Layout.fillWidth: true
                            text: root.tr("undergraduate.scheme_hint")
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 11
                            wrapMode: Text.WordWrap
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10
                        Text { text: root.tr("settings.stage"); color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        ModernComboBox {
                            id: stageSelector
                            Layout.preferredWidth: 190
                            model: (root.settings.stages || []).map(function(item) { return item.label })
                            currentIndex: root.stageIndex
                            onActivated: {
                                root.stageIndex = currentIndex
                                root.syncStageControls()
                                root.refreshModels()
                            }
                        }
                        Text { text: root.tr("settings.parallel"); color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        ModernTextField {
                            id: parallelBox
                            Layout.preferredWidth: 72
                            horizontalAlignment: TextInput.AlignHCenter
                            inputMethodHints: Qt.ImhDigitsOnly
                            validator: IntValidator {
                                bottom: 1
                                top: Number(root.stageData.parallelMax || 10)
                            }
                        }
                        SdkChoice {
                            id: defaultModel
                            Layout.fillWidth: true
                            choices: root.modelChoices
                            allowEmpty: false
                            unavailableLabel: root.unavailableModelLabel
                            enabled: root.catalog.state === "ready"
                        }
                        SdkChoice {
                            id: defaultReasoning
                            Layout.preferredWidth: 128
                            choices: root.reasoningChoices(defaultModel.selectedValue)
                            emptyLabel: root.tr("settings.reasoning_default")
                            unavailableLabel: root.unavailableModelLabel
                            enabled: root.catalog.state === "ready" && defaultModel.selectionAvailable
                        }
                        ModernButton {
                            text: root.tr("action.save")
                            iconName: "check"
                            tone: "primary"
                            compact: true
                            enabled: root.catalog.state === "ready" && defaultModel.selectionAvailable && defaultReasoning.selectionAvailable && parallelBox.acceptableInput
                            onClicked: root.controllerRef.saveStageSettings(
                                String(root.stageData.key || ""),
                                Number(parallelBox.text || 1),
                                defaultModel.selectedValue,
                                defaultReasoning.selectedValue
                            )
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        Text {
                            Layout.fillWidth: true
                            text: root.catalogStatusText()
                            color: root.catalog.state === "ready" ? Theme.textSecondary : Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                            wrapMode: Text.WordWrap
                        }
                        ModernButton {
                            text: root.tr("settings.models_refresh")
                            iconName: "refresh"
                            compact: true
                            enabled: root.catalog.state !== "loading"
                            onClicked: root.refreshModels()
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 50
                        radius: Theme.radiusSmall
                        color: Theme.surfaceSoft
                        border.width: 1
                        border.color: Theme.border
                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 8
                            spacing: 8
                            Text { text: root.tr("settings.workflow") + " · " + String(root.stageData.stepCount || 0) + " " + root.tr("settings.steps"); color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                            Item { Layout.fillWidth: true }
                            ModernTextField {
                                id: newStepName
                                Layout.preferredWidth: 230
                                placeholderText: root.tr("settings.new_step_name")
                                font.family: Theme.fontFamily
                                selectByMouse: true
                            }
                            ModernButton {
                                text: root.tr("settings.add_step")
                                iconName: "plus"
                                compact: true
                                enabled: newStepName.text.trim().length > 0
                                onClicked: {
                                    root.controllerRef.runWorkflowAction(
                                        String(root.stageData.key || ""),
                                        "add",
                                        "",
                                        newStepName.text
                                    )
                                    newStepName.text = ""
                                }
                            }
                            ModernButton {
                                text: root.tr("settings.restore_workflow")
                                iconName: "refresh"
                                compact: true
                                onClicked: root.controllerRef.runWorkflowAction(
                                    String(root.stageData.key || ""),
                                    "restore",
                                    "",
                                    ""
                                )
                            }
                        }
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 38
                        color: Theme.surfaceSoft
                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 10
                            anchors.rightMargin: 10
                            spacing: 8
                            Text { text: "Step"; Layout.preferredWidth: 44; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11 }
                            Text { text: root.tr("settings.step_name"); Layout.fillWidth: true; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11 }
                            Text { text: root.tr("settings.model_override"); Layout.preferredWidth: 155; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11 }
                            Text { text: root.tr("settings.reasoning"); Layout.preferredWidth: 112; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11 }
                            Text { text: root.tr("settings.token_limit"); Layout.preferredWidth: 105; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11 }
                            Item { Layout.preferredWidth: 164 }
                        }
                    }
                    ListView {
                        id: stepList
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        clip: true
                        spacing: 6
                        model: root.stageData.steps || []
                        ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                        delegate: Rectangle {
                            id: stepRow
                            required property var modelData
                            width: stepList.width
                            height: 52
                            radius: Theme.radiusSmall
                            color: Theme.surface
                            border.width: 1
                            border.color: Theme.border
                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 8
                                spacing: 8
                                Text { text: String(stepRow.modelData.position || ""); Layout.preferredWidth: 44; color: Theme.primary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 1
                                    Text { Layout.fillWidth: true; text: String(stepRow.modelData.name || ""); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 12; elide: Text.ElideRight }
                                    Text { Layout.fillWidth: true; text: String(stepRow.modelData.effectiveModel || ""); color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 9; elide: Text.ElideRight }
                                }
                                SdkChoice {
                                    id: stepModel
                                    Layout.preferredWidth: 155
                                    selectedValue: String(stepRow.modelData.model || "")
                                    choices: root.modelChoices
                                    emptyLabel: root.tr("settings.inherit")
                                    unavailableLabel: root.unavailableModelLabel
                                    enabled: root.catalog.state === "ready"
                                }
                                SdkChoice {
                                    id: stepReasoning
                                    Layout.preferredWidth: 112
                                    selectedValue: String(stepRow.modelData.reasoning || "")
                                    choices: root.reasoningChoices(stepModel.selectedValue || String(root.stageData.defaultModel || root.catalog.configuredModel || ""))
                                    emptyLabel: root.tr("settings.inherit")
                                    unavailableLabel: root.unavailableModelLabel
                                    enabled: root.catalog.state === "ready" && stepModel.selectionAvailable
                                }
                                ModernTextField {
                                    id: stepToken
                                    Layout.preferredWidth: 105
                                    text: String(stepRow.modelData.tokenLimit || 0)
                                    validator: IntValidator { bottom: 0; top: 100000000 }
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    selectByMouse: true
                                }
                                ModernButton {
                                    text: root.tr("settings.prompt")
                                    iconName: "edit"
                                    compact: true
                                    minimumButtonWidth: 72
                                    onClicked: root.controllerRef.openPromptEditor(
                                        String(root.stageData.key || ""),
                                        String(stepRow.modelData.stepId || ""),
                                        String(stepRow.modelData.kind || ""),
                                        String(stepRow.modelData.promptPath || ""),
                                        String(stepRow.modelData.name || "")
                                    )
                                }
                                ModernButton {
                                    text: root.tr("action.save")
                                    iconName: "check"
                                    compact: true
                                    minimumButtonWidth: 62
                                    enabled: root.catalog.state === "ready" && stepModel.selectionAvailable && stepReasoning.selectionAvailable && stepToken.acceptableInput
                                    onClicked: root.controllerRef.saveStepProfile(
                                        String(root.stageData.key || ""),
                                        String(stepRow.modelData.stepId || ""),
                                        stepModel.selectedValue,
                                        stepReasoning.selectedValue,
                                        Number(stepToken.text || 0)
                                    )
                                }
                                ModernButton {
                                    visible: String(stepRow.modelData.kind || "") === "custom"
                                    text: "↑"
                                    compact: true
                                    minimumButtonWidth: 34
                                    onClicked: root.controllerRef.runWorkflowAction(String(root.stageData.key || ""), "up", String(stepRow.modelData.stepId || ""), "")
                                }
                                ModernButton {
                                    visible: String(stepRow.modelData.kind || "") === "custom"
                                    text: "↓"
                                    compact: true
                                    minimumButtonWidth: 34
                                    onClicked: root.controllerRef.runWorkflowAction(String(root.stageData.key || ""), "down", String(stepRow.modelData.stepId || ""), "")
                                }
                                ModernButton {
                                    visible: String(stepRow.modelData.kind || "") === "custom"
                                    text: "×"
                                    tone: "danger"
                                    compact: true
                                    minimumButtonWidth: 34
                                    onClicked: root.controllerRef.runWorkflowAction(String(root.stageData.key || ""), "remove", String(stepRow.modelData.stepId || ""), "")
                                }
                            }
                        }
                    }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 24
                    spacing: 14
                    Text { text: root.tr("settings.smart_title"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 17; font.weight: Font.DemiBold }
                    Text {
                        Layout.fillWidth: true
                        text: root.tr("settings.smart_description")
                        wrapMode: Text.WordWrap
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                        lineHeight: 1.35
                    }
                    RowLayout {
                        Text { text: root.tr("settings.account_missing_policy"); color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        ModernComboBox {
                            id: smartAccountPolicy
                            Layout.preferredWidth: 260
                            model: [
                                root.tr("settings.account_auto_register"),
                                root.tr("settings.account_ask"),
                                root.tr("settings.account_block")
                            ]
                            currentIndex: 0
                        }
                    }
                    CheckBox {
                        id: smartPreferOrcid
                        text: root.tr("settings.prefer_orcid")
                        checked: true
                        font.family: Theme.fontFamily
                    }
                    CheckBox {
                        id: smartAuthorizeOrcid
                        text: root.tr("settings.auto_authorize_orcid")
                        checked: true
                        font.family: Theme.fontFamily
                    }
                    CheckBox {
                        id: smartTerms
                        text: root.tr("settings.require_terms")
                        checked: true
                        font.family: Theme.fontFamily
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: Theme.border
                    }
                    CheckBox {
                        id: smartAutoSubmit
                        text: root.tr("settings.auto_final_submit")
                        checked: true
                        font.family: Theme.fontFamily
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.tr("settings.auto_final_submit_note")
                        wrapMode: Text.WordWrap
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                        lineHeight: 1.3
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        ModernButton {
                            text: root.tr("action.save")
                            iconName: "check"
                            tone: "primary"
                            onClicked: root.controllerRef.saveSmartPolicies(
                                String(root.accountPolicyValues[smartAccountPolicy.currentIndex]),
                                smartTerms.checked,
                                smartPreferOrcid.checked,
                                smartAuthorizeOrcid.checked,
                                smartAutoSubmit.checked
                            )
                        }
                        ModernButton {
                            text: root.tr("action.accounts_sites")
                            iconName: "account"
                            onClicked: root.controllerRef.openAccountCenter()
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 24
                    spacing: 14

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 10
                        Rectangle {
                            Layout.preferredWidth: 38
                            Layout.preferredHeight: 38
                            radius: 10
                            color: Theme.primarySoft
                            Icon {
                                anchors.centerIn: parent
                                name: "robot"
                                iconSize: 20
                                iconColor: Theme.primary
                            }
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 2
                            Text {
                                text: root.tr("settings.codex_title")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: 17
                                font.weight: Font.DemiBold
                            }
                            Text {
                                Layout.fillWidth: true
                                text: root.tr("settings.codex_description")
                                wrapMode: Text.WordWrap
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                                lineHeight: 1.3
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 144
                        radius: Theme.radius
                        color: Theme.surfaceSoft
                        border.width: 1
                        border.color: Theme.border

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 14
                            spacing: 10
                            RowLayout {
                                Layout.fillWidth: true
                                Text {
                                    Layout.fillWidth: true
                                    text: root.tr("settings.codex_local_account")
                                    color: Theme.text
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 14
                                    font.weight: Font.DemiBold
                                }
                                StatusBadge {
                                    label: root.codexStatusText()
                                    tone: root.codexStatusTone()
                                }
                            }
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 1
                                color: Theme.border
                            }
                            GridLayout {
                                Layout.fillWidth: true
                                columns: 2
                                columnSpacing: 34
                                rowSpacing: 8
                                Text {
                                    text: root.tr("settings.codex_auth_mode") + ": "
                                          + String(root.codexStatus.auth_mode || "-")
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 12
                                }
                                Text {
                                    text: root.tr("settings.codex_plan") + ": "
                                          + String(root.codexStatus.plan_type || "-")
                                    color: Theme.textSecondary
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 12
                                }
                                Text {
                                    Layout.columnSpan: 2
                                    Layout.fillWidth: true
                                    text: root.tr("settings.codex_runtime")
                                    color: Theme.textMuted
                                    font.family: Theme.fontFamily
                                    font.pixelSize: 11
                                    wrapMode: Text.WordWrap
                                }
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 9
                        ModernButton {
                            objectName: "connectCodexAccount"
                            visible: !Boolean(root.codexStatus.authenticated)
                            text: root.tr("settings.codex_connect")
                            iconName: "account"
                            tone: "primary"
                            enabled: !Boolean(root.codexStatus.loginPending)
                            onClicked: root.controllerRef.connectCodexAccount()
                        }
                        ModernButton {
                            objectName: "connectCodexDeviceAccount"
                            visible: !Boolean(root.codexStatus.authenticated)
                            text: root.tr("settings.codex_device_connect")
                            iconName: "account"
                            enabled: !Boolean(root.codexStatus.loginPending)
                            onClicked: root.controllerRef.connectCodexDeviceAccount()
                        }
                        ModernButton {
                            objectName: "cancelCodexLogin"
                            visible: Boolean(root.codexStatus.loginPending)
                            enabled: Boolean(root.codexStatus.canCancelLogin)
                            text: root.tr("settings.codex_cancel_login")
                            onClicked: root.controllerRef.cancelCodexLogin()
                        }
                        ModernButton {
                            objectName: "refreshCodexAccount"
                            text: root.tr("settings.codex_refresh")
                            iconName: "refresh"
                            onClicked: root.controllerRef.refreshCodexAccount()
                        }
                        ModernButton {
                            visible: Boolean(root.codexStatus.authenticated)
                            text: root.tr("settings.codex_disconnect")
                            iconName: "logout"
                            enabled: !Boolean(root.codexStatus.loginPending)
                            onClicked: root.controllerRef.disconnectCodexAccount()
                        }
                        Item { Layout.fillWidth: true }
                    }

                    Text {
                        objectName: "codexDeviceCode"
                        Layout.fillWidth: true
                        visible: Boolean(root.codexStatus.loginPending) && text.length > 0
                        text: root.controllerRef ? String(root.controllerRef.codexLoginCode || "") : ""
                        font.family: Theme.fontFamily; font.pixelSize: 24; font.weight: Font.DemiBold
                        color: Theme.text
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: deviceNote.implicitHeight + 28
                        radius: Theme.radius
                        color: Theme.primarySoft
                        border.width: 1
                        border.color: Qt.rgba(
                            Theme.primary.r,
                            Theme.primary.g,
                            Theme.primary.b,
                            0.18
                        )
                        RowLayout {
                            anchors.fill: parent
                            anchors.margins: 14
                            spacing: 10
                            Icon {
                                name: "shield"
                                iconSize: 18
                                iconColor: Theme.primary
                            }
                            Text {
                                id: deviceNote
                                Layout.fillWidth: true
                                text: root.tr("settings.codex_device_note")
                                wrapMode: Text.WordWrap
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 12
                                lineHeight: 1.35
                            }
                        }
                    }

                    Text {
                        Layout.fillWidth: true
                        visible: root.codexFeedback.length > 0
                        text: root.codexFeedback
                        wrapMode: Text.WordWrap
                        color: Boolean(root.codexStatus.authenticated)
                               ? Theme.success : Theme.primary
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                    }
                    Item { Layout.fillHeight: true }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 24
                    spacing: 14
                    Text { text: root.tr("settings.gate_title"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 17; font.weight: Font.DemiBold }
                    Text { Layout.fillWidth: true; text: root.tr("settings.gate_description"); wrapMode: Text.WordWrap; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 12; lineHeight: 1.35 }
                    ModernComboBox {
                        id: gateMode
                        Layout.preferredWidth: 320
                        model: [root.tr("settings.gate_strict"), root.tr("settings.gate_trust")]
                        currentIndex: 0
                    }
                    ModernTextField { id: gatePassword; Layout.preferredWidth: 380; placeholderText: root.tr("settings.password"); echoMode: TextInput.Password }
                    ModernTextField { id: gateConfirmation; Layout.preferredWidth: 380; placeholderText: root.tr("settings.password_confirmation"); echoMode: TextInput.Password }
                    ModernButton {
                        text: root.tr("action.save")
                        iconName: "check"
                        tone: "primary"
                        onClicked: {
                            root.controllerRef.saveWritingGate(
                                gateMode.currentIndex === 1 ? "auto_trust" : "strict_confirmation",
                                gatePassword.text,
                                gateConfirmation.text
                            )
                            gatePassword.text = ""
                            gateConfirmation.text = ""
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 24
                    spacing: 14
                    Text { text: root.tr("settings.monitor_title"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 17; font.weight: Font.DemiBold }
                    Text { Layout.fillWidth: true; text: root.tr("settings.monitor_description"); wrapMode: Text.WordWrap; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 12; lineHeight: 1.35 }
                    ModernTextField {
                        id: monitorTime
                        Layout.preferredWidth: 180
                        text: "09:00"
                        placeholderText: "HH:MM"
                        font.family: Theme.monoFontFamily
                        selectByMouse: true
                    }
                    RowLayout {
                        ModernButton { text: root.tr("action.save"); iconName: "check"; tone: "primary"; onClicked: root.controllerRef.saveSmartMonitorTime(monitorTime.text) }
                        ModernButton { text: root.tr("action.check_all_submissions"); iconName: "refresh"; onClicked: root.controllerRef.prepareAction("check_all_submissions") }
                    }
                    Text {
                        text: root.tr("settings.last_monitor") + ": "
                              + String(root.settings.smartMonitor ? root.settings.smartMonitor.lastStatus : "-")
                              + " · " + String(root.settings.smartMonitor ? root.settings.smartMonitor.lastFinishedAt : "")
                        color: Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                    }
                    Item { Layout.fillHeight: true }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 24
                    spacing: 12
                    Text { text: root.tr("settings.author_title"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 17; font.weight: Font.DemiBold }
                    Text { Layout.fillWidth: true; text: root.tr("settings.author_description"); wrapMode: Text.WordWrap; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 12 }
                    Repeater {
                        model: root.settings.authorTemplates || []
                        delegate: Rectangle {
                            required property var modelData
                            Layout.fillWidth: true
                            Layout.preferredHeight: 66
                            radius: Theme.radius
                            color: Theme.surfaceSoft
                            border.width: 1
                            border.color: Theme.border
                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 12
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    Text { text: String(modelData.name || ""); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.DemiBold }
                                    Text { text: String(modelData.status || "") + (modelData.isDefault ? " · " + root.tr("settings.default_template") : ""); color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11 }
                                }
                                ModernButton {
                                    text: root.tr("settings.edit_template")
                                    iconName: "edit"
                                    compact: true
                                    onClicked: root.controllerRef.openAuthorTemplateEditor(String(modelData.templateId || ""))
                                }
                            }
                        }
                    }
                    Item { Layout.fillHeight: true }
                }
            }

            Item {
                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 24
                    spacing: 11

                    Text {
                        text: root.tr("settings.commercial_title")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: 17
                        font.weight: Font.DemiBold
                    }
                    Text {
                        Layout.fillWidth: true
                        text: Boolean(root.settings.commercial
                                      ? root.settings.commercial.configurationLocked
                                      : false)
                              ? root.tr("settings.commercial_customer_description")
                              : root.tr("settings.commercial_description")
                        wrapMode: Text.WordWrap
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                    }
                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 118
                        radius: Theme.radius
                        color: Theme.surfaceSoft
                        border.width: 1
                        border.color: Theme.border
                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 12
                            spacing: 4
                            Text {
                                text: root.tr("settings.commercial_status") + ": "
                                      + String(root.settings.commercial ? root.settings.commercial.licenseState : "not_required")
                                      + " · " + String(root.settings.commercial ? root.settings.commercial.plan : "local")
                                color: Theme.text
                                font.family: Theme.fontFamily
                                font.pixelSize: 13
                                font.weight: Font.DemiBold
                            }
                            Text {
                                visible: Boolean(root.settings.commercial
                                                 ? root.settings.commercial.signedIn
                                                 : false)
                                text: root.tr("settings.commercial_signed_in")
                                      + " " + String(root.settings.commercial
                                                     ? root.settings.commercial.emailHint
                                                     : "")
                                color: Theme.textSecondary
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                            }
                            Text {
                                visible: Boolean(root.settings.commercial)
                                         && Number(root.settings.commercial.maxConcurrentPapers || 0) > 0
                                text: root.tr("settings.commercial_limits")
                                      .replace("{papers}", String((root.settings.commercial || {}).maxConcurrentPapers || 0))
                                      .replace("{devices}", String((root.settings.commercial || {}).maxDevices || 0))
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                            }
                            Text {
                                Layout.fillWidth: true
                                text: String(root.settings.commercial ? root.settings.commercial.reason : "")
                                elide: Text.ElideRight
                                color: Theme.textMuted
                                font.family: Theme.fontFamily
                                font.pixelSize: 11
                            }
                        }
                    }
                    RowLayout {
                        visible: !Boolean(root.settings.commercial
                                          ? root.settings.commercial.configurationLocked
                                          : false)
                        Layout.fillWidth: true
                        CheckBox {
                            id: commercialEnabled
                            text: root.tr("settings.commercial_enabled")
                            font.family: Theme.fontFamily
                        }
                        CheckBox {
                            id: commercialDiagnostics
                            text: root.tr("settings.commercial_diagnostics")
                            font.family: Theme.fontFamily
                        }
                        Item { Layout.fillWidth: true }
                        Text {
                            text: root.tr("settings.commercial_channel")
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                        }
                        ModernComboBox {
                            id: commercialChannel
                            Layout.preferredWidth: 130
                            model: ["Stable", "Preview"]
                        }
                    }
                    ModernTextField {
                        id: commercialUrl
                        visible: !Boolean(root.settings.commercial
                                          ? root.settings.commercial.configurationLocked
                                          : false)
                        Layout.fillWidth: true
                        placeholderText: root.tr("settings.commercial_url")
                        selectByMouse: true
                    }
                    RowLayout {
                        visible: !Boolean(root.settings.commercial
                                          ? root.settings.commercial.configurationLocked
                                          : false)
                        Layout.fillWidth: true
                        CheckBox {
                            id: commercialDevelopment
                            visible: Boolean(root.settings.commercial ? root.settings.commercial.developmentAllowed : false)
                            text: root.tr("settings.commercial_development")
                            font.family: Theme.fontFamily
                        }
                        ModernTextField {
                            id: commercialKeyPath
                            Layout.fillWidth: true
                            visible: commercialDevelopment.visible
                            enabled: commercialDevelopment.checked
                            placeholderText: root.tr("settings.commercial_key_path")
                            selectByMouse: true
                        }
                        ModernButton {
                            text: root.tr("action.save")
                            iconName: "check"
                            tone: "primary"
                            onClicked: root.controllerRef.configureCommercial(
                                commercialEnabled.checked,
                                commercialUrl.text,
                                commercialChannel.currentIndex === 1 ? "preview" : "stable",
                                commercialDiagnostics.checked,
                                commercialDevelopment.checked,
                                commercialKeyPath.text
                            )
                        }
                    }
                    RowLayout {
                        visible: Boolean(root.settings.commercial
                                         ? root.settings.commercial.configurationLocked
                                         : false)
                        Layout.fillWidth: true
                        CheckBox {
                            id: commercialCustomerDiagnostics
                            text: root.tr("settings.commercial_diagnostics")
                            checked: Boolean(root.settings.commercial
                                             ? root.settings.commercial.diagnosticsOptIn
                                             : false)
                            font.family: Theme.fontFamily
                        }
                        Item { Layout.fillWidth: true }
                        ModernButton {
                            text: root.tr("action.save")
                            iconName: "check"
                            onClicked: root.controllerRef.updateCommercialPreferences(
                                commercialCustomerDiagnostics.checked
                            )
                        }
                        ModernButton {
                            text: root.tr("settings.commercial_account_center")
                            iconName: "external"
                            onClicked: root.controllerRef.openCommercialUrl("account")
                        }
                        ModernButton {
                            text: root.tr("settings.commercial_choose_plan")
                            iconName: "external"
                            tone: "primary"
                            onClicked: root.controllerRef.openCommercialUrl("pricing")
                        }
                    }
                    Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: Theme.border }
                    RowLayout {
                        visible: !Boolean(root.settings.commercial
                                          ? root.settings.commercial.signedIn
                                          : false)
                        Layout.fillWidth: true
                        ModernTextField {
                            id: commercialEmail
                            Layout.fillWidth: true
                            placeholderText: root.tr("settings.commercial_email")
                            selectByMouse: true
                        }
                        ModernButton {
                            text: root.tr("settings.commercial_request_code")
                            iconName: "forward"
                            onClicked: root.controllerRef.requestCommercialLoginCode(commercialEmail.text)
                        }
                    }
                    RowLayout {
                        visible: !Boolean(root.settings.commercial
                                          ? root.settings.commercial.signedIn
                                          : false)
                        Layout.fillWidth: true
                        ModernTextField {
                            id: commercialCode
                            Layout.fillWidth: true
                            placeholderText: root.tr("settings.commercial_code")
                            selectByMouse: true
                        }
                        ModernButton {
                            text: root.tr("settings.commercial_activate")
                            iconName: "account"
                            tone: "primary"
                            onClicked: root.controllerRef.completeCommercialLogin(
                                commercialEmail.text,
                                commercialCode.text
                            )
                        }
                    }
                    RowLayout {
                        Layout.fillWidth: true
                        ModernButton {
                            text: root.tr("settings.commercial_refresh")
                            iconName: "refresh"
                            onClicked: root.controllerRef.refreshCommercialLicense()
                        }
                        ModernButton {
                            text: root.tr("settings.commercial_check_update")
                            iconName: "download"
                            onClicked: root.controllerRef.checkCommercialUpdate()
                        }
                        ModernButton {
                            text: root.tr("settings.commercial_send_diagnostics")
                            iconName: "forward"
                            enabled: Boolean(root.settings.commercial
                                             ? root.settings.commercial.configurationLocked
                                             : false)
                                     ? commercialCustomerDiagnostics.checked
                                     : commercialDiagnostics.checked
                            onClicked: root.controllerRef.flushCommercialDiagnostics()
                        }
                        Item { Layout.fillWidth: true }
                        ModernButton {
                            visible: Boolean(root.settings.commercial
                                             ? root.settings.commercial.signedIn
                                             : false)
                            text: root.tr("settings.commercial_refresh_account")
                            iconName: "account"
                            onClicked: root.controllerRef.refreshCommercialAccount()
                        }
                        ModernButton {
                            visible: Boolean(root.settings.commercial
                                             ? root.settings.commercial.signedIn
                                             : false)
                            text: root.tr("settings.commercial_sign_out")
                            iconName: "logout"
                            onClicked: root.controllerRef.signOutCommercial()
                        }
                    }
                    ColumnLayout {
                        visible: Boolean(root.settings.commercial
                                         && root.settings.commercial.account
                                         && root.settings.commercial.account.devices)
                        Layout.fillWidth: true
                        spacing: 5
                        Text {
                            text: root.tr("settings.commercial_devices")
                            color: Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                        Repeater {
                            model: {
                                var account = root.settings.commercial
                                              ? root.settings.commercial.account
                                              : ({})
                                var devices = account.devices || []
                                return devices.slice(0, 3)
                            }
                            delegate: Rectangle {
                                required property var modelData
                                Layout.fillWidth: true
                                Layout.preferredHeight: 38
                                radius: Theme.radiusSmall
                                color: Theme.surfaceSoft
                                RowLayout {
                                    anchors.fill: parent
                                    anchors.leftMargin: 10
                                    anchors.rightMargin: 8
                                    Text {
                                        Layout.fillWidth: true
                                        text: String(modelData.os_family || "")
                                              + " · " + String(modelData.app_version || "")
                                              + " · " + String(modelData.device_id || "")
                                        color: Theme.textSecondary
                                        font.family: Theme.monoFontFamily
                                        font.pixelSize: 10
                                        elide: Text.ElideMiddle
                                    }
                                    StatusBadge {
                                        label: String(modelData.revoked_at || "").length > 0
                                               ? root.tr("settings.commercial_device_revoked")
                                               : String(modelData.device_id || "")
                                                 === String(root.settings.commercial.deviceId || "")
                                                 ? root.tr("settings.commercial_device_current")
                                                 : root.tr("settings.commercial_device_active")
                                        tone: String(modelData.revoked_at || "").length > 0
                                              ? "secondary" : "success"
                                    }
                                    ModernButton {
                                        visible: String(modelData.revoked_at || "").length === 0
                                        text: root.tr("settings.commercial_revoke")
                                        compact: true
                                        tone: String(modelData.device_id || "")
                                              === String(root.settings.commercial.deviceId || "")
                                              ? "danger" : "secondary"
                                        onClicked: root.controllerRef.revokeCommercialDevice(
                                            String(modelData.device_id || "")
                                        )
                                    }
                                }
                            }
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.commercialFeedback
                        visible: text.length > 0
                        wrapMode: Text.WordWrap
                        color: Theme.primary
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.tr("settings.commercial_privacy_note")
                        wrapMode: Text.WordWrap
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                    }
                    Item { Layout.fillHeight: true }
                }
            }
        }
    }
}
