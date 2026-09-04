#include "models/ContactListModel.h"

#include <QJsonArray>
#include <algorithm>

namespace clarp {

ContactListModel::ContactListModel(QObject* parent) : QAbstractListModel(parent) {}

int ContactListModel::rowCount(const QModelIndex& parent) const {
    return parent.isValid() ? 0 : static_cast<int>(m_contacts.size());
}

QVariant ContactListModel::data(const QModelIndex& index, int role) const {
    if (!index.isValid() || index.row() < 0 || index.row() >= m_contacts.size()) {
        return {};
    }
    const Contact& contact = m_contacts.at(index.row());
    switch (role) {
    case ContactIdRole:
        return contact.id;
    case NameRole:
        return contact.name;
    case DescriptionRole:
        return contact.description;
    case BuiltinRole:
        return contact.builtin;
    case AvatarSymbolRole:
        return contact.avatarSymbol;
    default:
        return {};
    }
}

QHash<int, QByteArray> ContactListModel::roleNames() const {
    return {
        {ContactIdRole, "contactId"},       {NameRole, "name"},
        {DescriptionRole, "description"},   {BuiltinRole, "builtin"},
        {AvatarSymbolRole, "avatarSymbol"},
    };
}

void ContactListModel::applySnapshot(const QJsonObject& snapshot,
                                     const QSet<QString>& activeNames) {
    QVector<Contact> contacts;
    const QJsonArray personas = snapshot.value(QStringLiteral("personas")).toArray();
    contacts.reserve(personas.size());
    for (const auto& value : personas) {
        if (!value.isObject()) {
            continue;
        }
        const QJsonObject object = value.toObject();
        Contact contact;
        contact.id = object.value(QStringLiteral("id")).toString();
        contact.name = object.value(QStringLiteral("name")).toString();
        if (contact.name.isEmpty() || activeNames.contains(contact.name.toCaseFolded())) {
            continue;
        }
        contact.description = object.value(QStringLiteral("personality")).toString();
        const QString prefix = QStringLiteral("Personality: ");
        if (contact.description.startsWith(prefix)) {
            contact.description.remove(0, prefix.size());
        }
        contact.avatarSymbol = object.value(QStringLiteral("avatar_symbol")).toString();
        contact.builtin = object.value(QStringLiteral("builtin")).toBool();
        contacts.append(std::move(contact));
    }
    std::ranges::stable_sort(contacts, [](const Contact& left, const Contact& right) {
        return left.name.localeAwareCompare(right.name) < 0;
    });
    beginResetModel();
    m_contacts = std::move(contacts);
    endResetModel();
    emit countChanged();
}

} // namespace clarp
