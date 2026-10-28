import QtQuick
import QtQuick.Controls

Menu {
    id: root
    property var actions: []
    property var controllerRef
    implicitWidth: 260
    padding: 6

    background: Rectangle {
        color: Theme.surface
        radius: Theme.radius
        border.width: 1
        border.color: Theme.border
    }

    Instantiator {
        model: root.actions
        delegate: MenuItem {
            required property var modelData
            height: 38
            text: String(modelData.label || "")
            font.family: Theme.fontFamily
            font.pixelSize: 13
            leftPadding: 12
            rightPadding: 12
            onTriggered: root.controllerRef.prepareSelectedAction(String(modelData.key || ""))
            contentItem: Row {
                spacing: 9
                Icon {
                    name: String(modelData.icon || "document")
                    iconSize: 15
                    iconColor: modelData.tone === "danger" ? Theme.danger
                             : modelData.tone === "attention" ? Theme.attention
                             : modelData.tone === "primary" ? Theme.primary
                             : Theme.textSecondary
                    anchors.verticalCenter: parent.verticalCenter
                }
                Text {
                    text: String(modelData.label || "")
                    color: modelData.tone === "danger" ? Theme.danger : Theme.text
                    font.family: Theme.fontFamily
                    font.pixelSize: 13
                    verticalAlignment: Text.AlignVCenter
                }
            }
            background: Rectangle {
                radius: Theme.radiusSmall
                color: highlighted ? Theme.surfaceHover : "transparent"
            }
        }
        onObjectAdded: function(index, object) { root.insertItem(index, object) }
        onObjectRemoved: function(index, object) { root.removeItem(object) }
    }
}
