import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
FocusScope {
 id:root; required property var keymap; visible:false; signal closed()
 onVisibleChanged:if(visible){editor.text=keymap.exportBindings();editor.forceActiveFocus();}
 Keys.onEscapePressed:{visible=false;closed();}
 Rectangle {anchors.fill:parent;color:"#bb101018"}
 MouseArea {anchors.fill:parent;onWheel:wheel=>wheel.accepted=true}
 Rectangle {anchors.centerIn:parent;width:Math.min(720,root.width-40);height:Math.min(600,root.height-40);color:"#1e2130";border.color:"#555970"
  ColumnLayout {anchors.fill:parent;anchors.margins:22;spacing:12
   TuiLabel {text:"Key bindings";font.pixelSize:20}
   TuiLabel {text:"Choose a command. Empty shortcut restores its default."}
   RowLayout {
    ThemedComboBox {id:command;Accessible.name:"Command";model:root.keymap.editableActions;Layout.fillWidth:true}
    TuiTextField {id:sequence;placeholderText:"Ctrl+Alt+K";Accessible.name:"Shortcut"}
    TuiButton {text:"Apply";onClicked:{if(root.keymap.setBinding(command.currentText,sequence.text))editor.text=root.keymap.exportBindings();}}
   }
   TuiLabel {text:root.keymap.error;color:"#ff9b9b";visible:text.length>0;wrapMode:Text.Wrap;Layout.fillWidth:true}
   TuiLabel {text:"Profile JSON"}
   ScrollView {Layout.fillWidth:true;Layout.fillHeight:true
    TextArea {id:editor;objectName:"keymapJson";font.family:"monospace";color:"#e7e1dc";wrapMode:TextEdit.Wrap;selectByMouse:true;Accessible.name:"Keymap JSON";background:Rectangle{color:"#161923"}}
   }
   RowLayout {TuiButton {text:"Import";onClicked:root.keymap.importBindings(editor.text)}
    TuiButton {text:"Export";onClicked:editor.text=root.keymap.exportBindings()}
    TuiButton {text:"Reset defaults";onClicked:{root.keymap.resetBindings();editor.text=root.keymap.exportBindings();}}
    Item {Layout.fillWidth:true}
    TuiButton {text:"Done";onClicked:{root.visible=false;root.closed();}}
   }
  }
 }
}
