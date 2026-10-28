import QtQuick
import QtQuick.Layouts

Item {
    id: root
    property string label: ""
    property string value: "0"
    property string meta: ""
    property string iconName: "document"
    property string tone: "info"
    readonly property color accent: tone === "success" ? Theme.success
                                  : tone === "attention" ? Theme.attention
                                  : tone === "danger" ? Theme.danger : Theme.primary
    readonly property color soft: tone === "success" ? Theme.successSoft
                                : tone === "attention" ? Theme.attentionSoft
                                : tone === "danger" ? Theme.dangerSoft : Theme.primarySoft
    implicitHeight: 104
    implicitWidth: 190

    Rectangle {
        anchors.fill: card
        anchors.topMargin: 3
        radius: Theme.radius
        color: Theme.shadow
    }
    Rectangle {
        id: card
        anchors.fill: parent
        anchors.bottomMargin: 2
        radius: Theme.radius
        color: Theme.surface
        border.width: 1
        border.color: Theme.border
        RowLayout {
            anchors.fill: parent
            anchors.margins: 16
            spacing: 13
            Rectangle {
                Layout.preferredWidth: 44
                Layout.preferredHeight: 44
                radius: 22
                color: root.soft
                Icon {
                    anchors.centerIn: parent
                    name: root.iconName
                    iconSize: 22
                    iconColor: root.accent
                }
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 1
                Text {
                    text: root.label
                    color: Theme.textSecondary
                    font.family: Theme.fontFamily
                    font.pixelSize: 13
                }
                Text {
                    text: root.value
                    color: Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 25
                    font.weight: Font.DemiBold
                }
                Text {
                    text: root.meta
                    color: Theme.textMuted
                    font.family: Theme.fontFamily
                    font.pixelSize: 11
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                }
            }
        }
    }
}
