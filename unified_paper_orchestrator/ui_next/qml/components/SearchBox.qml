import QtQuick
import QtQuick.Controls

TextField {
    id: root
    property string iconName: "search"
    implicitHeight: 38
    leftPadding: 39
    rightPadding: 13
    color: Theme.text
    placeholderTextColor: Theme.textMuted
    selectedTextColor: "white"
    selectionColor: Theme.primary
    font.family: Theme.fontFamily
    font.pixelSize: 13
    background: Rectangle {
        radius: Theme.radiusSmall
        color: Theme.surface
        border.width: 1
        border.color: root.activeFocus ? Theme.primary : root.hovered ? Theme.borderStrong : Theme.border
        Behavior on border.color { ColorAnimation { duration: 130 } }
        Icon {
            anchors.left: parent.left
            anchors.leftMargin: 13
            anchors.verticalCenter: parent.verticalCenter
            name: root.iconName
            iconSize: 16
            iconColor: root.activeFocus ? Theme.primary : Theme.textMuted
        }
    }
}
