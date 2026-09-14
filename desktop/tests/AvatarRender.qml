import QtQuick
import "../qml/components" as Product
Window {
 width: 400; height: 130; color: "#1a1b26"
 QtObject {id: fixture;property var avatarMotion:proofClock;property int avatarRevision:0;function avatarSource(session){return ""}}
 Product.AvatarActivity {id: avatar; x:25;y:40;controller:fixture;session:"A";name:"Aster";working:authoritativeWorking;avatarSize:40}
 Text {x:90;y:48;text:"Aster · Working";color:"#c7c9dc";font.pixelSize:14
  Product.ActivitySweep {anchors.fill:parent;working:avatar.authoritativeWorking;reducedMotion:avatar.reducedMotion;phase:avatar.phase}
 }
}
