import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root
    property var controllerRef
    property string selectedId: ""
    property bool compact: false
    signal paperActivated(string paperId)
    radius: Theme.radius
    color: Theme.surface
    border.width: 1
    border.color: Theme.border
    clip: true

    function tr(key) { return i18n.text(key, i18n.revision) }
    function tf(key, values) { return i18n.formatText(key, values, i18n.revision) }

    property var statusOptions: [
        {"label": root.tr("list.all_statuses"), "value": "all"},
        {"label": root.tr("list.active"), "value": "active"},
        {"label": root.tr("list.human"), "value": "waiting"},
        {"label": root.tr("list.blocked"), "value": "blocked"},
        {"label": root.tr("list.complete"), "value": "complete"}
    ]

    ActionMenu {
        id: contextActionMenu
        actions: root.controllerRef.selectedActions
        controllerRef: root.controllerRef
    }

    Timer {
        id: searchDelay
        interval: 170
        repeat: false
        onTriggered: root.controllerRef.setSearch(searchBox.text)
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 58
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 10
                Text {
                    text: root.tr("list.title")
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 15
                    font.weight: Font.DemiBold
                }
                Text {
                    text: root.tf("list.count", {"count": root.controllerRef.paperModel.count})
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                }
                Item { Layout.fillWidth: true }
                SearchBox {
                    id: searchBox
                    Layout.preferredWidth: root.compact ? 230 : 310
                    placeholderText: root.tr("list.search")
                    onTextChanged: searchDelay.restart()
                }
                ComboBox {
                    id: statusFilter
                    Layout.preferredWidth: 122
                    Layout.preferredHeight: 38
                    model: root.statusOptions
                    textRole: "label"
                    valueRole: "value"
                    font.family: Theme.fontFamily
                    font.pixelSize: 13
                    leftPadding: 12
                    rightPadding: 30
                    onActivated: root.controllerRef.setStatusFilter(currentValue)
                    contentItem: Text {
                        leftPadding: 3
                        text: statusFilter.displayText
                        color: Theme.textSecondary
                        font: statusFilter.font
                        verticalAlignment: Text.AlignVCenter
                        elide: Text.ElideRight
                    }
                    indicator: Icon {
                        name: "chevronDown"
                        iconSize: 12
                        iconColor: Theme.textMuted
                        x: statusFilter.width - width - 11
                        y: (statusFilter.height - height) / 2
                    }
                    background: Rectangle {
                        radius: Theme.radiusSmall
                        color: Theme.surface
                        border.width: 1
                        border.color: statusFilter.activeFocus ? Theme.primary : Theme.border
                    }
                    popup: Popup {
                        y: statusFilter.height + 5
                        width: statusFilter.width
                        implicitHeight: contentItem.implicitHeight + 8
                        padding: 4
                        background: Rectangle {
                            radius: Theme.radiusSmall
                            color: Theme.surface
                            border.width: 1
                            border.color: Theme.border
                        }
                        contentItem: ListView {
                            implicitHeight: contentHeight
                            model: statusFilter.popup.visible ? statusFilter.delegateModel : null
                            currentIndex: statusFilter.highlightedIndex
                            clip: true
                        }
                    }
                    delegate: ItemDelegate {
                        required property var modelData
                        width: statusFilter.width - 8
                        height: 34
                        contentItem: Text {
                            text: modelData.label
                            color: highlighted ? Theme.primary : Theme.textSecondary
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                            verticalAlignment: Text.AlignVCenter
                        }
                        background: Rectangle {
                            radius: 5
                            color: highlighted ? Theme.primarySoft : "transparent"
                        }
                    }
                }
            }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: Theme.border }

        Item {
            id: headerRow
            Layout.fillWidth: true
            Layout.preferredHeight: 42
            Rectangle { anchors.fill: parent; color: Theme.surfaceSoft }
            RowLayout {
                anchors.fill: parent
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                spacing: 0
                Text { text: root.tr("list.paper_id"); Layout.preferredWidth: root.compact ? 260 : 300; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                Text { text: root.tr("list.status"); Layout.preferredWidth: 104; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                Text { visible: !root.compact; text: root.tr("list.stage_step"); Layout.preferredWidth: 178; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                Text { text: root.tr("list.next"); Layout.fillWidth: true; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                Text { text: root.tr("list.progress"); Layout.preferredWidth: 132; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
                Text { visible: !root.compact; text: root.tr("list.updated"); Layout.preferredWidth: 94; horizontalAlignment: Text.AlignRight; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12; font.weight: Font.DemiBold }
            }
        }

        Rectangle { Layout.fillWidth: true; height: 1; color: Theme.border }

        ListView {
            id: paperList
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            model: root.controllerRef.paperModel
            boundsBehavior: Flickable.StopAtBounds
            reuseItems: true
            ScrollBar.vertical: ScrollBar {
                id: paperScrollBar
                policy: ScrollBar.AsNeeded
                width: 8
                contentItem: Rectangle {
                    implicitWidth: 5
                    radius: 3
                    color: paperScrollBar.pressed ? Theme.borderStrong : "#9EABBC"
                    opacity: paperScrollBar.active ? 0.62 : 0.18
                    Behavior on opacity { NumberAnimation { duration: 160 } }
                }
                background: Item {}
            }

            delegate: ItemDelegate {
                id: rowDelegate
                width: paperList.width
                height: 54
                hoverEnabled: true
                padding: 0
                onClicked: root.paperActivated(paperId)
                TapHandler {
                    acceptedButtons: Qt.RightButton
                    onTapped: {
                        root.paperActivated(paperId)
                        contextActionMenu.popup()
                    }
                }
                background: Rectangle {
                    color: root.selectedId === paperId ? Theme.primarySoft
                         : rowDelegate.hovered ? Theme.surfaceHover : Theme.surface
                    Behavior on color { ColorAnimation { duration: 110 } }
                    Rectangle {
                        width: 3
                        height: parent.height - 12
                        anchors.left: parent.left
                        anchors.verticalCenter: parent.verticalCenter
                        radius: 2
                        color: statusTone === "danger" ? Theme.danger
                             : root.selectedId === paperId ? Theme.primary : "transparent"
                    }
                    Rectangle { anchors.bottom: parent.bottom; anchors.left: parent.left; anchors.right: parent.right; height: 1; color: Theme.border }
                }
                contentItem: RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 16
                    anchors.rightMargin: 16
                    spacing: 0
                    ColumnLayout {
                        Layout.preferredWidth: root.compact ? 260 : 300
                        spacing: 1
                        Text {
                            Layout.fillWidth: true
                            text: paperId
                            color: Theme.text
                            font.family: Theme.fontFamily
                            font.pixelSize: 13
                            font.weight: Font.Medium
                            elide: Text.ElideRight
                        }
                        Text {
                            Layout.fillWidth: true
                            text: title
                            color: Theme.textMuted
                            font.family: Theme.fontFamily
                            font.pixelSize: 10
                            elide: Text.ElideRight
                        }
                    }
                    Item {
                        Layout.preferredWidth: 104
                        Layout.fillHeight: true
                        StatusBadge { anchors.left: parent.left; anchors.verticalCenter: parent.verticalCenter; label: statusLabel; tone: statusTone }
                    }
                    ColumnLayout {
                        visible: !root.compact
                        Layout.preferredWidth: 178
                        spacing: 1
                        Text { text: currentStage; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 12 }
                        Text { text: stepLabel; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 10 }
                    }
                    Text {
                        Layout.fillWidth: true
                        Layout.rightMargin: 12
                        text: nextActionLabel
                        color: isBlocked ? Theme.danger : Theme.textSecondary
                        font.family: Theme.fontFamily
                        font.pixelSize: 12
                        elide: Text.ElideRight
                    }
                    Item {
                        Layout.preferredWidth: 132
                        Layout.fillHeight: true
                        Column {
                            width: 110
                            anchors.verticalCenter: parent.verticalCenter
                            spacing: 5
                            Row {
                                spacing: 5
                                Text { text: Math.round(progress * 100) + "%"; color: Theme.textSecondary; font.family: Theme.fontFamily; font.pixelSize: 11 }
                                Text { text: queueLabel; color: Theme.textMuted; font.family: Theme.fontFamily; font.pixelSize: 10 }
                            }
                            SoftProgress {
                                width: parent.width
                                value: progress
                                accent: isComplete ? Theme.success
                                      : isBlocked ? Theme.danger
                                      : statusTone === "attention" ? Theme.attention
                                      : Theme.primary
                            }
                        }
                    }
                    Text {
                        visible: !root.compact
                        Layout.preferredWidth: 94
                        text: updatedAt
                        horizontalAlignment: Text.AlignRight
                        color: Theme.textMuted
                        font.family: Theme.fontFamily
                        font.pixelSize: 11
                    }
                }
            }

            footer: Item {
                width: paperList.width
                height: 54
                Text {
                    anchors.centerIn: parent
                    text: paperList.count === 0 ? root.tr("list.empty") : root.tf("list.shown", {"count": paperList.count})
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 12
                }
            }
        }
    }
}
