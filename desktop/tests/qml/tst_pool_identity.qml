import QtQuick
import QtQuick.Controls
import QtTest
import "../../qml/components"
TestCase {
 id:tc;name:"PooledMessageIdentity";when:windowShown;visible:true;width:650;height:500
 QtObject {id:stub;property int mediaRevision:0;property int agentRevision:0;property var toolNarrator:null;property bool sharedFilesystem:false
 function resolveMediaMarkdown(t){return t;}function markdownDisplayBlocks(t){return [t];}}
 Component {id:message;MessageDelegate {
  controller:stub;session:"fixture";messageId:"first";authorRole:"assistant";body:"The workspace retains its reading position."
  timestamp:"";messageKind:"message";toolName:"";origin:"";senderName:"";pending:false;deliveryFailed:false;activity:false;activityStatus:""
  automated:false;category:"";tools:[];displayCells:[];activityCount:0;toolDetailsAvailable:false;showTools:false;showTimestamp:false;width:600
 }}
 function test_newIdentityClearsExpansion(){const row=createTemporaryObject(message,tc);verify(row!==null);row.groupedExpanded=true;row.activityExpanded=true;row.messageId="second";compare(row.groupedExpanded,false);compare(row.activityExpanded,false);}
}
