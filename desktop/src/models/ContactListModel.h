#pragma once

#include <QAbstractListModel>
#include <QJsonObject>
#include <QSet>
#include <QVector>
#include <QtQmlIntegration/qqmlintegration.h>

namespace clarp {

class ContactListModel : public QAbstractListModel {
    Q_OBJECT
    QML_ANONYMOUS
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)

  public:
    enum Role {
        ContactIdRole = Qt::UserRole + 1,
        NameRole,
        DescriptionRole,
        BuiltinRole,
        AvatarSymbolRole,
    };
    Q_ENUM(Role)

    explicit ContactListModel(QObject* parent = nullptr);

    [[nodiscard]] int rowCount(const QModelIndex& parent = {}) const override;
    [[nodiscard]] QVariant data(const QModelIndex& index, int role) const override;
    [[nodiscard]] QHash<int, QByteArray> roleNames() const override;

    void applySnapshot(const QJsonObject& snapshot, const QSet<QString>& activeNames);

  signals:
    void countChanged();

  private:
    struct Contact {
        QString id;
        QString name;
        QString description;
        QString avatarSymbol;
        bool builtin = false;
    };
    QVector<Contact> m_contacts;
};

} // namespace clarp
