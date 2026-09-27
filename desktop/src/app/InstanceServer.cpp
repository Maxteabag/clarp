#include "app/InstanceServer.h"

#include <QByteArray>
#include <QCryptographicHash>
#include <QDir>
#include <QFile>
#include <QtEndian>
#include <QLocalServer>
#include <QLocalSocket>
#include <QtGlobal>

#include <array>
#include <memory>
#include <cerrno>
#include <cstring>
#include <poll.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

namespace clarp {
namespace {

constexpr int ReplyTimeoutMs = 2'000;
constexpr quint32 MaxMessageBytes = 64 * 1024;

QByteArray executableIdentity() {
    // A newly installed binary must not open its windows in an older process,
    // so the build (path and modification time) is part of the key.
    std::array<char, 4096> path{};
    const ssize_t length = readlink("/proc/self/exe", path.data(), path.size() - 1);
    if (length <= 0) return {};
    struct stat info {};
    if (stat(path.data(), &info) != 0) return {};
    return QByteArray(path.data(), static_cast<qsizetype>(length)) + '\0'
        + QByteArray::number(static_cast<qlonglong>(info.st_mtim.tv_sec)) + '.'
        + QByteArray::number(static_cast<qlonglong>(info.st_mtim.tv_nsec));
}

bool runsAlone(const QStringList& arguments) {
    static const QStringList separateFlags{QStringLiteral("-h"), QStringLiteral("--help"),
        QStringLiteral("--help-all"), QStringLiteral("-v"), QStringLiteral("--version"),
        QStringLiteral("--preview-versions")};
    for (const QString& argument : arguments)
        if (separateFlags.contains(argument)) return true;
    return qEnvironmentVariableIsSet("CLARP_SEPARATE_PROCESS")
        || qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PATH")
        || qEnvironmentVariable("CLARP_RESTORE_DESKTOP") == QStringLiteral("1");
}

} // namespace

QString instanceSocketPath(const QStringList& arguments) {
    const QString runtime = qEnvironmentVariable("XDG_RUNTIME_DIR");
    if (runtime.isEmpty() || runsAlone(arguments)) return {};
    const QByteArray executable = executableIdentity();
    if (executable.isEmpty()) return {};
    QByteArray identity = executable;
    for (const char* name : {"CLARP_INSTANCE_NAME", "CLARP_BASE_URL", "CLARP_TOKEN",
                             "CLARP_SHARED_FILESYSTEM_HOST", "XDG_CONFIG_HOME", "WAYLAND_DISPLAY",
                             "DISPLAY", "QT_QPA_PLATFORM", "CLARP_RENDERER", "QT_QUICK_BACKEND"}) {
        identity += '\0';
        identity += name;
        identity += '=';
        identity += qgetenv(name);
    }
    const QString digest = QString::fromLatin1(
        QCryptographicHash::hash(identity, QCryptographicHash::Sha256).toHex().left(20));
    return QDir(runtime).filePath(QStringLiteral("clarp-desktop-%1.sock").arg(digest));
}

bool forwardToRunningInstance(const QString& socketPath, const QStringList& arguments) {
    if (socketPath.isEmpty()) return false;
    const QByteArray path = QFile::encodeName(socketPath);
    sockaddr_un address{};
    if (static_cast<size_t>(path.size()) >= sizeof(address.sun_path)) return false;
    address.sun_family = AF_UNIX;
    std::memcpy(static_cast<void*>(&address.sun_path[0]), path.constData(), static_cast<size_t>(path.size()));
    const int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0) return false;
    const auto closeAndReturn = [fd](bool accepted) {
        close(fd);
        return accepted;
    };
    // NOLINTNEXTLINE(cppcoreguidelines-pro-type-reinterpret-cast): POSIX socket API
    if (connect(fd, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0)
        return closeAndReturn(false);
    // A 4-byte big-endian length, then NUL-terminated UTF-8 arguments. No
    // half-close: Qt's local socket treats it as a disconnect and the reply
    // could not be written back.
    QByteArray body;
    for (const QString& argument : arguments) {
        body += argument.toUtf8();
        body += '\0';
    }
    QByteArray payload(4, '\0');
    qToBigEndian(static_cast<quint32>(body.size()), payload.data());
    payload += body;
    qsizetype written = 0;
    while (written < payload.size()) {
        const QByteArrayView remaining = QByteArrayView(payload).sliced(written);
        const ssize_t sent = send(fd, remaining.data(),
                                  static_cast<size_t>(remaining.size()), MSG_NOSIGNAL);
        if (sent < 0 && errno == EINTR) continue;
        if (sent <= 0) return closeAndReturn(false);
        written += sent;
    }
    pollfd wait{.fd = fd, .events = POLLIN, .revents = 0};
    if (poll(&wait, 1, ReplyTimeoutMs) <= 0) return closeAndReturn(false);
    std::array<char, 8> reply{};
    const ssize_t received = recv(fd, reply.data(), reply.size(), 0);
    return closeAndReturn(received >= 2 && reply[0] == 'o' && reply[1] == 'k');
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
