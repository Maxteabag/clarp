import QtQuick
import QtQuick.Dialogs

// Created on first use by Composer.qml: importing QtQuick.Dialogs in the
// composer loaded the dialogs stack (~3.4 MB of libraries) at every start.
FileDialog {
    signal filesChosen(var files)

    title: "Attach a file"
    fileMode: FileDialog.OpenFiles
    onAccepted: filesChosen(selectedFiles)
}
