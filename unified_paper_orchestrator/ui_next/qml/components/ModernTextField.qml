import QtQuick
import QtQuick.Controls

TextField {
    id: control

    implicitHeight: 38
    leftPadding: 12
    rightPadding: 12
    topPadding: 7
    bottomPadding: 7
    color: Theme.text
    placeholderTextColor: Theme.textMuted
    selectionColor: Theme.primarySoft
    selectedTextColor: Theme.text
    font.family: Theme.fontFamily
    font.pixelSize: 13
    selectByMouse: true

    background: Rectangle {
        radius: Theme.radiusSmall
        color: control.enabled ? Theme.surface : Theme.surfaceSoft
        border.width: 1
        border.color: control.activeFocus ? Theme.primary
                      : control.hovered ? Theme.borderStrong : Theme.border
        Behavior on border.color { ColorAnimation { duration: 120 } }
    }
}
