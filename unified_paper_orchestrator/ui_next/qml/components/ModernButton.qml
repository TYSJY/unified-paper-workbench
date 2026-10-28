import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Button {
    id: root
    property string iconName: ""
    property string tone: "secondary"
    property bool compact: false
    property bool quiet: false
    property int minimumButtonWidth: compact ? 78 : 96
    property string toolTipText: text
    readonly property int effectiveIconSize: compact ? 15 : 17
    readonly property int contentSpacing: iconName.length > 0 && text.length > 0 ? 7 : 0
    readonly property real naturalContentWidth: buttonTextMeasure.implicitWidth
                                                + (iconName.length > 0 ? effectiveIconSize : 0)
                                                + contentSpacing

    implicitHeight: compact ? 34 : 40
    implicitWidth: Math.max(minimumButtonWidth, naturalContentWidth + leftPadding + rightPadding)
    leftPadding: compact ? 12 : 15
    rightPadding: compact ? 12 : 15
    focusPolicy: Qt.StrongFocus
    hoverEnabled: true
    Accessible.name: toolTipText

    Text {
        id: buttonTextMeasure
        visible: false
        font.family: Theme.fontFamily
        font.pixelSize: root.compact ? 13 : 14
        font.weight: root.tone === "primary" || root.tone === "danger" ? Font.DemiBold : Font.Medium
        text: root.text
    }

    contentItem: Row {
        id: contentRow
        spacing: root.contentSpacing
        anchors.centerIn: parent
        Icon {
            id: buttonIcon
            visible: root.iconName.length > 0
            name: root.iconName
            iconSize: root.effectiveIconSize
            iconColor: root.tone === "primary" || root.tone === "danger" ? "white" : Theme.textSecondary
        }
        Text {
            id: buttonLabel
            width: Math.min(implicitWidth, Math.max(0, root.availableWidth
                                                    - (buttonIcon.visible ? buttonIcon.implicitWidth : 0)
                                                    - root.contentSpacing))
            text: root.text
            color: !root.enabled ? Theme.textMuted
                 : root.tone === "primary" || root.tone === "danger" ? "white" : Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: root.compact ? 13 : 14
            font.weight: root.tone === "primary" || root.tone === "danger" ? Font.DemiBold : Font.Medium
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
    }

    ToolTip.visible: hovered && (buttonLabel.truncated || text.length === 0) && toolTipText.length > 0
    ToolTip.text: toolTipText
    ToolTip.delay: 450

    background: Item {
        implicitWidth: 96
        implicitHeight: root.compact ? 34 : 40

        Rectangle {
            id: shadowRect
            anchors.fill: buttonRect
            anchors.topMargin: 2
            radius: buttonRect.radius
            color: root.tone === "primary" ? "#221167D8"
                 : root.tone === "danger" ? "#22B9252E" : "#100E1A2B"
            opacity: root.enabled && !root.quiet && !root.down ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: 120 } }
        }
        Rectangle {
            id: buttonRect
            anchors.fill: parent
            anchors.bottomMargin: root.down ? 0 : 1
            radius: Theme.radiusSmall
            color: root.quiet ? "transparent"
                 : root.tone === "primary" ? Theme.primary
                 : root.tone === "danger" ? Theme.danger
                 : root.hovered ? Theme.surfaceHover : Theme.surface
            gradient: root.tone === "primary" ? primaryGradient
                    : root.tone === "danger" ? dangerGradient : null
            border.width: root.quiet ? 0 : 1
            border.color: root.tone === "primary" ? "#246FD0"
                        : root.tone === "danger" ? "#C72D36"
                        : root.activeFocus ? Theme.primary : root.hovered ? Theme.borderStrong : Theme.border
            opacity: root.enabled ? 1 : 0.56
            scale: root.down ? 0.985 : 1
            Behavior on color { ColorAnimation { duration: 130 } }
            Behavior on border.color { ColorAnimation { duration: 130 } }
            Behavior on scale { NumberAnimation { duration: 90; easing.type: Easing.OutCubic } }
        }
        Gradient {
            id: primaryGradient
            GradientStop { position: 0; color: root.hovered ? "#2A86FF" : Theme.primary }
            GradientStop { position: 1; color: root.down ? "#095BB9" : Theme.primaryDark }
        }
        Gradient {
            id: dangerGradient
            GradientStop { position: 0; color: root.hovered ? "#E14951" : Theme.danger }
            GradientStop { position: 1; color: root.down ? "#A41F27" : Theme.dangerDark }
        }
    }
}
