#include "models/VoiceListModel.h"

#include <QJsonArray>

namespace clarp {

VoiceListModel::VoiceListModel(QObject* parent) : QAbstractListModel(parent) {}

int VoiceListModel::rowCount(const QModelIndex& parent) const {
    return parent.isValid() ? 0 : static_cast<int>(m_voices.size());
}

QVariant VoiceListModel::data(const QModelIndex& index, int role) const {
    if (!index.isValid() || index.row() < 0 || index.row() >= m_voices.size()) {
        return {};
    }
    const Voice& voice = m_voices.at(index.row());
    switch (role) {
    case VoiceIdRole:
        return voice.id;
    case LabelRole:
        return voice.label;
    case TakenByRole:
        return voice.takenBy;
    case CurrentRole:
        return voice.current;
    default:
        return {};
    }
}

QHash<int, QByteArray> VoiceListModel::roleNames() const {
    return {
        {VoiceIdRole, "voiceId"},
        {LabelRole, "label"},
        {TakenByRole, "takenBy"},
        {CurrentRole, "current"},
    };
}

void VoiceListModel::applyResponse(const QJsonObject& response, const QString& currentVoiceId) {
    QVector<Voice> next;
    const QJsonArray rows = response.value(QStringLiteral("voices")).toArray();
    next.reserve(rows.size());
    for (const auto& value : rows) {
        if (!value.isObject()) {
            continue;
        }
        const QJsonObject object = value.toObject();
        Voice voice;
        voice.id = object.value(QStringLiteral("id")).toString();
        voice.label = object.value(QStringLiteral("label")).toString(voice.id);
        voice.current = voice.id == currentVoiceId;
        const QString owner = object.value(QStringLiteral("taken_by")).toString();
        if (!voice.current) {
            voice.takenBy = owner;
        }
        if (!voice.id.isEmpty()) {
            next.append(std::move(voice));
        }
    }
    beginResetModel();
    m_voices = std::move(next);
    endResetModel();
    emit countChanged();
}

} // namespace clarp
