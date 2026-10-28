import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: root
    property string iconName: "home"
    property bool selected: false
    implicitHeight: 52
    implicitWidth: Math.max(118, navContent.implicitWidth + 34)
    hoverEnabled: true
    padding: 0

    contentItem: RowLayout {
        id: navContent
        anchors.centerIn: parent
        spacing: 9
        Icon {
            name: root.iconName
            iconSize: 18
            iconColor: root.selected ? Theme.primary : Theme.textSecondary
        }
        Text {
            text: root.text
            color: root.selected ? Theme.primary : Theme.textSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 14
            font.weight: root.selected ? Font.DemiBold : Font.Medium
        }
    }
    background: Item {
        Rectangle {
            anchors.fill: parent
            anchors.margins: 5
            radius: Theme.radiusSmall
            color: root.selected ? Theme.primarySoft : root.hovered ? Theme.surfaceSoft : "transparent"
            Behavior on color { ColorAnimation { duration: 140 } }
        }
        Rectangle {
            width: Math.min(54, parent.width - 28)
            height: 3
            radius: 2
            color: Theme.primary
            anchors.bottom: parent.bottom
            anchors.horizontalCenter: parent.horizontalCenter
            opacity: root.selected ? 1 : 0
            scale: root.selected ? 1 : 0.45
            Behavior on opacity { NumberAnimation { duration: 150 } }
            Behavior on scale { NumberAnimation { duration: 170; easing.type: Easing.OutBack } }
        }
    }
}
