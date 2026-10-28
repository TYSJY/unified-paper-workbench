import QtQuick

Rectangle {
    id: root
    property real value: 0
    property color accent: Theme.primary
    implicitHeight: 6
    radius: height / 2
    color: Theme.neutralSoft
    clip: true
    Rectangle {
        width: Math.max(0, Math.min(parent.width, parent.width * root.value))
        height: parent.height
        radius: parent.radius
        color: root.accent
        Behavior on width { NumberAnimation { duration: 220; easing.type: Easing.OutCubic } }
    }
}
