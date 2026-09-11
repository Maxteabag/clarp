#include "media/RecordingSession.h"
#include <QDir>
#include <QStandardPaths>
#include <utility>

namespace clarp {
RecordingSession::RecordingSession(QString lockPath) {
    if (lockPath.isEmpty()) {
        lockPath = QDir(QStandardPaths::writableLocation(QStandardPaths::RuntimeLocation))
            .filePath(QStringLiteral("clarp-desktop-microphone.lock"));
    }
    m_lock = std::make_unique<QLockFile>(std::move(lockPath));
    // A long recording is not a stale lock. QLockFile still recovers dead PIDs.
    m_lock->setStaleLockTime(0);
}
bool RecordingSession::acquire(const QString& session) {
    if (m_active || !m_lock->tryLock()) return false;
    m_target = session;
    m_active = true;
    return true;
}
bool RecordingSession::busy() const {
    if (m_active || !m_lock->tryLock()) return true;
    m_lock->unlock();
    return false;
}
QString RecordingSession::release() {
    QString target = std::exchange(m_target, {});
    if (m_active) m_lock->unlock();
    m_active = false;
    return target;
}
}
