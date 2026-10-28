import QtQuick
import QtQuick.Controls

ComboBox {
    id: root
    property bool compact: false
    property string currentLabel: currentIndex >= 0 ? i18n.languages[currentIndex].label : ""
    implicitWidth: compact ? 58 : Math.max(116, languageLabelMeasure.implicitWidth + 48)
    implicitHeight: 38
    model: i18n.languages
    textRole: "label"
    valueRole: "code"

    function tr(key) { return i18n.text(key, i18n.revision) }
    function syncIndex() {
        var availableLanguages = i18n.languages
        for (var index = 0; index < availableLanguages.length; index++) {
            if (availableLanguages[index].code === i18n.locale) {
                currentIndex = index
                return
            }
        }
        currentIndex = 0
    }

    Component.onCompleted: syncIndex()
    onActivated: i18n.setLocale(currentValue)

    Connections {
        target: i18n
        function onLanguageChanged() { root.syncIndex() }
    }

    Text {
        id: languageLabelMeasure
        visible: false
        font.family: Theme.fontFamily
        font.pixelSize: 12
        text: root.currentLabel
    }

    delegate: ItemDelegate {
        required property var modelData
        required property int index
        width: root.popup.width
        height: 36
        highlighted: root.highlightedIndex === index
        onClicked: {
            root.currentIndex = index
            i18n.setLocale(modelData.code)
            root.popup.close()
        }
        contentItem: Text {
            text: modelData.label
            color: Theme.text
            font.family: Theme.fontFamily
            font.pixelSize: 12
            verticalAlignment: Text.AlignVCenter
        }
        background: Rectangle {
            radius: 7
            color: parent.highlighted ? Theme.primarySoft : "transparent"
        }
    }

    contentItem: Row {
        leftPadding: 11
        spacing: 7
        Icon {
            anchors.verticalCenter: parent.verticalCenter
            name: "globe"
            iconSize: 15
            iconColor: root.activeFocus ? Theme.primary : Theme.textSecondary
        }
        Text {
            anchors.verticalCenter: parent.verticalCenter
            text: root.compact
                  ? (root.currentIndex >= 0 ? i18n.languages[root.currentIndex].shortLabel : "")
                  : root.currentLabel
            color: Theme.textSecondary
            font.family: Theme.fontFamily
            font.pixelSize: 12
            font.weight: Font.Medium
        }
    }

    indicator: Icon {
        visible: !root.compact
        x: root.width - width - 10
        y: (root.height - height) / 2
        name: "chevronDown"
        iconSize: 12
        iconColor: Theme.textMuted
    }

    background: Rectangle {
        radius: Theme.radiusSmall
        color: root.hovered ? Theme.surfaceHover : Theme.surface
        border.width: 1
        border.color: root.activeFocus ? Theme.primary : root.hovered ? Theme.borderStrong : Theme.border
        Behavior on color { ColorAnimation { duration: 120 } }
        Behavior on border.color { ColorAnimation { duration: 120 } }
    }

    popup: Popup {
        y: root.height + 5
        width: Math.max(root.width, 138)
        implicitHeight: contentItem.implicitHeight + 10
        padding: 5
        contentItem: ListView {
            implicitHeight: contentHeight
            model: root.popup.visible ? root.delegateModel : null
            currentIndex: root.highlightedIndex
            clip: true
        }
        background: Rectangle {
            radius: Theme.radiusSmall
            color: Theme.surface
            border.width: 1
            border.color: Theme.border
        }
    }

    ToolTip.visible: hovered && compact
    ToolTip.text: tr("app.language")
    Accessible.name: tr("app.language")
}
