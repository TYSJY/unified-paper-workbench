import QtQuick
import QtQuick.Controls

ModernComboBox {
    id: control
    property string selectedValue: ""
    property var choices: []
    property string emptyLabel: ""
    property string unavailableLabel: ""
    property bool allowEmpty: true
    property var entries: {
        var result = allowEmpty ? [{value: "", label: emptyLabel}] : []
        for (var i = 0; i < choices.length; i++) result.push(choices[i])
        if (selectedValue && !result.some(function(item) { return item.value === selectedValue }))
            result.push({value: selectedValue, label: unavailableLabel + " · " + selectedValue})
        return result
    }
    property bool selectionAvailable: (allowEmpty && !selectedValue)
        || choices.some(function(item) { return item.value === selectedValue })
    model: entries.map(function(item) { return item.label })
    currentIndex: entries.findIndex(function(item) { return item.value === selectedValue })
    onActivated: selectedValue = entries[currentIndex].value
    ToolTip.visible: hovered && !selectionAvailable
    ToolTip.text: selectedValue + " · " + unavailableLabel
}
