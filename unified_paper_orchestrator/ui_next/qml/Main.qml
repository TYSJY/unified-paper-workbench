import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts
import "components" as UI

ApplicationWindow {
    id: window
    width: 1540
    height: 900
    minimumWidth: 1080
    minimumHeight: 680
    visible: true
    color: UI.Theme.canvas
    title: window.tr("app.title")

    property bool compactLayout: width < 1320
    property string writingScheme: controller.writingPromptScheme
    property bool undergraduateWriting: writingScheme.indexOf("undergraduate_") === 0
    property bool verboseLocale: i18n.locale !== "zh_CN"
    // English and other verbose locales need substantially more horizontal
    // room for workspace and action labels. Switch to the compact icon header
    // before those controls begin to clip at common 125% Windows DPI widths.
    property bool narrowHeader: width < (verboseLocale ? 1640 : 1220)
    property var navigation: [
        {"key": "overview", "label": window.tr("nav.overview"), "icon": "home"},
        {"key": "writing", "label": window.tr("nav.writing"), "icon": "document"},
        {"key": "figures", "label": window.tr("nav.figures"), "icon": "figures"},
        {"key": "journal_author", "label": window.tr("nav.journal_author"), "icon": "people"},
        {"key": "submission", "label": window.tr("nav.submission"), "icon": "submission"}
    ]

    function tr(key) { return i18n.text(key, i18n.revision) }
    function tf(key, values) { return i18n.formatText(key, values, i18n.revision) }
    function pipelineStatusLabel(token) {
        var normalized = String(token || "idle")
        var supported = ["idle", "preparing", "running", "needs_attention", "paused", "completed", "failed_start"]
        return window.tr("pipeline.status_" + (supported.indexOf(normalized) >= 0 ? normalized : "unknown"))
    }
    function pipelineSummary(status) {
        var value = status || ({})
        var papers = value.papers || []
        return window.pipelineStatusLabel(value.status)
               + " · "
               + window.tf("pipeline.submitted_count", {
                   "completed": Number(value.completed_count || 0),
                   "requested": Number(value.requested_count || papers.length)
               })
               + " · "
               + window.tf("pipeline.overall_progress", {
                   "progress": Number(value.progress_percent || 0)
               })
    }

    function pageSubtitle(key) {
        return window.tr("subtitle." + key)
    }

    Connections {
        target: controller
        function onToastRequested(message, tone) { toast.show(message, tone) }
        function onErrorRequested(title, message, technical) {
            errorPopup.dialogTitle = title
            errorPopup.dialogMessage = message
            errorPopup.technicalMessage = technical
            errorPopup.showTechnical = false
            errorPopup.open()
        }
        function onFileRequested(title, action, initialFolder) {
            evidenceFileDialog.title = title
            evidenceFileDialog.actionKey = action
            evidenceFileDialog.currentFolder = initialFolder
            evidenceFileDialog.open()
        }
        function onRetargetRequested(paperId) {
            retargetPopup.paperId = paperId
            retargetPopup.modeIndex = 0
            retargetPopup.tierIndex = 0
            retargetJournal.text = ""
            retargetPopup.open()
        }
        function onWorkspaceManagerRequested() {
            workspacePopup.open()
        }
        function onSettingsRequested() {
            settingsCenter.open()
        }
        function onPromptEditorRequested(title, stage, stepId, kind, text) {
            promptEditor.dialogTitle = title
            promptEditor.stageKey = stage
            promptEditor.stepId = stepId
            promptEditor.stepKind = kind
            promptText.text = text
            promptEditor.open()
        }
        function onAuthorTemplateEditorRequested(templateId, name, content, isDefault) {
            authorTemplateEditor.templateId = templateId
            authorTemplateName.text = name
            authorTemplateText.text = content
            authorTemplateDefault.checked = isDefault
            authorTemplateEditor.open()
        }
        function onJournalPreferencesRequested(paperId, data) {
            var quartiles = data.quartiles || ({})
            var indexing = data.indexing || ({})
            journalPreferences.paperId = paperId
            prefQ1.checked = Boolean(quartiles.Q1)
            prefQ2.checked = Boolean(quartiles.Q2)
            prefQ3.checked = Boolean(quartiles.Q3)
            prefQ4.checked = Boolean(quartiles.Q4)
            prefScie.checked = Boolean(indexing.SCIE)
            prefEsci.checked = Boolean(indexing.ESCI)
            prefScopus.checked = Boolean(indexing.Scopus)
            prefStrategy.text = String(data.strategy || "")
            var oaIndex = journalPreferences.oaValues.indexOf(String(data.oa_policy || "subscription_first"))
            prefOa.currentIndex = Math.max(0, oaIndex)
            prefNonOa.checked = Boolean(data.non_oa_required)
            prefMaxApc.text = String(data.max_apc || "")
            prefKeywords.text = String(data.keywords || "")
            prefPreferred.text = String(data.preferred_journals || "")
            prefRejected.text = String(data.rejected_journals || "")
            prefNotes.text = String(data.notes || "")
            journalPreferences.open()
        }
        function onAuthorTemplateApplyRequested(paperId, templates, defaultId) {
            authorTemplateApply.paperId = paperId
            authorTemplateApply.templates = templates
            authorTemplateApply.defaultId = defaultId
            var selectedIndex = 0
            for (var index = 0; index < templates.length; ++index) {
                if (String(templates[index].templateId || "") === defaultId) {
                    selectedIndex = index
                    break
                }
            }
            authorTemplateChoice.currentIndex = selectedIndex
            authorTemplateOverwrite.checked = true
            authorTemplateApply.open()
        }
        function onFullPipelineStatusRequested(status) {
            fullPipelinePopup.status = status
            fullPipelinePopup.open()
        }
        function onFullPipelineTrustRequested(count, passwordConfigured) {
            fullPipelineTrustPopup.paperCount = Number(count || 1)
            fullPipelineTrustPopup.passwordConfigured = Boolean(passwordConfigured)
            pipelineTrustPassword.text = ""
            pipelineTrustConfirmation.text = ""
            fullPipelineTrustPopup.open()
            pipelineTrustPassword.forceActiveFocus()
        }
        function onStateChanged() {
            if (fullPipelinePopup.visible)
                fullPipelinePopup.status = controller.fullPipelineStatus
        }
        function onConfirmRequested(title, message, action, value) {
            confirmPopup.dialogTitle = title
            confirmPopup.dialogMessage = message
            confirmPopup.actionKey = action
            confirmPopup.actionValue = value
            confirmPopup.open()
        }
        function onCountRequested(title, message, action, initialValue, minimum, maximum) {
            countPopup.dialogTitle = title
            countPopup.dialogMessage = message
            countPopup.actionKey = action
            countPopup.minimumValue = minimum
            countPopup.maximumValue = maximum
            countPopup.currentValue = initialValue
            countPopup.open()
        }
        function onUndergraduateBriefRequested(name, version) {
            undergraduateBrief.prepare(name, version)
        }
    }

    UI.UndergraduateBrief { id: undergraduateBrief; objectName: "undergraduateBrief"; controllerRef: controller }

    FileDialog {
        id: evidenceFileDialog
        property string actionKey: ""
        fileMode: FileDialog.OpenFile
        nameFilters: [
            window.tr("dialog.json_files"),
            window.tr("dialog.all_files")
        ]
        onAccepted: controller.executeSelectedPathAction(actionKey, selectedFile)
    }

    FolderDialog {
        id: registerWorkspaceDialog
        title: window.tr("workspace.register_title")
        onAccepted: controller.registerWorkspacePath(selectedFolder)
    }

    FolderDialog {
        id: createWorkspaceParentDialog
        title: window.tr("workspace.parent_title")
        onAccepted: {
            createWorkspacePopup.parentFolder = selectedFolder
            createWorkspacePopup.open()
        }
    }

    Rectangle {
        anchors.fill: parent
        color: UI.Theme.canvas
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Rectangle {
            id: appHeader
            Layout.fillWidth: true
            Layout.preferredHeight: 78
            color: UI.Theme.surface
            border.width: 0

            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 20
                anchors.rightMargin: 20
                spacing: 14

                Image {
                    source: controller.brandLogo
                    sourceSize.width: 38
                    sourceSize.height: 38
                    Layout.preferredWidth: 38
                    Layout.preferredHeight: 38
                    fillMode: Image.PreserveAspectFit
                    smooth: true
                    mipmap: true
                }
                ColumnLayout {
                    Layout.preferredWidth: window.verboseLocale ? 350 : (window.narrowHeader ? 215 : 260)
                    Layout.maximumWidth: window.verboseLocale ? 350 : 260
                    spacing: 1
                    RowLayout {
                        spacing: 8
                        Text {
                            text: window.tr("app.title")
                            color: UI.Theme.text
                            font.family: UI.Theme.fontFamily
                            font.pixelSize: 17
                            font.weight: Font.DemiBold
                        }
                        Rectangle {
                            width: 36
                            height: 22
                            radius: 6
                            color: UI.Theme.primarySoft
                            Text { anchors.centerIn: parent; text: "V2"; color: UI.Theme.primary; font.family: UI.Theme.fontFamily; font.pixelSize: 11; font.weight: Font.DemiBold }
                        }
                    }
                    Text {
                        text: window.tr("app.tagline")
                        color: UI.Theme.textMuted
                        font.family: UI.Theme.fontFamily
                        font.pixelSize: 11
                    }
                }

                Rectangle {
                    visible: !window.narrowHeader
                    Layout.fillWidth: true
                    Layout.maximumWidth: 480
                    Layout.preferredHeight: 44
                    radius: UI.Theme.radiusSmall
                    color: UI.Theme.surfaceSoft
                    border.width: 1
                    border.color: UI.Theme.border
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 13
                        anchors.rightMargin: 12
                        spacing: 9
                        UI.Icon { name: "folder"; iconSize: 17; iconColor: UI.Theme.primary }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 0
                            Text { text: controller.workspaceName; color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12; font.weight: Font.Medium }
                            Text { Layout.fillWidth: true; text: controller.workspacePath; color: UI.Theme.textMuted; font.family: UI.Theme.fontFamily; font.pixelSize: 10; elide: Text.ElideMiddle }
                        }
                        UI.ModernButton { text: window.tr("app.open"); iconName: "external"; compact: true; quiet: true; minimumButtonWidth: 60; onClicked: controller.openWorkspace() }
                        UI.ModernButton { text: window.tr("workspace.switch_short"); iconName: "folder"; compact: true; quiet: true; minimumButtonWidth: 68; onClicked: controller.openWorkspaceManager() }
                    }
                }
                Item { Layout.fillWidth: true }

                UI.ModernButton {
                    visible: window.narrowHeader
                    text: ""
                    toolTipText: window.tr("workspace.switch_short")
                    iconName: "folder"
                    compact: true
                    minimumButtonWidth: 38
                    onClicked: controller.openWorkspaceManager()
                }
                UI.ModernButton { text: window.narrowHeader ? "" : window.tr("action.refresh"); toolTipText: window.tr("action.refresh"); iconName: "refresh"; compact: true; minimumButtonWidth: window.narrowHeader ? 38 : 78; onClicked: controller.prepareAction("refresh") }
                UI.ModernButton {
                    text: controller.fullPipelineLabel
                    toolTipText: !controller.commercialStatus.canStart
                                 ? window.tr("commercial.read_only_tooltip")
                                 : controller.fullPipelineActive
                                   ? window.pipelineSummary(controller.fullPipelineStatus)
                                   : window.tr("action.full_pipeline")
                    iconName: "robot"
                    tone: "primary"
                    compact: true
                    enabled: Boolean(controller.commercialStatus.canStart)
                    onClicked: controller.prepareAction("full_pipeline")
                }
                UI.ModernButton { text: window.narrowHeader ? "" : window.tr("action.settings"); toolTipText: window.tr("action.settings"); iconName: "settings"; compact: true; minimumButtonWidth: window.narrowHeader ? 38 : 78; onClicked: controller.openSettings() }
                UI.ModernButton { text: window.narrowHeader ? "" : window.tr("action.accounts_sites"); toolTipText: window.tr("action.accounts_sites"); iconName: "account"; compact: true; minimumButtonWidth: window.narrowHeader ? 38 : 112; onClicked: controller.openAccountCenter() }
                UI.LanguageSelector { compact: window.narrowHeader }
                UI.ModernButton { text: window.narrowHeader ? "" : window.tr("action.emergency_stop"); toolTipText: window.tr("action.emergency_stop"); iconName: "stop"; tone: "danger"; compact: true; minimumButtonWidth: window.narrowHeader ? 42 : 112; onClicked: controller.prepareAction("pause_all") }
            }

            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: UI.Theme.border
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 54
            color: UI.Theme.surface
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 2
                Repeater {
                    model: window.navigation
                    delegate: UI.NavButton {
                        required property var modelData
                        text: modelData.label
                        iconName: modelData.icon
                        selected: controller.currentPage === modelData.key
                        onClicked: controller.selectPage(modelData.key)
                    }
                }
                Item { Layout.fillWidth: true }
                ColumnLayout {
                    visible: !window.compactLayout
                    spacing: 0
                    Text { text: controller.currentPageTitle; horizontalAlignment: Text.AlignRight; Layout.fillWidth: true; color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12; font.weight: Font.Medium }
                    Text { text: window.pageSubtitle(controller.currentPage); color: UI.Theme.textMuted; font.family: UI.Theme.fontFamily; font.pixelSize: 10 }
                }
            }
            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.bottom: parent.bottom; height: 1; color: UI.Theme.border }
        }

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: controller.loading ? 3 : 0
            clip: true
            Behavior on Layout.preferredHeight { NumberAnimation { duration: 120 } }
            Rectangle { anchors.fill: parent; color: UI.Theme.primarySoft }
            Rectangle {
                id: loadingBar
                visible: controller.loading
                width: parent.width * 0.30
                height: parent.height
                radius: 2
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0; color: "#001677FF" }
                    GradientStop { position: 0.5; color: UI.Theme.primary }
                    GradientStop { position: 1; color: "#001677FF" }
                }
                SequentialAnimation on x {
                    running: controller.loading
                    loops: Animation.Infinite
                    NumberAnimation { from: -loadingBar.width; to: window.width; duration: 1100; easing.type: Easing.InOutQuad }
                }
            }
        }

        Rectangle {
            visible: Boolean(controller.commercialStatus.readOnly)
            Layout.fillWidth: true
            Layout.preferredHeight: visible ? 46 : 0
            color: UI.Theme.attentionSoft
            border.width: 0
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 18
                anchors.rightMargin: 18
                spacing: 10
                UI.Icon {
                    name: "warning"
                    iconSize: 18
                    iconColor: UI.Theme.attention
                }
                Text {
                    Layout.fillWidth: true
                    text: window.tr("commercial.read_only_banner")
                    color: UI.Theme.textSecondary
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 12
                    wrapMode: Text.WordWrap
                }
                UI.ModernButton {
                    text: window.tr("commercial.choose_plan")
                    iconName: "external"
                    compact: true
                    tone: "primary"
                    onClicked: controller.openCommercialUrl("pricing")
                }
                UI.ModernButton {
                    text: window.tr("commercial.refresh_license")
                    iconName: "refresh"
                    compact: true
                    onClicked: controller.refreshCommercialLicense()
                }
            }
            Rectangle {
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                height: 1
                color: "#F1D2A8"
            }
        }

        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true

            RowLayout {
                anchors.fill: parent
                anchors.margins: 14
                spacing: 12

                ColumnLayout {
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    spacing: 12

                    Rectangle {
                        visible: controller.currentPage === "overview" || controller.currentPage === "writing"
                        Layout.fillWidth: true; Layout.preferredHeight: visible ? 78 : 0
                        color: UI.Theme.primarySoft; radius: UI.Theme.radius
                        RowLayout {
                            anchors.fill: parent; anchors.margins: 16; spacing: 14
                            UI.Icon { name: "document"; iconSize: 26; iconColor: UI.Theme.primary }
                            ColumnLayout {
                                Layout.fillWidth: true; spacing: 4
                                Text { objectName: "writingSchemeTitle"; text: window.undergraduateWriting ? "本科毕业论文" : "SCI / 期刊论文"; color: UI.Theme.text; font.family: UI.Theme.fontFamily; font.pixelSize: 17; font.weight: Font.DemiBold }
                                Text { text: window.undergraduateWriting
                                             ? "当前新建方案：" + (window.writingScheme === "undergraduate_software" ? "本科计算机软件版" : "本科人文社科版") + " · 从题目和学校资料开始"
                                             : "当前新建方案：原期刊版 · 研究选题、论文写作、配图与投稿";
                                       color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12 }
                            }
                            UI.ModernButton {
                                objectName: "newWritingProject"; text: window.undergraduateWriting ? "新建毕业论文" : "新建期刊论文"; iconName: "plus"; tone: "primary"
                                enabled: !controller.loading
                                onClicked: controller.prepareAction("new_writing")
                            }
                        }
                    }

                    UI.StageActionBar {
                        visible: controller.currentPage !== "overview"
                        Layout.fillWidth: true
                        pageKey: controller.currentPage
                        pageTitle: controller.currentPageTitle
                        controllerRef: controller
                        runAllowed: Boolean(controller.commercialStatus.canStart)
                    }

                    Rectangle {
                        visible: controller.currentPage === "submission"
                                 && controller.submissionMonitor.hasRun
                        Layout.fillWidth: true
                        Layout.preferredHeight: visible ? 42 : 0
                        radius: UI.Theme.radiusSmall
                        color: UI.Theme.surface
                        border.width: 1
                        border.color: UI.Theme.border

                        RowLayout {
                            anchors.fill: parent
                            anchors.leftMargin: 14
                            anchors.rightMargin: 14
                            spacing: 10
                            UI.Icon {
                                name: controller.submissionMonitor.status === "running"
                                      ? "refresh" : "check"
                                iconSize: 17
                                iconColor: controller.submissionMonitor.tone === "danger"
                                           ? UI.Theme.danger
                                           : controller.submissionMonitor.tone === "attention"
                                             ? UI.Theme.attention
                                             : UI.Theme.success
                            }
                            Text {
                                text: window.tr("submission_monitor.last_check")
                                color: UI.Theme.textSecondary
                                font.family: UI.Theme.fontFamily
                                font.pixelSize: 11
                                font.weight: Font.DemiBold
                            }
                            UI.StatusBadge {
                                label: window.tr(
                                    "submission_monitor."
                                    + String(controller.submissionMonitor.status || "never_run")
                                )
                                tone: String(controller.submissionMonitor.tone || "secondary")
                            }
                            Text {
                                Layout.fillWidth: true
                                text: window.tf("submission_monitor.summary", {
                                    "checked": Number(controller.submissionMonitor.checked || 0),
                                    "requested": Number(controller.submissionMonitor.requested || 0),
                                    "failed": Number(controller.submissionMonitor.failed || 0),
                                    "actions": Number(controller.submissionMonitor.actionRequired || 0)
                                })
                                color: UI.Theme.textMuted
                                font.family: UI.Theme.fontFamily
                                font.pixelSize: 11
                                elide: Text.ElideRight
                            }
                            Text {
                                text: String(controller.submissionMonitor.finishedAt || "")
                                color: UI.Theme.textMuted
                                font.family: UI.Theme.monoFontFamily
                                font.pixelSize: 10
                            }
                        }
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 104
                        spacing: 10
                        Repeater {
                            model: controller.metrics
                            delegate: UI.MetricCard {
                                required property var modelData
                                Layout.fillWidth: true
                                label: modelData.label
                                value: String(modelData.value)
                                meta: modelData.meta
                                iconName: modelData.icon
                                tone: modelData.tone
                            }
                        }
                    }

                    UI.PaperList {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        controllerRef: controller
                        selectedId: controller.selectedPaperId
                        compact: window.compactLayout
                        onPaperActivated: function(paperId) {
                            controller.selectPaper(paperId)
                            if (window.compactLayout) detailDrawer.open()
                        }
                    }
                }

                UI.DetailPanel {
                    visible: !window.compactLayout
                    Layout.preferredWidth: window.width >= 1540 ? 400 : 360
                    Layout.fillHeight: true
                    paper: controller.selectedPaper
                }
            }
        }

        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 30
            color: UI.Theme.surface
            border.width: 0
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 8
                Rectangle { width: 7; height: 7; radius: 4; color: controller.loading ? UI.Theme.primary : UI.Theme.success }
                Text { text: controller.statusText; color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 10 }
                Item { Layout.fillWidth: true }
                Text { text: window.tr("action.refresh") + ": " + controller.lastRefresh + "  ·  v" + controller.version; color: UI.Theme.textMuted; font.family: UI.Theme.fontFamily; font.pixelSize: 10 }
            }
            Rectangle { anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; height: 1; color: UI.Theme.border }
        }
    }

    Drawer {
        id: detailDrawer
        parent: Overlay.overlay
        edge: Qt.RightEdge
        width: Math.min(430, window.width * 0.90)
        height: window.height
        modal: true
        interactive: true
        background: Rectangle { color: UI.Theme.surface }
        contentItem: UI.DetailPanel {
            paper: controller.selectedPaper
            closeVisible: true
            onCloseRequested: detailDrawer.close()
        }
    }

    Popup {
        id: confirmPopup
        property string dialogTitle: window.tr("dialog.confirm_title")
        property string dialogMessage: ""
        property string actionKey: ""
        property int actionValue: 0
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(460, window.width - 48)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 130 }
            NumberAnimation {
                property: "scale"
                from: 0.96
                to: 1
                duration: 150
                easing.type: Easing.OutCubic
            }
        }
        exit: Transition { NumberAnimation { property: "opacity"; to: 0; duration: 100 } }
        background: Rectangle { radius: UI.Theme.radiusLarge; color: UI.Theme.surface; border.width: 1; border.color: UI.Theme.border }
        contentItem: ColumnLayout {
            spacing: 0
            Item {
                Layout.fillWidth: true
                Layout.preferredHeight: 74
                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 20
                    anchors.rightMargin: 20
                    spacing: 12
                    Rectangle {
                        width: 38
                        height: 38
                        radius: 19
                        color: UI.Theme.attentionSoft
                        UI.Icon {
                            anchors.centerIn: parent
                            name: "warning"
                            iconColor: UI.Theme.attention
                            iconSize: 19
                        }
                    }
                    Text { Layout.fillWidth: true; text: confirmPopup.dialogTitle; color: UI.Theme.text; font.family: UI.Theme.fontFamily; font.pixelSize: 17; font.weight: Font.DemiBold }
                }
            }
            Rectangle { Layout.fillWidth: true; height: 1; color: UI.Theme.border }
            Text {
                Layout.fillWidth: true
                Layout.margins: 20
                text: confirmPopup.dialogMessage
                wrapMode: Text.WordWrap
                color: UI.Theme.textSecondary
                font.family: UI.Theme.fontFamily
                font.pixelSize: 13
                lineHeight: 1.35
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 18
                Item { Layout.fillWidth: true }
                UI.ModernButton { text: window.tr("action.cancel"); compact: true; onClicked: confirmPopup.close() }
                UI.ModernButton {
                    text: window.tr("action.confirm")
                    tone: confirmPopup.actionKey === "pause_all" ? "danger" : "primary"
                    compact: true
                    onClicked: {
                        confirmPopup.close()
                        controller.executeAction(confirmPopup.actionKey, confirmPopup.actionValue)
                    }
                }
            }
        }
    }

    Popup {
        id: countPopup
        objectName: "countPopup"
        property string dialogTitle: window.tr("dialog.count_title")
        property string dialogMessage: ""
        property string actionKey: ""
        property int minimumValue: 1
        property int maximumValue: 8
        property int currentValue: 1
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(480, window.width - 48)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle { radius: UI.Theme.radiusLarge; color: UI.Theme.surface; border.width: 1; border.color: UI.Theme.border }
        contentItem: ColumnLayout {
            spacing: 14
            Text { Layout.fillWidth: true; Layout.leftMargin: 22; Layout.rightMargin: 22; Layout.topMargin: 20; text: countPopup.dialogTitle; color: UI.Theme.text; font.family: UI.Theme.fontFamily; font.pixelSize: 18; font.weight: Font.DemiBold }
            Text { Layout.fillWidth: true; Layout.leftMargin: 22; Layout.rightMargin: 22; text: countPopup.dialogMessage; wrapMode: Text.WordWrap; color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 13; lineHeight: 1.35 }
            Rectangle {
                Layout.alignment: Qt.AlignHCenter
                Layout.preferredWidth: 210
                Layout.preferredHeight: 54
                radius: UI.Theme.radius
                color: UI.Theme.surfaceSoft
                border.width: 1
                border.color: UI.Theme.border
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 8
                    UI.ModernButton { text: "−"; compact: true; minimumButtonWidth: 42; enabled: countPopup.currentValue > countPopup.minimumValue; onClicked: countPopup.currentValue-- }
                    Text { Layout.fillWidth: true; text: countPopup.currentValue; horizontalAlignment: Text.AlignHCenter; color: UI.Theme.text; font.family: UI.Theme.fontFamily; font.pixelSize: 23; font.weight: Font.DemiBold }
                    UI.ModernButton { text: "+"; compact: true; minimumButtonWidth: 42; enabled: countPopup.currentValue < countPopup.maximumValue; onClicked: countPopup.currentValue++ }
                }
            }
            Text { Layout.alignment: Qt.AlignHCenter; text: window.tr("dialog.range_label") + " " + countPopup.minimumValue + "–" + countPopup.maximumValue; color: UI.Theme.textMuted; font.family: UI.Theme.fontFamily; font.pixelSize: 11 }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 20
                Layout.rightMargin: 20
                Layout.bottomMargin: 20
                Item { Layout.fillWidth: true }
                UI.ModernButton { text: window.tr("action.cancel"); compact: true; onClicked: countPopup.close() }
                UI.ModernButton { text: window.tr("action.start"); iconName: "play"; tone: "primary"; compact: true; onClicked: { countPopup.close(); controller.executeAction(countPopup.actionKey, countPopup.currentValue) } }
            }
        }
    }

    Popup {
        id: errorPopup
        property string dialogTitle: window.tr("dialog.error_title")
        property string dialogMessage: ""
        property string technicalMessage: ""
        property bool showTechnical: false
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(640, window.width - 48)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            radius: UI.Theme.radiusLarge
            color: UI.Theme.surface
            border.width: 1
            border.color: UI.Theme.border
        }
        contentItem: ColumnLayout {
            spacing: 14
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.topMargin: 20
                spacing: 12
                Rectangle {
                    width: 38
                    height: 38
                    radius: 19
                    color: UI.Theme.dangerSoft
                    UI.Icon {
                        anchors.centerIn: parent
                        name: "warning"
                        iconColor: UI.Theme.danger
                        iconSize: 19
                    }
                }
                Text {
                    Layout.fillWidth: true
                    text: errorPopup.dialogTitle
                    color: UI.Theme.text
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                    wrapMode: Text.WordWrap
                }
            }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                text: errorPopup.dialogMessage
                wrapMode: Text.WrapAnywhere
                color: UI.Theme.textSecondary
                font.family: UI.Theme.fontFamily
                font.pixelSize: 13
                lineHeight: 1.35
            }
            UI.ModernButton {
                visible: errorPopup.technicalMessage.length > 0
                Layout.leftMargin: 22
                text: errorPopup.showTechnical
                    ? window.tr("detail.hide_technical")
                    : window.tr("detail.show_technical")
                compact: true
                onClicked: errorPopup.showTechnical = !errorPopup.showTechnical
            }
            ScrollView {
                visible: errorPopup.showTechnical
                Layout.fillWidth: true
                Layout.preferredHeight: 190
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                clip: true
                TextArea {
                    text: errorPopup.technicalMessage
                    readOnly: true
                    wrapMode: TextEdit.WrapAnywhere
                    selectByMouse: true
                    color: UI.Theme.textSecondary
                    font.family: UI.Theme.monoFontFamily
                    font.pixelSize: 11
                    background: Rectangle {
                        color: UI.Theme.surfaceSoft
                        radius: UI.Theme.radiusSmall
                        border.width: 1
                        border.color: UI.Theme.border
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.bottomMargin: 20
                Item { Layout.fillWidth: true }
                UI.ModernButton {
                    text: window.tr("action.close")
                    tone: "primary"
                    compact: true
                    onClicked: errorPopup.close()
                }
            }
        }
    }

    Popup {
        id: retargetPopup
        property string paperId: ""
        property int modeIndex: 0
        property int tierIndex: 0
        readonly property var modeValues: ["ai", "specific"]
        readonly property var tierValues: ["sci", "chinese_core", "general"]
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(540, window.width - 48)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            radius: UI.Theme.radiusLarge
            color: UI.Theme.surface
            border.width: 1
            border.color: UI.Theme.border
        }
        contentItem: ColumnLayout {
            spacing: 13
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.topMargin: 20
                text: window.tr("retarget.title")
                color: UI.Theme.text
                font.family: UI.Theme.fontFamily
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                text: window.tr("retarget.paper") + ": " + retargetPopup.paperId
                color: UI.Theme.textMuted
                font.family: UI.Theme.fontFamily
                font.pixelSize: 12
                elide: Text.ElideMiddle
            }
            Text { Layout.leftMargin: 22; text: window.tr("retarget.mode"); color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12 }
            UI.ModernComboBox {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                model: [window.tr("retarget.ai"), window.tr("retarget.specific")]
                currentIndex: retargetPopup.modeIndex
                onActivated: retargetPopup.modeIndex = currentIndex
            }
            UI.ModernTextField {
                id: retargetJournal
                visible: retargetPopup.modeIndex === 1
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                placeholderText: window.tr("retarget.journal_placeholder")
                font.family: UI.Theme.fontFamily
                selectByMouse: true
            }
            Text { Layout.leftMargin: 22; text: window.tr("retarget.tier"); color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12 }
            UI.ModernComboBox {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                model: [window.tr("retarget.sci"), window.tr("retarget.chinese_core"), window.tr("retarget.general")]
                currentIndex: retargetPopup.tierIndex
                onActivated: retargetPopup.tierIndex = currentIndex
            }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                text: window.tr("retarget.safety_note")
                wrapMode: Text.WordWrap
                color: UI.Theme.textMuted
                font.family: UI.Theme.fontFamily
                font.pixelSize: 11
                lineHeight: 1.3
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.bottomMargin: 20
                Item { Layout.fillWidth: true }
                UI.ModernButton { text: window.tr("action.cancel"); compact: true; onClicked: retargetPopup.close() }
                UI.ModernButton {
                    text: window.tr("retarget.submit")
                    iconName: "forward"
                    tone: "primary"
                    compact: true
                    enabled: retargetPopup.modeIndex === 0 || retargetJournal.text.trim().length > 0
                    onClicked: {
                        var mode = retargetPopup.modeValues[retargetPopup.modeIndex]
                        var tier = retargetPopup.tierValues[retargetPopup.tierIndex]
                        retargetPopup.close()
                        controller.requestRetarget(mode, retargetJournal.text, tier)
                    }
                }
            }
        }
    }

    Popup {
        id: workspacePopup
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(720, window.width - 48)
        height: Math.min(610, window.height - 60)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            radius: UI.Theme.radiusLarge
            color: UI.Theme.surface
            border.width: 1
            border.color: UI.Theme.border
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
                    text: window.tr("workspace.title")
                    color: UI.Theme.text
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                }
                UI.ModernButton { text: window.tr("action.close"); compact: true; quiet: true; onClicked: workspacePopup.close() }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: UI.Theme.border }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.topMargin: 15
                Layout.bottomMargin: 10
                text: window.tr("workspace.description")
                wrapMode: Text.WordWrap
                color: UI.Theme.textMuted
                font.family: UI.Theme.fontFamily
                font.pixelSize: 12
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 18
                Layout.rightMargin: 18
                clip: true
                Column {
                    width: parent.width
                    spacing: 8
                    Repeater {
                        model: controller.registeredWorkspaces
                        delegate: Rectangle {
                            required property var modelData
                            width: parent.width
                            height: 74
                            radius: UI.Theme.radius
                            color: modelData.active ? UI.Theme.primarySoft : UI.Theme.surfaceSoft
                            border.width: 1
                            border.color: modelData.active ? "#AFCFFA" : UI.Theme.border
                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 12
                                spacing: 12
                                UI.Icon { name: "folder"; iconSize: 19; iconColor: modelData.active ? UI.Theme.primary : UI.Theme.textMuted }
                                ColumnLayout {
                                    Layout.fillWidth: true
                                    spacing: 2
                                    RowLayout {
                                        Text { text: String(modelData.name || ""); color: UI.Theme.text; font.family: UI.Theme.fontFamily; font.pixelSize: 13; font.weight: Font.DemiBold }
                                        UI.StatusBadge { visible: modelData.active; label: window.tr("workspace.current"); tone: "info" }
                                    }
                                    Text { Layout.fillWidth: true; text: String(modelData.path || ""); color: UI.Theme.textMuted; font.family: UI.Theme.fontFamily; font.pixelSize: 10; elide: Text.ElideMiddle }
                                }
                                UI.ModernButton {
                                    text: modelData.active ? window.tr("workspace.in_use") : window.tr("workspace.use")
                                    iconName: modelData.active ? "check" : "forward"
                                    tone: modelData.active ? "secondary" : "primary"
                                    compact: true
                                    enabled: !modelData.active && modelData.exists && modelData.valid
                                    onClicked: controller.requestWorkspaceSwitch(String(modelData.path || ""))
                                }
                            }
                        }
                    }
                }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: UI.Theme.border }
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 18
                spacing: 10
                UI.ModernButton { text: window.tr("workspace.open_current"); iconName: "external"; compact: true; onClicked: controller.openWorkspace() }
                Item { Layout.fillWidth: true }
                UI.ModernButton { text: window.tr("workspace.register_existing"); iconName: "plus"; compact: true; onClicked: registerWorkspaceDialog.open() }
                UI.ModernButton { text: window.tr("workspace.create_new"); iconName: "plus"; tone: "primary"; compact: true; onClicked: createWorkspaceParentDialog.open() }
            }
        }
    }

    Popup {
        id: createWorkspacePopup
        property url parentFolder: ""
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(500, window.width - 48)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle { radius: UI.Theme.radiusLarge; color: UI.Theme.surface; border.width: 1; border.color: UI.Theme.border }
        contentItem: ColumnLayout {
            spacing: 13
            Text { Layout.fillWidth: true; Layout.leftMargin: 22; Layout.rightMargin: 22; Layout.topMargin: 20; text: window.tr("workspace.create_title"); color: UI.Theme.text; font.family: UI.Theme.fontFamily; font.pixelSize: 18; font.weight: Font.DemiBold }
            Text { Layout.fillWidth: true; Layout.leftMargin: 22; Layout.rightMargin: 22; text: window.tr("workspace.parent") + ": " + createWorkspacePopup.parentFolder; color: UI.Theme.textMuted; font.family: UI.Theme.fontFamily; font.pixelSize: 11; elide: Text.ElideMiddle }
            UI.ModernTextField {
                id: workspaceNameInput
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                placeholderText: window.tr("workspace.name_placeholder")
                font.family: UI.Theme.fontFamily
                selectByMouse: true
            }
            CheckBox {
                id: importWorkspaceCheck
                Layout.leftMargin: 22
                text: window.tr("workspace.import_current")
                font.family: UI.Theme.fontFamily
            }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                text: window.tr("workspace.create_note")
                wrapMode: Text.WordWrap
                color: UI.Theme.textMuted
                font.family: UI.Theme.fontFamily
                font.pixelSize: 11
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.bottomMargin: 20
                Item { Layout.fillWidth: true }
                UI.ModernButton { text: window.tr("action.cancel"); compact: true; onClicked: createWorkspacePopup.close() }
                UI.ModernButton {
                    text: window.tr("workspace.create")
                    iconName: "plus"
                    tone: "primary"
                    compact: true
                    enabled: workspaceNameInput.text.trim().length > 0
                    onClicked: {
                        createWorkspacePopup.close()
                        controller.createWorkspaceAt(
                            createWorkspacePopup.parentFolder,
                            workspaceNameInput.text,
                            importWorkspaceCheck.checked
                        )
                        workspaceNameInput.text = ""
                        importWorkspaceCheck.checked = false
                    }
                }
            }
        }
    }

    UI.SettingsCenter {
        id: settingsCenter
        objectName: "settingsCenter"
        controllerRef: controller
    }

    UI.CommercialOnboarding {
        anchors.fill: parent
        z: 500
        visible: Boolean(controller.commercialStatus.onboardingRequired)
        controllerRef: controller
    }

    Popup {
        id: promptEditor
        property string dialogTitle: ""
        property string stageKey: ""
        property string stepId: ""
        property string stepKind: ""
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(980, window.width - 56)
        height: Math.min(740, window.height - 56)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            radius: UI.Theme.radiusLarge
            color: UI.Theme.surface
            border.width: 1
            border.color: UI.Theme.border
        }
        contentItem: ColumnLayout {
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 68
                Layout.leftMargin: 22
                Layout.rightMargin: 18
                UI.Icon { name: "edit"; iconSize: 21; iconColor: UI.Theme.primary }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Text {
                        Layout.fillWidth: true
                        text: promptEditor.dialogTitle
                        color: UI.Theme.text
                        font.family: UI.Theme.fontFamily
                        font.pixelSize: 17
                        font.weight: Font.DemiBold
                        elide: Text.ElideRight
                    }
                    Text {
                        text: promptEditor.stageKey + " · " + promptEditor.stepId
                        color: UI.Theme.textMuted
                        font.family: UI.Theme.monoFontFamily
                        font.pixelSize: 10
                    }
                }
                UI.ModernButton {
                    text: window.tr("action.close")
                    compact: true
                    quiet: true
                    onClicked: promptEditor.close()
                }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: UI.Theme.border }
            Rectangle {
                Layout.fillWidth: true
                Layout.leftMargin: 18
                Layout.rightMargin: 18
                Layout.topMargin: 14
                Layout.bottomMargin: 10
                Layout.preferredHeight: 48
                radius: UI.Theme.radiusSmall
                color: UI.Theme.attentionSoft
                RowLayout {
                    anchors.fill: parent
                    anchors.margins: 10
                    UI.Icon { name: "warning"; iconSize: 17; iconColor: UI.Theme.attention }
                    Text {
                        Layout.fillWidth: true
                        text: window.tr("settings.prompt_warning")
                        wrapMode: Text.WordWrap
                        color: UI.Theme.textSecondary
                        font.family: UI.Theme.fontFamily
                        font.pixelSize: 11
                    }
                }
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 18
                Layout.rightMargin: 18
                clip: true
                TextArea {
                    id: promptText
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    color: UI.Theme.text
                    font.family: UI.Theme.monoFontFamily
                    font.pixelSize: 12
                    leftPadding: 14
                    rightPadding: 14
                    topPadding: 12
                    bottomPadding: 12
                    background: Rectangle {
                        color: UI.Theme.surfaceSoft
                        radius: UI.Theme.radiusSmall
                        border.width: 1
                        border.color: promptText.activeFocus ? UI.Theme.primary : UI.Theme.border
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 18
                Text {
                    Layout.fillWidth: true
                    text: window.tr("settings.prompt_backup_note")
                    color: UI.Theme.textMuted
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 10
                    wrapMode: Text.WordWrap
                }
                UI.ModernButton {
                    text: window.tr("action.cancel")
                    compact: true
                    onClicked: promptEditor.close()
                }
                UI.ModernButton {
                    text: window.tr("action.save")
                    iconName: "check"
                    tone: "primary"
                    compact: true
                    onClicked: {
                        controller.savePromptText(
                            promptEditor.stageKey,
                            promptEditor.stepId,
                            promptEditor.stepKind,
                            promptText.text
                        )
                        promptEditor.close()
                    }
                }
            }
        }
    }

    Popup {
        id: authorTemplateEditor
        property string templateId: ""
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(900, window.width - 56)
        height: Math.min(720, window.height - 56)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            radius: UI.Theme.radiusLarge
            color: UI.Theme.surface
            border.width: 1
            border.color: UI.Theme.border
        }
        contentItem: ColumnLayout {
            spacing: 12
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 18
                Layout.topMargin: 18
                UI.Icon { name: "people"; iconSize: 21; iconColor: UI.Theme.primary }
                Text {
                    Layout.fillWidth: true
                    text: window.tr("settings.author_editor_title")
                    color: UI.Theme.text
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                }
                UI.ModernButton { text: window.tr("action.close"); compact: true; quiet: true; onClicked: authorTemplateEditor.close() }
            }
            UI.ModernTextField {
                id: authorTemplateName
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                placeholderText: window.tr("settings.template_name")
                font.family: UI.Theme.fontFamily
                selectByMouse: true
            }
            CheckBox {
                id: authorTemplateDefault
                Layout.leftMargin: 22
                text: window.tr("settings.make_default")
                font.family: UI.Theme.fontFamily
            }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                text: window.tr("settings.author_markdown_note")
                wrapMode: Text.WordWrap
                color: UI.Theme.textMuted
                font.family: UI.Theme.fontFamily
                font.pixelSize: 11
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                clip: true
                TextArea {
                    id: authorTemplateText
                    selectByMouse: true
                    wrapMode: TextEdit.Wrap
                    color: UI.Theme.text
                    font.family: UI.Theme.monoFontFamily
                    font.pixelSize: 12
                    leftPadding: 14
                    rightPadding: 14
                    topPadding: 12
                    bottomPadding: 12
                    background: Rectangle {
                        color: UI.Theme.surfaceSoft
                        radius: UI.Theme.radiusSmall
                        border.width: 1
                        border.color: authorTemplateText.activeFocus ? UI.Theme.primary : UI.Theme.border
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.bottomMargin: 18
                Item { Layout.fillWidth: true }
                UI.ModernButton { text: window.tr("action.cancel"); compact: true; onClicked: authorTemplateEditor.close() }
                UI.ModernButton {
                    text: window.tr("action.save")
                    iconName: "check"
                    tone: "primary"
                    compact: true
                    enabled: authorTemplateName.text.trim().length > 0
                    onClicked: {
                        controller.saveAuthorTemplate(
                            authorTemplateEditor.templateId,
                            authorTemplateName.text,
                            authorTemplateText.text,
                            authorTemplateDefault.checked
                        )
                        authorTemplateEditor.close()
                    }
                }
            }
        }
    }

    Popup {
        id: journalPreferences
        property string paperId: ""
        readonly property var oaValues: ["subscription_first", "either", "oa_allowed", "oa_required"]
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(760, window.width - 56)
        height: Math.min(720, window.height - 56)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            radius: UI.Theme.radiusLarge
            color: UI.Theme.surface
            border.width: 1
            border.color: UI.Theme.border
        }
        contentItem: ColumnLayout {
            spacing: 12
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 18
                Layout.topMargin: 18
                UI.Icon { name: "submission"; iconSize: 21; iconColor: UI.Theme.primary }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Text { text: window.tr("journal_preferences.title"); color: UI.Theme.text; font.family: UI.Theme.fontFamily; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Text { text: journalPreferences.paperId; color: UI.Theme.textMuted; font.family: UI.Theme.monoFontFamily; font.pixelSize: 10 }
                }
                UI.ModernButton { text: window.tr("action.close"); compact: true; quiet: true; onClicked: journalPreferences.close() }
            }
            ScrollView {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                clip: true
                ColumnLayout {
                    width: parent.width
                    spacing: 12
                    Text { text: window.tr("journal_preferences.quartiles"); color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                    RowLayout {
                        CheckBox { id: prefQ1; text: "Q1"; font.family: UI.Theme.fontFamily }
                        CheckBox { id: prefQ2; text: "Q2"; font.family: UI.Theme.fontFamily }
                        CheckBox { id: prefQ3; text: "Q3"; font.family: UI.Theme.fontFamily }
                        CheckBox { id: prefQ4; text: "Q4"; font.family: UI.Theme.fontFamily }
                        Item { Layout.fillWidth: true }
                    }
                    Text { text: window.tr("journal_preferences.indexing"); color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                    RowLayout {
                        CheckBox { id: prefScie; text: "SCIE"; font.family: UI.Theme.fontFamily }
                        CheckBox { id: prefEsci; text: "ESCI"; font.family: UI.Theme.fontFamily }
                        CheckBox { id: prefScopus; text: "Scopus"; font.family: UI.Theme.fontFamily }
                        Item { Layout.fillWidth: true }
                    }
                    UI.ModernTextField { id: prefStrategy; Layout.fillWidth: true; placeholderText: window.tr("journal_preferences.strategy") }
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: window.tr("journal_preferences.oa_policy"); color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12 }
                        UI.ModernComboBox {
                            id: prefOa
                            Layout.preferredWidth: 220
                            model: [
                                window.tr("journal_preferences.subscription_first"),
                                window.tr("journal_preferences.either"),
                                window.tr("journal_preferences.oa_allowed"),
                                window.tr("journal_preferences.oa_required")
                            ]
                        }
                        CheckBox { id: prefNonOa; Layout.fillWidth: true; text: window.tr("journal_preferences.non_oa_required"); font.family: UI.Theme.fontFamily }
                    }
                    UI.ModernTextField { id: prefMaxApc; Layout.fillWidth: true; placeholderText: window.tr("journal_preferences.max_apc") }
                    UI.ModernTextField { id: prefKeywords; Layout.fillWidth: true; placeholderText: window.tr("journal_preferences.keywords") }
                    UI.ModernTextField { id: prefPreferred; Layout.fillWidth: true; placeholderText: window.tr("journal_preferences.preferred") }
                    UI.ModernTextField { id: prefRejected; Layout.fillWidth: true; placeholderText: window.tr("journal_preferences.rejected") }
                    Text {
                        text: window.tr("journal_preferences.notes")
                        color: UI.Theme.textSecondary
                        font.family: UI.Theme.fontFamily
                        font.pixelSize: 12
                    }
                    TextArea {
                        id: prefNotes
                        Layout.fillWidth: true
                        Layout.preferredHeight: 120
                        wrapMode: TextEdit.Wrap
                        selectByMouse: true
                        font.family: UI.Theme.fontFamily
                        color: UI.Theme.text
                        background: Rectangle {
                            color: UI.Theme.surfaceSoft
                            radius: UI.Theme.radiusSmall
                            border.width: 1
                            border.color: prefNotes.activeFocus ? UI.Theme.primary : UI.Theme.border
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.bottomMargin: 18
                Item { Layout.fillWidth: true }
                UI.ModernButton { text: window.tr("action.cancel"); compact: true; onClicked: journalPreferences.close() }
                UI.ModernButton {
                    text: window.tr("action.save")
                    iconName: "check"
                    tone: "primary"
                    compact: true
                    onClicked: {
                        controller.saveJournalPreferences(
                            journalPreferences.paperId,
                            prefQ1.checked,
                            prefQ2.checked,
                            prefQ3.checked,
                            prefQ4.checked,
                            prefScie.checked,
                            prefEsci.checked,
                            prefScopus.checked,
                            prefStrategy.text,
                            journalPreferences.oaValues[prefOa.currentIndex],
                            prefNonOa.checked,
                            prefMaxApc.text,
                            prefKeywords.text,
                            prefPreferred.text,
                            prefRejected.text,
                            prefNotes.text
                        )
                        journalPreferences.close()
                    }
                }
            }
        }
    }

    Popup {
        id: authorTemplateApply
        property string paperId: ""
        property string defaultId: ""
        property var templates: []
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(560, window.width - 48)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle { radius: UI.Theme.radiusLarge; color: UI.Theme.surface; border.width: 1; border.color: UI.Theme.border }
        contentItem: ColumnLayout {
            spacing: 13
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.topMargin: 20
                text: window.tr("author_apply.title")
                color: UI.Theme.text
                font.family: UI.Theme.fontFamily
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                text: window.tr("author_apply.paper") + ": " + authorTemplateApply.paperId
                color: UI.Theme.textMuted
                font.family: UI.Theme.monoFontFamily
                font.pixelSize: 10
                elide: Text.ElideMiddle
            }
            UI.ModernComboBox {
                id: authorTemplateChoice
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                model: authorTemplateApply.templates.map(function(item) {
                    var suffix = item.isDefault ? " · " + window.tr("settings.default_template") : ""
                    return String(item.name || item.templateId || "") + " [" + String(item.status || "") + "]" + suffix
                })
            }
            CheckBox {
                id: authorTemplateOverwrite
                Layout.leftMargin: 22
                text: window.tr("author_apply.overwrite")
                font.family: UI.Theme.fontFamily
            }
            Text {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                text: window.tr("author_apply.note")
                wrapMode: Text.WordWrap
                color: UI.Theme.textMuted
                font.family: UI.Theme.fontFamily
                font.pixelSize: 11
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.leftMargin: 22
                Layout.rightMargin: 22
                Layout.bottomMargin: 20
                Item { Layout.fillWidth: true }
                UI.ModernButton { text: window.tr("action.cancel"); compact: true; onClicked: authorTemplateApply.close() }
                UI.ModernButton {
                    text: window.tr("author_apply.apply")
                    iconName: "check"
                    tone: "primary"
                    compact: true
                    enabled: authorTemplateApply.templates.length > 0
                    onClicked: {
                        var selected = authorTemplateApply.templates[authorTemplateChoice.currentIndex]
                        if (selected) {
                            controller.applyAuthorTemplate(
                                authorTemplateApply.paperId,
                                String(selected.templateId || ""),
                                authorTemplateOverwrite.checked
                            )
                        }
                        authorTemplateApply.close()
                    }
                }
            }
        }
    }

    Popup {
        id: fullPipelineTrustPopup
        property int paperCount: 1
        property bool passwordConfigured: false
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(580, window.width - 56)
        height: Math.min(450, window.height - 56)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            radius: UI.Theme.radiusLarge
            color: UI.Theme.surface
            border.width: 1
            border.color: UI.Theme.border
        }
        contentItem: ColumnLayout {
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 70
                Layout.leftMargin: 22
                Layout.rightMargin: 18
                Rectangle {
                    Layout.preferredWidth: 38
                    Layout.preferredHeight: 38
                    radius: 19
                    color: UI.Theme.attentionSoft
                    UI.Icon { anchors.centerIn: parent; name: "shield"; iconSize: 20; iconColor: UI.Theme.attention }
                }
                Text {
                    Layout.fillWidth: true
                    text: window.tr("pipeline.trust_title")
                    color: UI.Theme.text
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                }
                UI.ModernButton { text: window.tr("action.close"); compact: true; quiet: true; onClicked: fullPipelineTrustPopup.close() }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: UI.Theme.border }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 22
                spacing: 13
                Text {
                    Layout.fillWidth: true
                    text: window.tf("pipeline.trust_description", {"count": fullPipelineTrustPopup.paperCount})
                    wrapMode: Text.WordWrap
                    color: UI.Theme.textSecondary
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 13
                    lineHeight: 1.35
                }
                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: 48
                    radius: UI.Theme.radiusSmall
                    color: UI.Theme.primarySoft
                    RowLayout {
                        anchors.fill: parent
                        anchors.leftMargin: 13
                        anchors.rightMargin: 13
                        UI.Icon { name: "document"; iconSize: 16; iconColor: UI.Theme.primary }
                        Text {
                            Layout.fillWidth: true
                            text: window.tf("pipeline.trust_count", {"count": fullPipelineTrustPopup.paperCount})
                            color: UI.Theme.primaryDark
                            font.family: UI.Theme.fontFamily
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                        }
                    }
                }
                UI.ModernTextField {
                    id: pipelineTrustPassword
                    Layout.fillWidth: true
                    placeholderText: fullPipelineTrustPopup.passwordConfigured
                                     ? window.tr("pipeline.existing_password")
                                     : window.tr("pipeline.new_password")
                    echoMode: TextInput.Password
                }
                UI.ModernTextField {
                    id: pipelineTrustConfirmation
                    visible: !fullPipelineTrustPopup.passwordConfigured
                    Layout.fillWidth: true
                    placeholderText: window.tr("pipeline.confirm_password")
                    echoMode: TextInput.Password
                }
                Text {
                    Layout.fillWidth: true
                    text: window.tr("pipeline.trust_note")
                    wrapMode: Text.WordWrap
                    color: UI.Theme.textMuted
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 11
                    lineHeight: 1.3
                }
                Item { Layout.fillHeight: true }
                RowLayout {
                    Layout.fillWidth: true
                    Item { Layout.fillWidth: true }
                    UI.ModernButton { text: window.tr("action.cancel"); compact: true; onClicked: fullPipelineTrustPopup.close() }
                    UI.ModernButton {
                        text: window.tr("pipeline.enable_and_start")
                        iconName: "play"
                        tone: "primary"
                        compact: true
                        enabled: pipelineTrustPassword.text.length >= 4
                                 && (fullPipelineTrustPopup.passwordConfigured
                                     || pipelineTrustPassword.text === pipelineTrustConfirmation.text)
                        onClicked: {
                            var count = fullPipelineTrustPopup.paperCount
                            var password = pipelineTrustPassword.text
                            var confirmation = pipelineTrustConfirmation.text
                            fullPipelineTrustPopup.close()
                            controller.startFullPipelineWithTrust(count, password, confirmation)
                        }
                    }
                }
            }
        }
    }

    Popup {
        id: fullPipelinePopup
        property var status: ({})
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(760, window.width - 56)
        height: Math.min(620, window.height - 56)
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape
        padding: 0
        background: Rectangle {
            radius: UI.Theme.radiusLarge
            color: UI.Theme.surface
            border.width: 1
            border.color: UI.Theme.border
        }
        contentItem: ColumnLayout {
            spacing: 0
            RowLayout {
                Layout.fillWidth: true
                Layout.preferredHeight: 70
                Layout.leftMargin: 22
                Layout.rightMargin: 18
                Rectangle {
                    Layout.preferredWidth: 38
                    Layout.preferredHeight: 38
                    radius: 19
                    color: UI.Theme.primarySoft
                    UI.Icon { anchors.centerIn: parent; name: "robot"; iconSize: 20; iconColor: UI.Theme.primary }
                }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Text { text: window.tr("pipeline.title"); color: UI.Theme.text; font.family: UI.Theme.fontFamily; font.pixelSize: 18; font.weight: Font.DemiBold }
                    Text { text: String(fullPipelinePopup.status.batch_id || ""); color: UI.Theme.textMuted; font.family: UI.Theme.monoFontFamily; font.pixelSize: 10 }
                }
                UI.StatusBadge {
                    label: window.pipelineStatusLabel(fullPipelinePopup.status.status)
                    tone: String(fullPipelinePopup.status.status || "") === "paused" ? "attention"
                          : String(fullPipelinePopup.status.status || "") === "needs_attention" ? "danger"
                          : "info"
                }
                UI.ModernButton { text: window.tr("action.close"); compact: true; quiet: true; onClicked: fullPipelinePopup.close() }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: UI.Theme.border }
            ColumnLayout {
                Layout.fillWidth: true
                Layout.margins: 20
                spacing: 10
                Text {
                    Layout.fillWidth: true
                    text: window.pipelineSummary(fullPipelinePopup.status)
                    wrapMode: Text.WordWrap
                    color: UI.Theme.textSecondary
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 13
                    font.weight: Font.Medium
                }
                UI.SoftProgress {
                    Layout.fillWidth: true
                    value: Number(fullPipelinePopup.status.progress_percent || 0) / 100
                    accent: String(fullPipelinePopup.status.status || "") === "needs_attention"
                            ? UI.Theme.danger : UI.Theme.primary
                }
                Text {
                    visible: String(fullPipelinePopup.status.last_error || "").length > 0
                    Layout.fillWidth: true
                    text: String(fullPipelinePopup.status.last_error || "")
                    wrapMode: Text.WrapAnywhere
                    color: UI.Theme.danger
                    font.family: UI.Theme.monoFontFamily
                    font.pixelSize: 10
                }
            }
            Rectangle { Layout.fillWidth: true; Layout.preferredHeight: 1; color: UI.Theme.border }
            ListView {
                id: pipelineSlots
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.leftMargin: 18
                Layout.rightMargin: 18
                Layout.topMargin: 10
                clip: true
                spacing: 7
                model: fullPipelinePopup.status.papers || []
                ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }
                delegate: Rectangle {
                    required property var modelData
                    width: pipelineSlots.width
                    height: 62
                    radius: UI.Theme.radiusSmall
                    color: UI.Theme.surfaceSoft
                    border.width: 1
                    border.color: UI.Theme.border
                    RowLayout {
                        anchors.fill: parent
                        anchors.margins: 10
                        Text {
                            text: "#" + String(modelData.slot || "")
                            color: UI.Theme.primary
                            font.family: UI.Theme.monoFontFamily
                            font.pixelSize: 12
                            font.weight: Font.DemiBold
                            Layout.preferredWidth: 34
                        }
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 1
                            Text {
                                Layout.fillWidth: true
                                text: String(modelData.paper_id || modelData.reserved_pipeline_prefix || window.tr("pipeline.assigning_id"))
                                elide: Text.ElideRight
                                color: UI.Theme.text
                                font.family: UI.Theme.fontFamily
                                font.pixelSize: 12
                                font.weight: Font.Medium
                            }
                            Text {
                                Layout.fillWidth: true
                                text: String(modelData.detail || modelData.phase || "")
                                elide: Text.ElideRight
                                color: UI.Theme.textMuted
                                font.family: UI.Theme.fontFamily
                                font.pixelSize: 10
                            }
                        }
                        Text {
                            text: String(modelData.phase || "")
                            color: UI.Theme.textSecondary
                            font.family: UI.Theme.fontFamily
                            font.pixelSize: 11
                            Layout.preferredWidth: 110
                            elide: Text.ElideRight
                        }
                        ColumnLayout {
                            Layout.preferredWidth: 110
                            Text {
                                text: String(modelData.progress_percent || 0) + "%"
                                color: UI.Theme.textSecondary
                                font.family: UI.Theme.fontFamily
                                font.pixelSize: 10
                            }
                            UI.SoftProgress {
                                Layout.fillWidth: true
                                value: Number(modelData.progress_percent || 0) / 100
                                accent: UI.Theme.primary
                            }
                        }
                    }
                }
            }
            RowLayout {
                Layout.fillWidth: true
                Layout.margins: 18
                Text {
                    Layout.fillWidth: true
                    text: String(fullPipelinePopup.status.runtime_running ? window.tr("pipeline.runtime_running") : window.tr("pipeline.runtime_idle"))
                    color: UI.Theme.textMuted
                    font.family: UI.Theme.fontFamily
                    font.pixelSize: 10
                }
                UI.ModernButton {
                    visible: String(fullPipelinePopup.status.status || "") !== "paused"
                    text: window.tr("pipeline.pause")
                    iconName: "pause"
                    compact: true
                    onClicked: {
                        fullPipelinePopup.close()
                        controller.prepareAction("pause_full_pipeline")
                    }
                }
                UI.ModernButton {
                    visible: String(fullPipelinePopup.status.status || "") === "paused"
                    text: window.tr("pipeline.resume")
                    iconName: "play"
                    tone: "primary"
                    compact: true
                    onClicked: {
                        fullPipelinePopup.close()
                        controller.prepareAction("resume_full_pipeline")
                    }
                }
            }
        }
    }

    Rectangle {
        id: toast
        property string tone: "info"
        property alias message: toastText.text
        function show(text, newTone) {
            message = text
            tone = newTone || "info"
            opacity = 1
            toastTimer.restart()
        }
        width: Math.min(390, toastText.implicitWidth + 66)
        height: Math.max(48, toastText.implicitHeight + 24)
        radius: UI.Theme.radius
        color: tone === "danger" ? UI.Theme.dangerSoft : tone === "success" ? UI.Theme.successSoft : tone === "attention" ? UI.Theme.attentionSoft : UI.Theme.primarySoft
        border.width: 1
        border.color: tone === "danger" ? "#F0BBC0" : tone === "success" ? "#BCE8D2" : tone === "attention" ? "#F5D5B3" : "#C7DDFD"
        anchors.right: parent.right
        anchors.rightMargin: 22
        anchors.bottom: parent.bottom
        anchors.bottomMargin: 48
        z: 300
        opacity: 0
        visible: opacity > 0
        Behavior on opacity { NumberAnimation { duration: 160 } }
        RowLayout {
            anchors.fill: parent
            anchors.margins: 12
            spacing: 10
            UI.Icon { name: toast.tone === "danger" ? "warning" : toast.tone === "success" ? "check" : "info"; iconSize: 18; iconColor: toast.tone === "danger" ? UI.Theme.danger : toast.tone === "success" ? UI.Theme.success : toast.tone === "attention" ? UI.Theme.attention : UI.Theme.primary }
            Text { id: toastText; Layout.fillWidth: true; color: UI.Theme.textSecondary; font.family: UI.Theme.fontFamily; font.pixelSize: 12; wrapMode: Text.WordWrap }
        }
        Timer { id: toastTimer; interval: 3200; onTriggered: toast.opacity = 0 }
    }
}
