#include "network/SseParser.h"

#include <QJsonDocument>
#include <QJsonParseError>

namespace clarp {

QList<SseMessage> SseParser::feed(const QByteArray& bytes) {
    m_buffer.append(bytes);
    m_buffer.replace("\r\n", "\n");

    QList<SseMessage> messages;
    qsizetype boundary = 0;
    while ((boundary = m_buffer.indexOf("\n\n")) >= 0) {
        const QByteArray block = m_buffer.first(boundary);
        m_buffer.remove(0, boundary + 2);
        SseMessage message = parseBlock(block);
        if (!message.data.isEmpty()) {
            messages.append(std::move(message));
        }
    }
    return messages;
}

void SseParser::reset() { m_buffer.clear(); }

SseMessage SseParser::parseBlock(const QByteArray& block) {
    SseMessage message;
    QByteArray data;
    const QList<QByteArray> lines = block.split('\n');
    for (const QByteArray& line : lines) {
        if (line.isEmpty() || line.startsWith(':')) {
            continue;
        }
        const qsizetype separator = line.indexOf(':');
        const QByteArray field = separator < 0 ? line : line.first(separator);
        QByteArray value = separator < 0 ? QByteArray{} : line.sliced(separator + 1);
        if (value.startsWith(' ')) {
            value.removeFirst();
        }
        if (field == "id") {
            message.id = QString::fromUtf8(value);
        } else if (field == "event") {
            message.event = QString::fromUtf8(value);
        } else if (field == "data") {
            if (!data.isEmpty()) {
                data.append('\n');
            }
            data.append(value);
        }
    }

    QJsonParseError error;
    const QJsonDocument document = QJsonDocument::fromJson(data, &error);
    if (error.error == QJsonParseError::NoError && document.isObject()) {
        message.data = document.object();
    }
    return message;
}

} // namespace clarp
