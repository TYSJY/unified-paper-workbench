import QtQuick
import QtQuick.Controls

ComboBox {
    id: control

    implicitHeight: 38
    leftPadding: 12
    rightPadding: 34
    font.family: Theme.fontFamily
    font.pixelSize: 13

    contentItem: Text {
        leftPadding: 1
        rightPadding: 2
        text: control.displayText
        color: control.enabled ? Theme.textSecondary : Theme.textMuted
        font: control.font
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    indicator: Icon {
        name: "chevronDown"
        iconSize: 12
        iconColor: Theme.textMuted
        x: control.width - width - 12
        y: (control.height - height) / 2
    }

    background: Rectangle {
        radius: Theme.radiusSmall
        color: control.enabled ? Theme.surface : Theme.surfaceSoft
        border.width: 1
        border.color: control.activeFocus ? Theme.primary
                      : control.hovered ? Theme.borderStrong : Theme.border
        Behavior on border.color { ColorAnimation { duration: 120 } }
    }

    delegate: ItemDelegate {
        required property int index
        width: control.width - 8
        height: 36
        leftPadding: 10
        rightPadding: 10
        contentItem: Text {
            text: control.textAt(index)
            color: highlighted ? Theme.primaryDark : Theme.textSecondary
            font: control.font
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 6
            color: highlighted ? Theme.primarySoft : "transparent"
        }
    }

    popup: Popup {
        y: control.height + 5
        width: control.width
        implicitHeight: Math.min(contentItem.implicitHeight + 8, 310)
        padding: 4
        background: Rectangle {
            radius: Theme.radiusSmall
            color: Theme.surface
            border.width: 1
            border.color: Theme.border
        }
        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollIndicator.vertical: ScrollIndicator {}
        }
    }
}
