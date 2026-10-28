import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    property var paper: ({})
    property bool closeVisible: false
    property bool technicalExpanded: false
    signal closeRequested()
    function tr(key) { return i18n.text(key, i18n.revision) }
    color: Theme.surface
    radius: Theme.radius
    border.width: 1
    border.color: Theme.border

    Flickable {
        id: detailFlick
        anchors.fill: parent
        anchors.margins: 1
        contentWidth: width
        contentHeight: detailColumn.implicitHeight + 28
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar {
            id: detailScrollBar
            policy: ScrollBar.AsNeeded
            width: 8
            contentItem: Rectangle {
                implicitWidth: 5
                radius: 3
                color: "#9EABBC"
                opacity: detailScrollBar.active ? 0.58 : 0.16
                Behavior on opacity { NumberAnimation { duration: 160 } }
            }
            background: Item {}
        }

        ColumnLayout {
            id: detailColumn
            x: 14
            y: 14
            width: detailFlick.width - 28
            spacing: 12

            RowLayout {
                Layout.fillWidth: true
                spacing: 8
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 3
                    Text {
                        Layout.fillWidth: true
                        text: root.paper.paperId || root.tr("detail.select_paper")
                        color: Theme.text
                        font.family: Theme.fontFamily
                        font.pixelSize: 14
                        font.weight: Font.DemiBold
                        wrapMode: Text.WrapAnywhere
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.paper.currentStage ? root.paper.currentStage + " · " + root.paper.updatedAt : root.tr("detail.select_hint")
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                    }
                }
                StatusBadge { visible: !!root.paper.paperId; label: root.paper.statusLabel || root.tr("detail.not_selected"); tone: root.paper.statusTone || "neutral" }
                ModernButton { visible: root.closeVisible; text: ""; iconName: "close"; compact: true; quiet: true; implicitWidth: 34; onClicked: root.closeRequested() }
            }

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: statusContent.implicitHeight + 28
                radius: Theme.radius
                color: Theme.surfaceSoft
                border.width: 1
                border.color: Theme.border
                ColumnLayout {
                    id: statusContent
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 9
                    RowLayout {
                        Layout.fillWidth: true
                        Text { text: root.tr("detail.current_status"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.DemiBold }
                        Item { Layout.fillWidth: true }
                        Text { text: Math.round((root.paper.progress || 0) * 100) + "%"; color: Theme.primary; font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.DemiBold }
                    }
                    SoftProgress { Layout.fillWidth: true; value: root.paper.progress || 0; accent: root.paper.statusTone === "danger" ? Theme.danger : Theme.primary }
                    Text { Layout.fillWidth: true; text: root.paper.stepLabel || root.tr("detail.not_selected_step"); color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; wrapMode: Text.WordWrap }
                    Text { Layout.fillWidth: true; text: root.paper.detail || ""; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 11; wrapMode: Text.WordWrap }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                implicitHeight: nextContent.implicitHeight + 28
                radius: Theme.radius
                color: Theme.primarySoft
                border.width: 1
                border.color: "#CFE2FF"
                ColumnLayout {
                    id: nextContent
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 5
                    Text { text: root.tr("detail.next_action"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.DemiBold }
                    Text { Layout.fillWidth: true; text: root.paper.nextActionLabel || root.tr("detail.select_for_next"); color: Theme.primaryDark; font.family: Theme.fontFamily; font.pixelSize: 14; font.weight: Font.Medium; wrapMode: Text.WordWrap }
                }
            }

            Text { text: root.tr("detail.stage_progress"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.DemiBold }
            Repeater {
                model: root.paper.stageCards || []
                delegate: Rectangle {
                    required property var modelData
                    Layout.fillWidth: true
                    implicitHeight: stageInner.implicitHeight + 24
                    radius: Theme.radiusSmall
                    color: Theme.surface
                    border.width: 1
                    border.color: Theme.border
                    RowLayout {
                        id: stageInner
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 10
                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            RowLayout {
                                Layout.fillWidth: true
                                Text { text: modelData.label; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.Medium }
                                Item { Layout.fillWidth: true }
                                StatusBadge { label: modelData.statusLabel; tone: modelData.tone }
                            }
                            SoftProgress {
                                Layout.fillWidth: true
                                value: modelData.progress
                                accent: modelData.tone === "success" ? Theme.success
                                      : modelData.tone === "danger" ? Theme.danger
                                      : modelData.tone === "attention" ? Theme.attention
                                      : Theme.primary
                            }
                        }
                    }
                }
            }

            Text { text: root.tr("detail.recent_events"); color: Theme.text; font.family: Theme.fontFamily; font.pixelSize: 13; font.weight: Font.DemiBold; Layout.topMargin: 2 }
            Rectangle {
                Layout.fillWidth: true
                implicitHeight: eventColumn.implicitHeight + 24
                radius: Theme.radiusSmall
                color: Theme.surfaceSoft
                border.width: 1
                border.color: Theme.border
                ColumnLayout {
                    id: eventColumn
                    anchors.fill: parent
                    anchors.margins: 12
                    spacing: 8
                    Repeater {
                        model: root.paper.events || []
                        delegate: RowLayout {
                            required property string modelData
                            required property int index
                            Layout.fillWidth: true
                            spacing: 8
                            Rectangle { width: 7; height: 7; radius: 4; color: index === 0 ? Theme.primary : Theme.success }
                            Text { Layout.fillWidth: true; text: modelData; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11; wrapMode: Text.WordWrap }
                        }
                    }
                }
            }

            ModernButton {
                Layout.fillWidth: true
                text: root.tr(root.technicalExpanded ? "detail.hide_technical" : "detail.show_technical")
                iconName: "info"
                quiet: true
                compact: true
                onClicked: root.technicalExpanded = !root.technicalExpanded
            }
            Rectangle {
                visible: root.technicalExpanded
                Layout.fillWidth: true
                implicitHeight: technicalText.implicitHeight + 24
                radius: Theme.radiusSmall
                color: "#111827"
                Text {
                    id: technicalText
                    anchors.fill: parent
                    anchors.margins: 12
                    text: root.paper.technical || root.tr("detail.no_technical")
                    color: "#D1D5DB"
                    font.family: "Consolas"
                    font.pixelSize: 10
                    wrapMode: Text.WrapAnywhere
                    maximumLineCount: 28
                    elide: Text.ElideRight
                }
            }
            Item { Layout.preferredHeight: 8 }
        }
    }
}
