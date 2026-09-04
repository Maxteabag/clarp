#pragma once

#include <QByteArray>
#include <QDBusArgument>
#include <QDBusObjectPath>
#include <QObject>
#include <QString>
#include <functional>

namespace clarp {

struct SecretValue {
    QDBusObjectPath session;
    QByteArray parameters;
    QByteArray value;
    QString contentType;
};

QDBusArgument& operator<<(QDBusArgument& argument, const SecretValue& secret);
const QDBusArgument& operator>>(const QDBusArgument& argument, SecretValue& secret);

class CredentialStore final : public QObject {
    Q_OBJECT

  public:
    explicit CredentialStore(QObject* parent = nullptr);

    void lookup(const QString& serverUrl);
    void store(const QString& serverUrl, const QString& token);
    void remove(const QString& serverUrl);

  signals:
    void lookupFinished(const QString& serverUrl, const QString& token);
    void storeFinished(const QString& serverUrl);
    void removeFinished(const QString& serverUrl);
    void storeFailed(const QString& message);

  private:
    using SessionCallback = std::function<void(const QDBusObjectPath&)>;
    void openSession(SessionCallback callback);
};

} // namespace clarp

Q_DECLARE_METATYPE(clarp::SecretValue)
