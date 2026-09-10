#pragma once

#include <QLockFile>
#include <QString>
#include <memory>

namespace clarp {
// The microphone is exclusive across Hosts and windows. Its destination is
// immutable until release, even when the UI focus or selected chat changes.
class RecordingSession final {
  public:
    explicit RecordingSession(QString lockPath = {});
    bool acquire(const QString& session);
    QString release();
    [[nodiscard]] bool busy() const;
    [[nodiscard]] QString target() const { return m_target; }
    [[nodiscard]] bool active() const { return m_active; }
  private:
    std::unique_ptr<QLockFile> m_lock;
    QString m_target;
    bool m_active = false;
};
}
