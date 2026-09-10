#pragma once

#include <QElapsedTimer>
#include <QHash>
#include <QVariantMap>

namespace clarp {
// Read only shared local Git metadata, never run Git on the UI thread or infer
// repository identity from directory names reported by a remote Host.
class WorkspaceContext final {
  public:
    WorkspaceContext();
    [[nodiscard]] QVariantMap describe(const QString& path, bool sharedFilesystem) const;
  private:
    struct Entry { qint64 checkedAt; QVariantMap details; };
    QElapsedTimer m_clock;
    mutable QHash<QString, Entry> m_cache;
};
}
