#include "app/CredentialStore.h"

#include <QDBusConnection>
#include <QDBusInterface>
#include <QDBusMetaType>
#include <QDBusPendingCallWatcher>
#include <QDBusPendingReply>
#include <QDBusVariant>
#include <QMap>
#include <QTimer>

namespace clarp {
namespace {

constexpr auto ServiceName = "org.freedesktop.secrets";
constexpr auto ServicePath = "/org/freedesktop/secrets";
constexpr auto ServiceInterface = "org.freedesktop.Secret.Service";
constexpr auto CollectionPath = "/org/freedesktop/secrets/aliases/default";
constexpr auto CollectionInterface = "org.freedesktop.Secret.Collection";
constexpr auto ItemInterface = "org.freedesktop.Secret.Item";

QMap<QString, QString> attributes(const QString& serverUrl) {
    return {
        {QStringLiteral("application"), QStringLiteral("com.maxteabag.Clarp")},
        {QStringLiteral("server"), serverUrl},
    };
}

} // namespace

QDBusArgument& operator<<(QDBusArgument& argument, const SecretValue& secret) {
    argument.beginStructure();
    argument << secret.session << secret.parameters << secret.value << secret.contentType;
    argument.endStructure();
    // Qt's DBus extraction operator must return its input reference.
    return argument; // NOLINT(bugprone-return-const-ref-from-parameter)
}

const QDBusArgument& operator>>(const QDBusArgument& argument, SecretValue& secret) {
    argument.beginStructure();
    argument >> secret.session >> secret.parameters >> secret.value >> secret.contentType;
    argument.endStructure();
    // Qt's DBus extraction operator must return its input reference.
    return argument; // NOLINT(bugprone-return-const-ref-from-parameter)
}

CredentialStore::CredentialStore(QObject* parent) : QObject(parent) {
    qDBusRegisterMetaType<SecretValue>();
    qDBusRegisterMetaType<QMap<QString, QString>>();
}

void CredentialStore::lookup(const QString& serverUrl) {
    // The watchers are QObject-parented and call deleteLater in their terminal callbacks.
    // NOLINTBEGIN(clang-analyzer-cplusplus.NewDeleteLeaks)
    openSession([this, serverUrl](const QDBusObjectPath& session) {
        if (session.path().isEmpty()) {
            emit lookupFinished(serverUrl, {});
            return;
        }
        QDBusInterface service(ServiceName, ServicePath, ServiceInterface,
                               QDBusConnection::sessionBus());
        auto* search = new QDBusPendingCallWatcher(
            service.asyncCall(QStringLiteral("SearchItems"),
                              QVariant::fromValue(attributes(serverUrl))),
            this);
        connect(
            search, &QDBusPendingCallWatcher::finished, this, [this, search, session, serverUrl] {
                QDBusPendingReply<QList<QDBusObjectPath>, QList<QDBusObjectPath>> reply = *search;
                search->deleteLater();
                if (reply.isError() || reply.argumentAt<0>().isEmpty()) {
                    emit lookupFinished(serverUrl, {});
                    return;
                }

                const QDBusObjectPath itemPath = reply.argumentAt<0>().first();
                QDBusInterface item(ServiceName, itemPath.path(), ItemInterface,
                                    QDBusConnection::sessionBus());
                auto* getSecret = new QDBusPendingCallWatcher(
                    item.asyncCall(QStringLiteral("GetSecret"), QVariant::fromValue(session)),
                    this);
                connect(getSecret, &QDBusPendingCallWatcher::finished, this,
                        [this, getSecret, serverUrl] {
                            QDBusPendingReply<SecretValue> secretReply = *getSecret;
                            getSecret->deleteLater();
                            const QString token =
                                secretReply.isError()
                                    ? QString{}
                                    : QString::fromUtf8(secretReply.value().value).trimmed();
                            emit lookupFinished(serverUrl, token);
                        });
            });
    });
    // NOLINTEND(clang-analyzer-cplusplus.NewDeleteLeaks)
}

void CredentialStore::store(const QString& serverUrl, const QString& token) {
    openSession([this, serverUrl, token](const QDBusObjectPath& session) {
        if (session.path().isEmpty()) {
            emit storeFailed(QStringLiteral("Secret Service session could not be opened"));
            return;
        }

        QVariantMap properties;
        properties.insert(QStringLiteral("org.freedesktop.Secret.Item.Label"),
                          QStringLiteral("Clarp native desktop token"));
        properties.insert(QStringLiteral("org.freedesktop.Secret.Item.Attributes"),
                          QVariant::fromValue(attributes(serverUrl)));
        const SecretValue secret{
            .session = session,
            .parameters = {},
            .value = token.toUtf8(),
            .contentType = QStringLiteral("text/plain; charset=utf-8"),
        };

        QDBusInterface collection(ServiceName, CollectionPath, CollectionInterface,
                                  QDBusConnection::sessionBus());
        auto* create = new QDBusPendingCallWatcher(
            collection.asyncCall(QStringLiteral("CreateItem"), properties,
                                 QVariant::fromValue(secret), true),
            this);
        connect(create, &QDBusPendingCallWatcher::finished, this, [this, create, serverUrl] {
            QDBusPendingReply<QDBusObjectPath, QDBusObjectPath> reply = *create;
            create->deleteLater();
            if (reply.isError()) {
                emit storeFailed(reply.error().message());
                return;
            }
            if (reply.argumentAt<0>().path().isEmpty() && !reply.argumentAt<1>().path().isEmpty() &&
                reply.argumentAt<1>().path() != QStringLiteral("/")) {
                emit storeFailed(QStringLiteral("Secret Service requires an unlock prompt"));
                return;
            }
            emit storeFinished(serverUrl);
        });
    });
}

void CredentialStore::remove(const QString& serverUrl) {
    // The watchers are QObject-parented and call deleteLater in their terminal callbacks.
    // NOLINTBEGIN(clang-analyzer-cplusplus.NewDeleteLeaks)
    QDBusInterface service(ServiceName, ServicePath, ServiceInterface,
                           QDBusConnection::sessionBus());
    auto* search =
        new QDBusPendingCallWatcher(service.asyncCall(QStringLiteral("SearchItems"),
                                                      QVariant::fromValue(attributes(serverUrl))),
                                    this);
    connect(search, &QDBusPendingCallWatcher::finished, this, [this, search, serverUrl] {
        QDBusPendingReply<QList<QDBusObjectPath>, QList<QDBusObjectPath>> reply = *search;
        search->deleteLater();
        if (reply.isError()) {
            emit storeFailed(reply.error().message());
            return;
        }
        QList<QDBusObjectPath> items = reply.argumentAt<0>();
        items.append(reply.argumentAt<1>());
        if (items.isEmpty()) {
            emit removeFinished(serverUrl);
            return;
        }
        QDBusInterface item(ServiceName, items.first().path(), ItemInterface,
                            QDBusConnection::sessionBus());
        auto* remove = new QDBusPendingCallWatcher(item.asyncCall(QStringLiteral("Delete")), this);
        connect(remove, &QDBusPendingCallWatcher::finished, this, [this, remove, serverUrl] {
            QDBusPendingReply<QDBusObjectPath> removeReply = *remove;
            remove->deleteLater();
            if (removeReply.isError()) {
                emit storeFailed(removeReply.error().message());
                return;
            }
            const QString prompt = removeReply.value().path();
            if (!prompt.isEmpty() && prompt != QStringLiteral("/")) {
                emit storeFailed(QStringLiteral("Secret Service requires a delete prompt"));
                return;
            }
            emit removeFinished(serverUrl);
        });
    });
    // NOLINTEND(clang-analyzer-cplusplus.NewDeleteLeaks)
}

void CredentialStore::openSession(SessionCallback callback) {
    if (!QDBusConnection::sessionBus().isConnected()) {
        QTimer::singleShot(0, this, [callback = std::move(callback)] { callback({}); });
        return;
    }
    QDBusInterface service(ServiceName, ServicePath, ServiceInterface,
                           QDBusConnection::sessionBus());
    auto* watcher = new QDBusPendingCallWatcher(
        service.asyncCall(QStringLiteral("OpenSession"), QStringLiteral("plain"),
                          QVariant::fromValue(QDBusVariant(QString{}))),
        this);
    connect(watcher, &QDBusPendingCallWatcher::finished, this,
            [watcher, callback = std::move(callback)]() mutable {
                QDBusPendingReply<QDBusVariant, QDBusObjectPath> reply = *watcher;
                watcher->deleteLater();
                callback(reply.isError() ? QDBusObjectPath{} : reply.argumentAt<1>());
            });
}

} // namespace clarp
