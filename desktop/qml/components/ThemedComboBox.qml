pragma ComponentBehavior: Bound
import QtQuick
import QtQuick.Controls

ComboBox {
    id: root
    // Basic's default delegate pairs highlightedText with palette.light,
    // rather than highlight, producing low contrast with a dark app palette.
    delegate: ItemDelegate {
        id: option
        required property int index
        width: ListView.view ? ListView.view.width : root.width
        text: root.textAt(index)
        highlighted: root.highlightedIndex === index
        hoverEnabled: root.hoverEnabled
        palette: root.palette
        font.weight: root.currentIndex === index ? Font.DemiBold : Font.Normal
        background: Rectangle {
            color: option.highlighted ? root.palette.highlight
                : option.down ? root.palette.midlight : root.palette.base
        }
    }
}
