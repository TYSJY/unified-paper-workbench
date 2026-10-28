import QtQuick

Text {
    id: root
    property string name: "document"
    property int iconSize: 18
    property color iconColor: Theme.textSecondary
    readonly property var glyphs: ({
        "home": "\uE80F",
        "document": "\uE8A5",
        "figures": "\uE91B",
        "people": "\uE716",
        "submission": "\uE898",
        "refresh": "\uE72C",
        "settings": "\uE713",
        "account": "\uE77B",
        "globe": "\uE774",
        "more": "\uE712",
        "stop": "\uE71A",
        "play": "\uE768",
        "pause": "\uE769",
        "search": "\uE721",
        "folder": "\uE8B7",
        "chevronDown": "\uE70D",
        "chevronRight": "\uE76C",
        "external": "\uE8A7",
        "robot": "\uE99A",
        "clock": "\uE823",
        "check": "\uE73E",
        "warning": "\uE7BA",
        "shield": "\uE83D",
        "close": "\uE711",
        "copy": "\uE8C8",
        "filter": "\uE71C",
        "plus": "\uE710",
        "info": "\uE946",
        "upload": "\uE898",
        "download": "\uE896",
        "edit": "\uE70F",
        "archive": "\uE7B8",
        "trash": "\uE74D",
        "logout": "\uE8AC",
        "forward": "\uE72A",
        "package": "\uE7B8",
        "person": "\uE77B"
    })
    text: glyphs[name] || glyphs.document
    color: iconColor
    font.family: Theme.iconFont
    font.pixelSize: iconSize
    horizontalAlignment: Text.AlignHCenter
    verticalAlignment: Text.AlignVCenter
    renderType: Text.NativeRendering
}
