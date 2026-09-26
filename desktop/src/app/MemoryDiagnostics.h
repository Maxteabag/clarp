#pragma once
#include <QDateTime>
#include <QFile>
#include <unistd.h>
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

// Process CPU (user + system) in milliseconds per second since the previous
// call; the first call returns 0.
inline qint64 processCpuMsPerSecond() {
    static qint64 lastTicks = -1;
    static qint64 lastMs = 0;
    QFile stat(QStringLiteral("/proc/self/stat"));
    if (!stat.open(QIODevice::ReadOnly)) return 0;
    const QByteArray line = stat.readAll();
    const qsizetype close = line.lastIndexOf(')');
    const QList<QByteArray> fields = line.mid(close + 2).split(' ');
    if (fields.size() < 13) return 0;
    const qint64 ticks = fields.at(11).toLongLong() + fields.at(12).toLongLong();
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    const long hz = sysconf(_SC_CLK_TCK);
    qint64 result = 0;
    if (lastTicks >= 0 && now > lastMs && hz > 0)
        result = (ticks - lastTicks) * 1000 / hz * 1000 / (now - lastMs);
    lastTicks = ticks;
    lastMs = now;
    return result;
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
