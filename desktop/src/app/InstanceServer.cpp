#include "app/InstanceServer.h"
#include "launcher/InstanceForward.h"

#include <QByteArray>
#include <QtEndian>
#include <QLocalServer>
#include <QLocalSocket>

#include <array>
#include <memory>
#include <sys/stat.h>
#include <unistd.h>
#include <vector>

namespace clarp {
namespace {

constexpr quint32 MaxMessageBytes = 64 * 1024;

QString executablePath() {
    std::array<char, 4096> path{};
    const ssize_t length = readlink("/proc/self/exe", path.data(), path.size() - 1);
    if (length <= 0) return {};
    return QString::fromLocal8Bit(path.data(), static_cast<qsizetype>(length));
}

std::vector<std::string> toLauncherArguments(const QStringList& arguments) {
    std::vector<std::string> result;
    result.reserve(static_cast<size_t>(arguments.size()));
    for (const QString& argument : arguments)
        result.push_back(argument.toUtf8().toStdString());
    return result;
}

} // namespace

QString instanceSocketPath(const QStringList& arguments) {
    return instanceSocketPathForExecutable(executablePath(), arguments);
}

QString instanceSocketPathForExecutable(const QString& path, const QStringList& arguments) {
    return QString::fromStdString(clarp::launcher::instanceSocketPathForExecutable(
        path.toLocal8Bit().toStdString(), toLauncherArguments(arguments)));
}

bool forwardToRunningInstance(const QString& socketPath, const QStringList& arguments) {
    return clarp::launcher::forwardToRunningInstance(
        socketPath.toLocal8Bit().toStdString(), toLauncherArguments(arguments))
        == clarp::launcher::ForwardResult::Accepted;
}

InstanceServer::InstanceServer(QObject* parent) : QObject(parent), m_server(new QLocalServer(this)) {
    m_server->setSocketOptions(QLocalServer::UserAccessOption);
    connect(m_server, &QLocalServer::newConnection, this, [this] {
        while (QLocalSocket* client = m_server->nextPendingConnection()) {
            auto buffer = std::make_shared<QByteArray>();
            connect(client, &QLocalSocket::readyRead, this, [this, client, buffer] {
                buffer->append(client->readAll());
                if (buffer->size() < 4) return;
                const auto length = qFromBigEndian<quint32>(buffer->constData());
                if (length > MaxMessageBytes) {
                    client->disconnectFromServer();
                    return;
                }
                if (buffer->size() < 4 + static_cast<qsizetype>(length)) return;
                QStringList arguments;
                for (const QByteArray& part : buffer->mid(4, length).split('\0'))
                    if (!part.isEmpty()) arguments.append(QString::fromUtf8(part));
                buffer->clear();
                client->write("ok");
                client->flush();
                client->disconnectFromServer();
                emit windowRequested(arguments);
            });
            connect(client, &QLocalSocket::disconnected, client, &QObject::deleteLater);
        }
    });
}

InstanceServer::~InstanceServer() = default;

bool InstanceServer::listen(const QString& socketPath) {
    if (socketPath.isEmpty()) return false;
    // Reaching here means no live instance answered on this path, so any
    // socket file left there belongs to a process that is gone.
    QLocalServer::removeServer(socketPath);
    return m_server->listen(socketPath);
}

} // namespace clarp
