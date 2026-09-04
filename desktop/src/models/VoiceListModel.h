#pragma once

#include <QAbstractListModel>
#include <QJsonObject>
#include <QVector>
#include <QtQmlIntegration/qqmlintegration.h>

namespace clarp {

class VoiceListModel : public QAbstractListModel {
    Q_OBJECT
    QML_ANONYMOUS
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)

  public:
    enum Role {
        VoiceIdRole = Qt::UserRole + 1,
        LabelRole,
        TakenByRole,
        CurrentRole,
    };
    Q_ENUM(Role)

    explicit VoiceListModel(QObject* parent = nullptr);

    [[nodiscard]] int rowCount(const QModelIndex& parent = {}) const override;
    [[nodiscard]] QVariant data(const QModelIndex& index, int role) const override;
    [[nodiscard]] QHash<int, QByteArray> roleNames() const override;

    void applyResponse(const QJsonObject& response, const QString& currentVoiceId);

  signals:
    void countChanged();

  private:
    struct Voice {
        QString id;
        QString label;
        QString takenBy;
        bool current = false;
    };
    QVector<Voice> m_voices;
};

} // namespace clarp
