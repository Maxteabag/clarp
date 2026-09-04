#pragma once

#include "protocol/ProtocolTypes.h"

#include <QAudioFormat>
#include <QAudioOutput>
#include <QAudioSink>
#include <QBuffer>
#include <QJsonObject>
#include <QMediaPlayer>
#include <QNetworkAccessManager>
#include <QObject>
#include <QQueue>
#include <QSet>
#include <QUrl>
#include <QtQmlIntegration/qqmlintegration.h>
#include <memory>

class QAudioSource;
class QIODevice;
class QNetworkReply;
class QNetworkRequest;

namespace clarp {

class AudioController : public QObject {
    Q_OBJECT
    QML_ANONYMOUS
    Q_PROPERTY(bool recording READ recording NOTIFY recordingChanged)
    Q_PROPERTY(bool transcribing READ transcribing NOTIFY transcribingChanged)
    Q_PROPERTY(bool playing READ playing NOTIFY playingChanged)
    Q_PROPERTY(bool paused READ paused NOTIFY playingChanged)

  public:
    explicit AudioController(QObject* parent = nullptr);
    ~AudioController() override;

    void setEndpoint(QUrl baseUrl, QString bearerToken);
    void setMuted(bool muted);
    void enqueueClip(const QJsonObject& event);

    [[nodiscard]] bool recording() const;
    [[nodiscard]] bool transcribing() const;
    [[nodiscard]] bool playing() const;
    [[nodiscard]] bool paused() const;
    [[nodiscard]] bool playbackAvailable() const;

    Q_INVOKABLE void toggleRecording();
    Q_INVOKABLE void startRecording();
    Q_INVOKABLE void stopRecording();
    Q_INVOKABLE void cancelRecording();
    Q_INVOKABLE void silence();
    void pausePlayback();
    void resumePlayback();
    void togglePlaybackPause();

  signals:
    void recordingChanged();
    void transcribingChanged();
    void playingChanged();
    void transcriptionReady(const QString& text, const QString& traceId,
                            const QString& transcriptionId, bool handsFree);
    void mediaError(const QString& message);

  private:
    [[nodiscard]] QUrl resolve(const QString& path) const;
    [[nodiscard]] QNetworkRequest request(const QUrl& url) const;
    void setRecording(bool recording);
    void setTranscribing(bool transcribing);
    void ensurePlayer();
    void startNextClip();
    void downloadCurrentSource();
    void downloadHlsPlaylist();
    void downloadNextHlsArtifact();
    void playBufferedMedia(const QByteArray& audio, const QUrl& sourceUrl);
    void playRawPcm(const QByteArray& audio);
    void finishCurrentClip(const QString& status, const QString& error = {});
    void acknowledge(const AudioClip& clip, const QString& status, const QString& error = {});
    void uploadRecording(const QByteArray& wav);

    QNetworkAccessManager m_network;
    std::unique_ptr<QAudioOutput> m_audioOutput;
    std::unique_ptr<QAudioSink> m_pcmOutput;
    std::unique_ptr<QMediaPlayer> m_player;
    QBuffer m_playbackBuffer;
    std::unique_ptr<QAudioSource> m_audioSource;
    QIODevice* m_captureDevice = nullptr;
    QAudioFormat m_captureFormat;
    QByteArray m_capturePcm;
    QQueue<AudioClip> m_clipQueue;
    QQueue<qint64> m_recentClipIds;
    QSet<qint64> m_seenClipIds;
    AudioClip m_currentClip;
    QNetworkReply* m_downloadReply = nullptr;
    QUrl m_currentSource;
    QList<QUrl> m_hlsArtifacts;
    QByteArray m_hlsMedia;
    QUrl m_baseUrl;
    QString m_bearerToken;
    bool m_recording = false;
    bool m_transcribing = false;
    bool m_downloading = false;
    bool m_playStarted = false;
    bool m_hasCurrentClip = false;
    bool m_muted = false;
};

} // namespace clarp
