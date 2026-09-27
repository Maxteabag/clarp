#pragma once

#include <QObject>
#include <QString>
#include <QStringList>

class QLocalServer;

namespace clarp {

// A second launch of the same Clarp (same binary build, instance, Host,
// config and display) opens a window in the running process instead of
// starting another one: ~57 ms and ~48 MB for the window against ~210 ms
// and ~100 MB for a new process. The launching process only forwards its
// arguments over a Unix socket and exits.

// Empty when this launch must run on its own (no runtime dir, screenshot or
// version-manager runs, update relaunches, CLARP_SEPARATE_PROCESS=1).
[[nodiscard]] QString instanceSocketPath(const QStringList& arguments);
[[nodiscard]] QString instanceSocketPathForExecutable(const QString& executablePath,
                                                      const QStringList& arguments);

// Hands `arguments` to a running instance. Plain POSIX, safe before any
// QCoreApplication exists. True only when the instance accepted them.
[[nodiscard]] bool forwardToRunningInstance(const QString& socketPath, const QStringList& arguments);

class InstanceServer : public QObject {
    Q_OBJECT

  public:
    explicit InstanceServer(QObject* parent = nullptr);
    ~InstanceServer() override;
    InstanceServer(const InstanceServer&) = delete;
    InstanceServer& operator=(const InstanceServer&) = delete;
    InstanceServer(InstanceServer&&) = delete;
    InstanceServer& operator=(InstanceServer&&) = delete;

    bool listen(const QString& socketPath);

  signals:
    void windowRequested(const QStringList& arguments);

  private:
    QLocalServer* m_server = nullptr;
};

} // namespace clarp
