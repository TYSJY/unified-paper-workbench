import QtQuick
import QtQuick.Layouts

Rectangle {
    id: root
    property var controllerRef
    property string pageKey: "writing"
    property string pageTitle: tr("nav.writing")
    property bool runAllowed: true
    function tr(key) { return i18n.text(key, i18n.revision) }
    radius: Theme.radius
    color: Theme.surface
    border.width: 1
    border.color: Theme.border
    implicitHeight: 68

    RowLayout {
        anchors.fill: parent
        anchors.margins: 12
        spacing: 10
        Rectangle {
            width: 36
            height: 36
            radius: 10
            color: Theme.primarySoft
            Icon { anchors.centerIn: parent; name: root.pageKey === "figures" ? "figures" : root.pageKey === "journal_author" ? "people" : root.pageKey === "submission" ? "submission" : "document"; iconColor: Theme.primary; iconSize: 19 }
        }
        ColumnLayout {
            Layout.preferredWidth: root.width < 900 ? 150 : 230
            spacing: 1
            Text { text: root.pageTitle + " " + root.tr("stage.task_suffix"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 14; font.weight: Font.DemiBold }
            Text { text: root.tr(root.pageKey === "submission" ? "stage.smart_queue_description" : "stage.queue_description"); color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11 }
        }
        ModernButton {
            text: root.tr(root.pageKey === "submission" ? "action.run_smart_submission" : "action.run_all")
            iconName: "play"
            tone: "primary"
            compact: true
            enabled: root.runAllowed
            toolTipText: root.runAllowed ? text : root.tr("commercial.read_only_tooltip")
            onClicked: root.controllerRef.prepareAction("start_stage")
        }
        ModernButton {
            visible: root.pageKey === "submission"
            text: root.tr("action.check_all_submissions")
            iconName: "refresh"
            compact: true
            onClicked: root.controllerRef.prepareAction("check_all_submissions")
        }
        ModernButton { text: root.tr(root.pageKey === "submission" ? "action.pause_smart_submission" : "action.safe_pause"); iconName: "pause"; compact: true; onClicked: root.controllerRef.prepareAction("pause_stage") }
        ModernButton {
            visible: root.pageKey === "writing"
            text: root.tr("action.new_writing")
            iconName: "plus"
            compact: true
            enabled: root.runAllowed
            toolTipText: root.runAllowed ? text : root.tr("commercial.read_only_tooltip")
            onClicked: root.controllerRef.prepareAction("new_writing")
        }
        Item { Layout.fillWidth: true }
        ModernButton {
            objectName: "openSelectedPaperPdf"
            visible: root.pageKey !== "writing" && Boolean(root.controllerRef.selectedPaperId)
            text: root.tr(root.width < 900 ? "action.paper_pdf_short" : "action.open_paper_pdf")
            iconName: "document"
            compact: true
            onClicked: root.controllerRef.prepareSelectedAction("open_paper_pdf")
        }
        ModernButton {
            visible: root.controllerRef.selectedActions.length > 0
            text: root.tr("action.selected_actions")
            iconName: "more"
            compact: true
            onClicked: selectedActionMenu.popup()
        }
    }

    ActionMenu {
        id: selectedActionMenu
        actions: root.controllerRef.selectedActions
        controllerRef: root.controllerRef
    }
}
