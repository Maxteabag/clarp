import QtQuick
import QtTest
import "../../qml/components"
TestCase {
 id:tc;name:"KeymapCustomization";when:windowShown;visible:true;width:800;height:650
 KeyboardMap {id:map;hasAgent:true;hasRows:true;canSend:true;hasAttention:true}
 KeymapEditor {id:editor;anchors.fill:parent;keymap:map}
 function init(){map.resetBindings();map.contextName="pane";editor.visible=false;}
 function cleanup(){map.resetBindings();}
 function test_overrideAndExport(){
  verify(map.setBinding("split-right","Ctrl+Alt+P"));
  verify(map.shortcuts.some(e=>e.key==="Ctrl+Alt+P"&&e.action==="split-right"));
  verify(!map.shortcuts.some(e=>e.key==="Ctrl+Alt+V"));
  const backup=map.exportBindings();map.resetBindings();verify(map.importBindings(backup));
  compare(JSON.parse(map.exportBindings()).bindings["split-right"],"Ctrl+Alt+P");
  map.contextName="composer";verify(map.shortcuts.some(e=>e.action==="split-right"&&e.key==="Ctrl+Alt+P"));
  map.contextName="modal";compare(map.shortcuts.length,1);
 }
 function test_conflictsAreAtomic(){
  const prior=map.exportBindings();verify(!map.setBinding("zoom","Ctrl+K"));compare(map.exportBindings(),prior);
  verify(!map.setBinding("zoom","Ctrl+V"));verify(!map.importBindings('{"version":99,"bindings":{}}'));
  verify(!map.setBinding("unknown","Ctrl+Alt+P"));compare(map.exportBindings(),prior);
 }
 function test_invalidContainers_data(){
  return [[],["Ctrl+Alt+P"],true,false,1,0,null,"", "text"].map((value,i)=>({tag:"container-"+i,value:value}));
 }
 function test_invalidContainers(data){
  verify(map.setBinding("split-right","Ctrl+Alt+P"));
  const prior=map.exportBindings();
  const accepted=map.importBindings(JSON.stringify({version:1,bindings:data.value}));
  compare(map.exportBindings(),prior,"Rejected import must preserve existing bindings");
  verify(!accepted,"Non-object binding container must be rejected");
 }
 function test_emptyObjectIsExplicitReset(){
  verify(map.setBinding("split-right","Ctrl+Alt+P"));
  verify(map.importBindings('{"version":1,"bindings":{}}'));
  compare(Object.keys(JSON.parse(map.exportBindings()).bindings).length,0);
 }
 function test_nativeEditorAndEscape(){editor.visible=true;wait(30);const field=findChild(editor,"keymapJson");verify(field!==null);verify(field.activeFocus);keyClick(Qt.Key_Escape);compare(editor.visible,false);}
}
