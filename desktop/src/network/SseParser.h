#pragma once

#include <QByteArray>
#include <QJsonObject>
#include <QList>
#include <QString>

namespace clarp {

struct SseMessage {
    QString id;
    QString event;
    QJsonObject data;
};

class SseParser final {
  public:
    [[nodiscard]] QList<SseMessage> feed(const QByteArray& bytes);
    void reset();

  private:
    [[nodiscard]] static SseMessage parseBlock(const QByteArray& block);

    QByteArray m_buffer;
};

} // namespace clarp
