#pragma once

#include <QJsonObject>
#include <QString>

namespace clarp {

class TranscriptCache final {
  public:
    explicit TranscriptCache(QString rootDirectory = {});

    [[nodiscard]] QJsonObject load(const QString& baseUrl, const QString& session) const;
    [[nodiscard]] bool save(const QString& baseUrl, const QString& session,
                            const QJsonObject& snapshot) const;
    void remove(const QString& baseUrl, const QString& session) const;

  private:
    [[nodiscard]] QString pathFor(const QString& baseUrl, const QString& session) const;

    QString m_rootDirectory;
};

} // namespace clarp
