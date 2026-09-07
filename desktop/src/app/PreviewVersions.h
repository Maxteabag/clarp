#pragma once
#include <QObject>
#include <QProcess>
#include <QTimer>
#include <QVariantMap>
#include <QtQmlIntegration/qqmlintegration.h>

namespace clarp {
class PreviewVersions : public QObject {
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(bool enabled READ enabled CONSTANT)
    Q_PROPERTY(bool busy READ busy NOTIFY changed)
    Q_PROPERTY(QString runningHash READ runningHash CONSTANT)
    Q_PROPERTY(QVariantMap catalog READ catalog NOTIFY changed)
    Q_PROPERTY(QString error READ error NOTIFY changed)
    Q_PROPERTY(QString notice READ notice NOTIFY changed)
public:
    explicit PreviewVersions(QObject* parent = nullptr);
    [[nodiscard]] bool enabled() const { return m_enabled; }
    [[nodiscard]] bool busy() const { return m_process.state() != QProcess::NotRunning; }
    [[nodiscard]] QString runningHash() const { return m_runningHash; }
    [[nodiscard]] QVariantMap catalog() const { return m_catalog; }
    [[nodiscard]] QString error() const { return m_error; }
    [[nodiscard]] QString notice() const { return m_notice; }
    Q_INVOKABLE void refresh();
    Q_INVOKABLE void selectVersion(const QString& hash);
signals:
    void changed();
private:
    bool m_enabled = false;
    bool m_fixture = false;
    bool m_selecting = false;
    QString m_helper;
    QString m_runningHash;
    QString m_error;
    QString m_notice;
    QVariantMap m_catalog;
    QProcess m_process;
    QTimer m_timer;
};
}
