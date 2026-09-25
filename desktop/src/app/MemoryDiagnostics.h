#pragma once
#include <QFile>
#include <cstring>
#include <QQuickItem>
#include <QQuickWindow>
#include <QString>
#include <QVariantMap>

namespace clarp {
// Process memory as the kernel accounts it, read from /proc/self/status.
// Returns kilobytes; zero when a field is missing (non-Linux, or a sandbox).
inline QVariantMap processMemoryKb() {
    QVariantMap values;
    QFile status(QStringLiteral("/proc/self/status"));
    if (!status.open(QIODevice::ReadOnly | QIODevice::Text)) return values;
    for (const QByteArray& line : status.readAll().split('\n')) {
        for (const char* key : {"VmRSS", "RssAnon", "RssFile", "VmSwap", "VmHWM"}) {
            const auto keyLength = static_cast<qsizetype>(strlen(key));
            if (line.startsWith(key) && line.size() > keyLength && line[keyLength] == ':') {
                const QList<QByteArray> parts = line.mid(keyLength + 1).trimmed().split(' ');
                values.insert(QString::fromLatin1(key), parts.isEmpty() ? 0 : parts.first().toLongLong());
            }
        }
    }
    return values;
}

// Live Qt Quick items under a window, and how many of them are text editors.
// A steadily rising count across identical views is the signature of leaked
// delegates; a flat count with rising RSS points at non-item allocations.
inline void countItems(const QQuickItem* item, qint64& items, qint64& textItems) {
    if (item == nullptr) return;
    ++items;
    const char* className = item->metaObject()->className();
    if (qstrcmp(className, "QQuickTextEdit") == 0 || qstrcmp(className, "QQuickText") == 0
        || qstrcmp(className, "QQuickTextInput") == 0 || qstrncmp(className, "QQuickTextEdit_", 15) == 0)
        ++textItems;
    for (const QQuickItem* child : item->childItems()) countItems(child, items, textItems);
}

inline QVariantMap windowItemCounts(const QQuickWindow* window) {
    qint64 items = 0;
    qint64 textItems = 0;
    if (window != nullptr) countItems(window->contentItem(), items, textItems);
    return {{QStringLiteral("items"), items}, {QStringLiteral("textItems"), textItems}};
}
}  // namespace clarp
