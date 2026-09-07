import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: root

    required property var controller

    color: "#e61a1b26"

    MouseArea {
        anchors.fill: parent
        onClicked: (mouse) => {
            return mouse.accepted = true;
        }
    }

    Rectangle {
        width: Math.min(520, parent.width - 48)
        height: cardColumn.implicitHeight + 56
        anchors.centerIn: parent
        radius: 22
        color: "#20212e"
        border.color: "#41445a"

        ColumnLayout {
            id: cardColumn

            anchors.left: parent.left
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            anchors.margins: 28
            spacing: 16

            Text {
                text: "Connect to Clarp"
                color: "#c0caf5"
                font.pixelSize: 24
                font.weight: Font.DemiBold
            }

            Text {
                Layout.fillWidth: true
                text: "The native client connects directly to the Clarp server. Credentials remain outside the UI after this session."
                color: "#8d93b0"
                font.pixelSize: 12
                wrapMode: Text.Wrap
            }

            TextField {
                id: serverField

                Layout.fillWidth: true
                text: root.controller.baseUrl
                placeholderText: "http://127.0.0.1:7682"
                selectByMouse: true
            }

            TextField {
                id: tokenField

                Layout.fillWidth: true
                placeholderText: "Device or administrator token"
                echoMode: TextInput.Password
                passwordCharacter: "•"
                selectByMouse: true
            }

            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 1
                color: "#292b3a"
            }

            Label {
                text: "Or pair with a one-time code from clarp-admin pair create"
                color: "#8d93b0"
                font.pixelSize: 11
            }

            RowLayout {
                Layout.fillWidth: true

                TextField {
                    id: pairingField

                    Layout.fillWidth: true
                    placeholderText: "clp_…"
                    echoMode: TextInput.Password
                    selectByMouse: true
                }

                Button {
                    text: "Pair"
                    enabled: pairingField.text.trim().length > 0 && !root.controller.connecting
                    onClicked: root.controller.pairDevice(serverField.text, pairingField.text)
                }

            }

            Text {
                visible: root.controller.errorMessage.length > 0
                Layout.fillWidth: true
                text: root.controller.errorMessage
                color: "#e58b8c"
                font.pixelSize: 11
                wrapMode: Text.Wrap
            }

            RowLayout {
                Layout.fillWidth: true

                StatusPill {
                    status: root.controller.connectionState
                }

                Item {
                    Layout.fillWidth: true
                }

                Button {
                    text: "Cancel"
                    visible: root.controller.agents.count > 0
                    onClicked: root.visible = false
                }

                Button {
                    text: "Forget device token"
                    visible: root.controller.hasStoredCredential
                    enabled: !root.controller.connecting
                    onClicked: {
                        tokenField.clear();
                        pairingField.clear();
                        root.controller.forgetCredential();
                    }
                }

                Button {
                    text: root.controller.connecting ? "Connecting…" : "Connect"
                    enabled: !root.controller.connecting
                    onClicked: root.controller.connectToServer(serverField.text, tokenField.text)
                }

            }

        }

    }

}
