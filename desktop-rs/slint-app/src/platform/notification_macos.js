// Executed by osascript. Notification text is passed separately in argv.
ObjC.import("Foundation");
function run(argv) {
    const center = $.NSUserNotificationCenter.defaultUserNotificationCenter;
    const delivered = center.deliveredNotifications;
    for (let i = 0; i < delivered.count; i++) {
        const item = delivered.objectAtIndex(i);
        if (ObjC.unwrap(item.identifier) === argv[0]) {
            center.removeDeliveredNotification(item);
        }
    }
    if (argv.length > 1) {
        const note = $.NSUserNotification.alloc.init;
        note.identifier = argv[0];
        note.title = argv[1];
        note.informativeText = argv[2];
        if (argv[3]) note.soundName = argv[3];
        center.deliverNotification(note);
    }
}
