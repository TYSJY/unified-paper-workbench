import QtQuick

Rectangle {
    id: root
    property string label: ""
    property string tone: "neutral"
    readonly property color toneColor: tone === "info" ? Theme.primary
                                      : tone === "success" ? Theme.success
                                      : tone === "attention" ? Theme.attention
                                      : tone === "danger" ? Theme.danger
                                      : Theme.textSecondary
    readonly property color toneBackground: tone === "info" ? Theme.primarySoft
                                           : tone === "success" ? Theme.successSoft
                                           : tone === "attention" ? Theme.attentionSoft
                                           : tone === "danger" ? Theme.dangerSoft
                                           : Theme.neutralSoft
    implicitWidth: badgeText.implicitWidth + 18
    implicitHeight: 25
    radius: 6
    color: toneBackground
    border.width: 1
    border.color: Qt.rgba(toneColor.r, toneColor.g, toneColor.b, 0.20)
    Text {
        id: badgeText
        anchors.centerIn: parent
        text: root.label
        color: root.toneColor
        font.family: Theme.fontFamily
        font.pixelSize: 12
        font.weight: Font.Medium
    }
}
