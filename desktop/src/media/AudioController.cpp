#include "media/AudioController.h"

#include "media/WavEncoder.h"

#include <QAudioDevice>
#include <QAudioSource>
#include <QIODevice>
#include <QJsonDocument>
#include <QJsonParseError>
#include <QMediaDevices>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QRegularExpression>
#include <QTimer>
#include <QUuid>
#include <algorithm>

namespace clarp {

AudioController::AudioController(QObject* parent) : QObject(parent) {
    m_pendingPlayback.setInterval(250);
    connect(&m_pendingPlayback, &QTimer::timeout, this, &AudioController::startNextClip);
    m_pendingPlayback.start();
    connect(&m_coordinator, &AudioCoordinator::clipReady, this, &AudioController::enqueueOwned);
    connect(&m_coordinator, &AudioCoordinator::commandReceived, this, &AudioController::applyCommand);
    connect(&m_coordinator, &AudioCoordinator::error, this, &AudioController::mediaError);
    connect(&m_coordinator, &AudioCoordinator::stateReceived, this,
        [this](bool muted, bool playing, bool paused, bool available) {
            if (m_muted != muted) {
                m_muted = muted;
                if (muted && m_coordinator.owner()) silenceLocal();
                emit mutedChanged(muted);
            }
            if (!m_coordinator.owner() && (m_remotePlaying != playing || m_remotePaused != paused || m_remoteAvailable != available)) {
                m_remotePlaying = playing;
                m_remotePaused = paused;
                m_remoteAvailable = available;
                emit playingChanged();
            }
        });
    connect(&m_coordinator, &AudioCoordinator::ownershipChanged, this, [this] {
        if (!m_coordinator.owner()) resetPlayback();
        emit playingChanged();
    });
    connect(this, &AudioController::playingChanged, this, [this] {
        m_coordinator.publish(playing(), paused(), playbackAvailable());
    });
}


AudioController::~AudioController() {
    for (auto* reply : m_transcriptionSessions.keys()) {
        disconnect(reply, nullptr, this, nullptr);
        reply->abort();
    }
    cancelRecording();
    resetPlayback();
    m_coordinator.stop();
}

void AudioController::ensurePlayer() {
    if (m_player != nullptr) {
        return;
    }
    m_audioOutput = std::make_unique<QAudioOutput>();
    m_player = std::make_unique<QMediaPlayer>();
    m_player->setAudioOutput(m_audioOutput.get());
    m_player->setPlaybackRate(1.2);
    m_audioOutput->setVolume(1.0F);

    connect(m_player.get(), &QMediaPlayer::playbackStateChanged, this, [this] {
        emit playingChanged();
        if (m_player->playbackState() == QMediaPlayer::PlayingState && !m_playStarted) {
            m_playStarted = true;
            acknowledge(m_currentClip, QStringLiteral("play-start"));
        }
    });
    connect(m_player.get(), &QMediaPlayer::mediaStatusChanged, this,
            [this](QMediaPlayer::MediaStatus status) {
                if (status == QMediaPlayer::EndOfMedia && m_hasCurrentClip) {
                    finishCurrentClip(QStringLiteral("play-ok"));
                }
            });
    connect(m_player.get(), &QMediaPlayer::errorOccurred, this,
            [this](QMediaPlayer::Error error, const QString& message) {
                if (error == QMediaPlayer::NoError) {
                    return;
                }
                finishCurrentClip(QStringLiteral("play-fail"), message);
                emit mediaError(message);
            });
}

void AudioController::setEndpoint(QUrl baseUrl, QString bearerToken) {
    if (baseUrl.isEmpty()) {
        cancelRecording();
        for (auto* reply : m_transcriptionSessions.keys()) {
            m_cancelledTranscriptions.insert(reply);
            reply->abort();
        }
        resetPlayback();
        m_coordinator.stop();
        m_baseUrl = QUrl{};
        m_bearerToken.clear();
        return;
    }
    QString path = baseUrl.path();
    if (!path.endsWith('/')) {
        path.append('/');
    }
    baseUrl.setPath(path);
    baseUrl.setQuery(QString{});
    baseUrl.setFragment(QString{});
    if (m_baseUrl != baseUrl || m_bearerToken != bearerToken) {
        cancelRecording();
        for (auto* reply : m_transcriptionSessions.keys()) {
            m_cancelledTranscriptions.insert(reply);
            reply->abort();
        }
        resetPlayback();
        m_coordinator.stop();
    }
    m_baseUrl = std::move(baseUrl);
    m_bearerToken = std::move(bearerToken);
    if (!m_bearerToken.isEmpty() && !qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PATH")) m_coordinator.configure(m_baseUrl.toString() + QChar::Null + m_bearerToken, m_muted);
}

void AudioController::setMuted(bool muted) {
    if (m_coordinator.configured()) {
        m_coordinator.command(QStringLiteral("mute"), muted);
    } else {
        m_muted = muted;
        emit mutedChanged(muted);
    }
}

void AudioController::enqueueClip(const QJsonObject& event) {
    if (!AudioClip::fromJson(event).preferredSource().isEmpty()) m_coordinator.submit(event);
}

void AudioController::enqueueOwned(const QJsonObject& event) {
    if (!m_coordinator.owner()) return;
    if (m_muted) {
        m_coordinator.finish(event);
        return;
    }
    acknowledge(AudioClip::fromJson(event), QStringLiteral("queued"));
    m_clipQueue.enqueue(event);
    startNextClip();
}

bool AudioController::recording() const { return m_recording; }

bool AudioController::transcribing() const { return m_transcriptionsInFlight > 0; }

int AudioController::transcriptionsInFlight() const { return m_transcriptionsInFlight; }

int AudioController::transcriptionsForSession(const QString& session) const {
    return m_transcriptionsBySession.value(session);
}

bool AudioController::playing() const {
    if (m_coordinator.configured() && !m_coordinator.owner()) return m_remotePlaying;
    return (m_player != nullptr && m_player->playbackState() == QMediaPlayer::PlayingState) ||
           (m_pcmOutput != nullptr && m_pcmOutput->state() == QAudio::ActiveState);
}

bool AudioController::paused() const {
    if (m_coordinator.configured() && !m_coordinator.owner()) return m_remotePaused;
    return (m_player != nullptr && m_player->playbackState() == QMediaPlayer::PausedState) ||
           (m_pcmOutput != nullptr && m_pcmOutput->state() == QAudio::SuspendedState);
}

bool AudioController::playbackAvailable() const {
    return m_coordinator.configured() && !m_coordinator.owner() ? m_remoteAvailable : m_hasCurrentClip;
}

void AudioController::toggleRecording() {
    if (m_recording) {
        stopRecording();
    } else {
        startRecording();
    }
}

void AudioController::toggleRecordingForSession(const QString& session) {
    if (m_recording) {
        stopRecording();
        return;
    }
    m_recordingSession = session;
    startRecording();
}

void AudioController::startRecording() {
    if (m_recording) {
        return;
    }
    if (qEnvironmentVariableIsSet("CLARP_SCREENSHOT_PATH")) return;
    if (!m_recordingLease.acquire(m_recordingSession)) {
        m_recordingSession.clear();
        emit mediaError(QStringLiteral("Another Clarp window is recording. Stop that recording before starting here."));
        return;
    }
    silence();
    const QAudioDevice device = QMediaDevices::defaultAudioInput();
    if (device.isNull()) {
        cancelRecording();
        emit mediaError(QStringLiteral("No microphone is available"));
        return;
    }

    QAudioFormat requested;
    requested.setSampleRate(16'000);
    requested.setChannelCount(1);
    requested.setSampleFormat(QAudioFormat::Int16);
    m_captureFormat = device.isFormatSupported(requested) ? requested : device.preferredFormat();
    m_capturePcm.clear();
    m_audioSource = std::make_unique<QAudioSource>(device, m_captureFormat);
    m_captureDevice = m_audioSource->start();
    if (m_captureDevice == nullptr) {
        m_audioSource.reset();
        cancelRecording();
        emit mediaError(QStringLiteral("The microphone could not be started"));
        return;
    }
    connect(m_captureDevice, &QIODevice::readyRead, this, [this] {
        if (m_captureDevice != nullptr) {
            m_capturePcm.append(m_captureDevice->readAll());
        }
    });
    setRecording(true);
}

void AudioController::stopRecording() {
    if (!m_recording || m_audioSource == nullptr) {
        return;
    }
    if (m_captureDevice != nullptr) {
        m_capturePcm.append(m_captureDevice->readAll());
    }
    m_audioSource->stop();
    m_captureDevice = nullptr;
    m_audioSource.reset();
    setRecording(false);

    const QString targetSession = m_recordingLease.release();
    m_recordingSession.clear();
    const QByteArray wav = encodeWav(m_capturePcm, m_captureFormat);
    m_capturePcm.clear();
    if (wav.size() <= 1'068) {
        emit mediaError(QStringLiteral("Recording was too short"));
        return;
    }
    transcribeRecording(wav, targetSession);
}

void AudioController::cancelRecording() {
    if (m_audioSource != nullptr) {
        m_audioSource->stop();
        m_audioSource.reset();
    }
    m_recordingLease.release();
    m_captureDevice = nullptr;
    m_capturePcm.clear();
    m_recordingSession.clear();
    setRecording(false);
}

void AudioController::cancelTranscriptionsForSession(const QString& session) {
    for (auto it = m_transcriptionSessions.cbegin(); it != m_transcriptionSessions.cend(); ++it) {
        if (it.value() == session && it.key() != nullptr) {
            m_cancelledTranscriptions.insert(it.key());
            it.key()->abort();
        }
    }
}

void AudioController::silence() { m_coordinator.command(QStringLiteral("stop")); }

void AudioController::silenceLocal() {
    while (!m_clipQueue.isEmpty()) m_coordinator.finish(m_clipQueue.dequeue());
    if (m_hasCurrentClip) {
        finishCurrentClip(QStringLiteral("play-fail"), QStringLiteral("interrupted by user"));
    }
}

void AudioController::pausePlayback() { m_coordinator.command(QStringLiteral("pause")); }
void AudioController::resumePlayback() { m_coordinator.command(QStringLiteral("resume")); }
void AudioController::togglePlaybackPause() { m_coordinator.command(QStringLiteral("toggle")); }

void AudioController::applyCommand(const QString& action) {
    if (action == QStringLiteral("stop")) {
        silenceLocal();
    } else if (action == QStringLiteral("pause")) {
        pauseLocal();
    } else if (action == QStringLiteral("resume")) {
        resumeLocal();
    } else if (action == QStringLiteral("toggle")) {
        if (paused()) resumeLocal();
        else pauseLocal();
    }
}

void AudioController::resetPlayback() {
    // Stop effects before relinquishing the bus name. Keep unstarted journal
    // entries for the next owner; never acknowledge interrupted audio as played.
    m_clipQueue.clear();
    m_hasCurrentClip = false;
    m_currentClip = {};
    m_currentEvent = {};
    m_downloading = m_playStarted = false;
    m_remotePlaying = m_remotePaused = m_remoteAvailable = false;
    if (m_downloadReply != nullptr) {
        disconnect(m_downloadReply, nullptr, this, nullptr);
        m_downloadReply->abort();
        m_downloadReply->deleteLater();
        m_downloadReply = nullptr;
    }
    if (m_player != nullptr) {
        m_player->stop();
        m_player->setSource({});
    }
    if (m_pcmOutput != nullptr) { m_pcmOutput->stop(); m_pcmOutput = nullptr; }
    m_playbackBuffer.close();
    m_hlsArtifacts.clear();
    m_hlsMedia.clear();
}

void AudioController::pauseLocal() {
    if (m_player != nullptr && m_player->playbackState() == QMediaPlayer::PlayingState) {
        m_player->pause();
    } else if (m_pcmOutput != nullptr && m_pcmOutput->state() == QAudio::ActiveState) {
        m_pcmOutput->suspend();
    }
}

void AudioController::resumeLocal() {
    if (m_player != nullptr && m_player->playbackState() == QMediaPlayer::PausedState) {
        m_player->play();
    } else if (m_pcmOutput != nullptr && m_pcmOutput->state() == QAudio::SuspendedState) {
        m_pcmOutput->resume();
    }
}

QUrl AudioController::resolve(const QString& path) const {
    QUrl candidate(path);
    if (candidate.isRelative()) {
        QString relative = path;
        while (relative.startsWith('/')) {
            relative.removeFirst();
        }
        return m_baseUrl.resolved(QUrl(relative));
    }
    if (candidate.scheme() != m_baseUrl.scheme() || candidate.host() != m_baseUrl.host() ||
        candidate.port(-1) != m_baseUrl.port(-1)) {
        return {};
    }
    return candidate;
}

QNetworkRequest AudioController::request(const QUrl& url) const {
    QNetworkRequest result(url);
    result.setAttribute(QNetworkRequest::RedirectPolicyAttribute,
                        QNetworkRequest::NoLessSafeRedirectPolicy);
    result.setRawHeader("User-Agent", "ClarpNativeDesktop/0.1");
    if (!m_bearerToken.isEmpty()) {
        result.setRawHeader("Authorization", "Bearer " + m_bearerToken.toUtf8());
    }
    return result;
}

void AudioController::setRecording(bool recording) {
    if (m_recording == recording) {
        return;
    }
    m_recording = recording;
    emit recordingChanged();
}

void AudioController::changeTranscriptionCount(const QString& session, int delta) {
    m_transcriptionsInFlight = std::max(0, m_transcriptionsInFlight + delta);
    const int sessionCount = std::max(0, m_transcriptionsBySession.value(session) + delta);
    if (sessionCount == 0) {
        m_transcriptionsBySession.remove(session);
    } else {
        m_transcriptionsBySession.insert(session, sessionCount);
    }
    emit transcribingChanged();
}

void AudioController::startNextClip() {
    if (m_muted || m_downloading || m_hasCurrentClip || m_clipQueue.isEmpty()) {
        return;
    }
    if (!m_coordinator.owner() || m_recordingLease.busy()) return;
    m_currentEvent = m_clipQueue.dequeue();
    if (!m_coordinator.begin(m_currentEvent)) return;
    m_currentClip = AudioClip::fromJson(m_currentEvent);
    m_hasCurrentClip = true;
    emit playingChanged();
    m_playStarted = false;
    if (!m_currentClip.playlistUrl.isEmpty() && m_currentClip.completeUrl.isEmpty()) {
        downloadHlsPlaylist();
        return;
    }
    const bool raw = m_currentClip.audioFormat.value(QStringLiteral("container")).toString() ==
                     QStringLiteral("raw");
    QString source = m_currentClip.url;
    if (!raw && !m_currentClip.completeUrl.isEmpty()) {
        source = m_currentClip.completeUrl;
    } else if (!m_currentClip.streamUrl.isEmpty()) {
        source = m_currentClip.streamUrl;
    }
    m_currentSource = resolve(source);
    if (!m_currentSource.isValid()) {
        finishCurrentClip(QStringLiteral("play-fail"),
                          QStringLiteral("audio source is outside the configured server"));
        return;
    }
    downloadCurrentSource();
}

void AudioController::downloadCurrentSource() {
    m_downloading = true;
    QNetworkReply* reply = m_network.get(request(m_currentSource));
    m_downloadReply = reply;
    connect(reply, &QNetworkReply::finished, this, [this, reply] {
        if (m_downloadReply != reply) {
            reply->deleteLater();
            return;
        }
        m_downloadReply = nullptr;
        m_downloading = false;
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (reply->error() != QNetworkReply::NoError || status < 200 || status >= 300) {
            const QString message = reply->errorString();
            reply->deleteLater();
            finishCurrentClip(QStringLiteral("play-fail"), message);
            emit mediaError(message);
            return;
        }
        const QByteArray audio = reply->readAll();
        reply->deleteLater();
        if (m_currentClip.audioFormat.value(QStringLiteral("container")).toString() ==
            QStringLiteral("raw")) {
            playRawPcm(audio);
        } else {
            playBufferedMedia(audio, m_currentSource);
        }
    });
}

void AudioController::downloadHlsPlaylist() {
    m_currentSource = resolve(m_currentClip.playlistUrl);
    if (!m_currentSource.isValid()) {
        finishCurrentClip(QStringLiteral("play-fail"),
                          QStringLiteral("playlist is outside the configured server"));
        return;
    }
    m_downloading = true;
    QNetworkReply* reply = m_network.get(request(m_currentSource));
    m_downloadReply = reply;
    connect(reply, &QNetworkReply::finished, this, [this, reply] {
        if (m_downloadReply != reply) {
            reply->deleteLater();
            return;
        }
        m_downloadReply = nullptr;
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        const QByteArray playlist = reply->readAll();
        const QString error = reply->errorString();
        const bool failed =
            reply->error() != QNetworkReply::NoError || status < 200 || status >= 300;
        reply->deleteLater();
        if (failed) {
            m_downloading = false;
            finishCurrentClip(QStringLiteral("play-fail"), error);
            emit mediaError(error);
            return;
        }

        m_hlsArtifacts.clear();
        static const QRegularExpression mapExpression(
            QStringLiteral(R"hls(^#EXT-X-MAP:.*URI=[\"']([^\"']+)[\"'])hls"));
        for (const QString& rawLine : QString::fromUtf8(playlist).split('\n')) {
            const QString line = rawLine.trimmed();
            QString artifact;
            const QRegularExpressionMatch mapMatch = mapExpression.match(line);
            if (mapMatch.hasMatch()) {
                artifact = mapMatch.captured(1);
            } else if (!line.isEmpty() && !line.startsWith('#')) {
                artifact = line;
            }
            if (artifact.isEmpty()) {
                continue;
            }
            const QUrl artifactUrl = m_currentSource.resolved(QUrl(artifact));
            if (resolve(artifactUrl.toString()).isValid()) {
                m_hlsArtifacts.append(artifactUrl);
            }
        }
        if (m_hlsArtifacts.isEmpty()) {
            m_downloading = false;
            finishCurrentClip(QStringLiteral("play-fail"),
                              QStringLiteral("playlist contained no playable media"));
            return;
        }
        m_hlsMedia.clear();
        downloadNextHlsArtifact();
    });
}

void AudioController::downloadNextHlsArtifact() {
    if (m_hlsArtifacts.isEmpty()) {
        m_downloading = false;
        QUrl mediaUrl = m_currentSource.adjusted(QUrl::RemoveFilename);
        mediaUrl.setPath(mediaUrl.path() + QStringLiteral("clarp-voice.mp4"));
        playBufferedMedia(m_hlsMedia, mediaUrl);
        m_hlsMedia.clear();
        return;
    }
    const QUrl artifact = m_hlsArtifacts.takeFirst();
    QNetworkReply* reply = m_network.get(request(artifact));
    m_downloadReply = reply;
    connect(reply, &QNetworkReply::finished, this, [this, reply] {
        if (m_downloadReply != reply) {
            reply->deleteLater();
            return;
        }
        m_downloadReply = nullptr;
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (reply->error() != QNetworkReply::NoError || status < 200 || status >= 300) {
            const QString error = reply->errorString();
            reply->deleteLater();
            m_downloading = false;
            finishCurrentClip(QStringLiteral("play-fail"), error);
            emit mediaError(error);
            return;
        }
        m_hlsMedia.append(reply->readAll());
        reply->deleteLater();
        downloadNextHlsArtifact();
    });
}

void AudioController::playBufferedMedia(const QByteArray& audio, const QUrl& sourceUrl) {
    if (audio.isEmpty()) {
        finishCurrentClip(QStringLiteral("play-fail"), QStringLiteral("audio response was empty"));
        return;
    }
    m_playbackBuffer.close();
    m_playbackBuffer.setData(audio);
    if (!m_playbackBuffer.open(QIODevice::ReadOnly)) {
        finishCurrentClip(QStringLiteral("play-fail"), QStringLiteral("audio buffer failed"));
        return;
    }
    ensurePlayer();
    m_player->setSourceDevice(&m_playbackBuffer, sourceUrl);
    m_player->play();
}

void AudioController::playRawPcm(const QByteArray& audio) {
    QAudioFormat format;
    format.setSampleRate(
        m_currentClip.audioFormat.value(QStringLiteral("sample_rate")).toInt(44'100));
    format.setChannelCount(m_currentClip.audioFormat.value(QStringLiteral("channels")).toInt(1));
    const QString encoding = m_currentClip.audioFormat.value(QStringLiteral("encoding")).toString();
    if (encoding == QStringLiteral("pcm_f32le")) {
        format.setSampleFormat(QAudioFormat::Float);
    } else if (encoding == QStringLiteral("pcm_s16le")) {
        format.setSampleFormat(QAudioFormat::Int16);
    } else if (encoding == QStringLiteral("pcm_u8")) {
        format.setSampleFormat(QAudioFormat::UInt8);
    } else {
        finishCurrentClip(QStringLiteral("play-fail"),
                          QStringLiteral("unsupported raw PCM encoding: %1").arg(encoding));
        return;
    }
    const QAudioDevice device = QMediaDevices::defaultAudioOutput();
    if (device.isNull() || !device.isFormatSupported(format)) {
        finishCurrentClip(QStringLiteral("play-fail"),
                          QStringLiteral("the output device does not support the clip format"));
        return;
    }
    m_playbackBuffer.close();
    m_playbackBuffer.setData(audio);
    if (!m_playbackBuffer.open(QIODevice::ReadOnly)) {
        finishCurrentClip(QStringLiteral("play-fail"), QStringLiteral("audio buffer failed"));
        return;
    }
    m_pcmOutput = std::make_unique<QAudioSink>(device, format);
    connect(m_pcmOutput.get(), &QAudioSink::stateChanged, this, [this](QAudio::State state) {
        emit playingChanged();
        if (state == QAudio::ActiveState && !m_playStarted) {
            m_playStarted = true;
            acknowledge(m_currentClip, QStringLiteral("play-start"));
        } else if (state == QAudio::IdleState && m_hasCurrentClip) {
            QTimer::singleShot(0, this, [this] {
                if (m_hasCurrentClip) {
                    finishCurrentClip(QStringLiteral("play-ok"));
                }
            });
        } else if (state == QAudio::StoppedState && m_hasCurrentClip &&
                   m_pcmOutput->error() != QAudio::NoError) {
            QTimer::singleShot(0, this, [this] {
                if (m_hasCurrentClip) {
                    finishCurrentClip(QStringLiteral("play-fail"),
                                      QStringLiteral("raw PCM playback failed"));
                }
            });
        }
    });
    m_pcmOutput->start(&m_playbackBuffer);
}

void AudioController::finishCurrentClip(const QString& status, const QString& error) {
    if (!m_hasCurrentClip) {
        return;
    }
    const AudioClip finished = m_currentClip;
    m_coordinator.finish(m_currentEvent);
    m_currentEvent = {};
    m_currentClip = {};
    m_hasCurrentClip = false;
    m_downloading = false;
    m_playStarted = false;
    if (m_downloadReply != nullptr) {
        disconnect(m_downloadReply, nullptr, this, nullptr);
        m_downloadReply->abort();
        m_downloadReply->deleteLater();
        m_downloadReply = nullptr;
    }
    m_hlsArtifacts.clear();
    m_hlsMedia.clear();
    if (m_player != nullptr) {
        m_player->stop();
        m_player->setSource({});
    }
    if (m_pcmOutput != nullptr) {
        m_pcmOutput->stop();
        m_pcmOutput = nullptr;
    }
    m_playbackBuffer.close();
    acknowledge(finished, status, error);
    emit playingChanged();
    startNextClip();
}

void AudioController::acknowledge(const AudioClip& clip, const QString& status,
                                  const QString& error) {
    QJsonObject body{
        {QStringLiteral("clip_id"), clip.clipId},
        {QStringLiteral("url"), clip.url},
        {QStringLiteral("status"), status},
        {QStringLiteral("trace_id"), clip.traceId},
    };
    if (!error.isEmpty()) {
        body.insert(QStringLiteral("error"), error);
    }
    QNetworkRequest networkRequest = request(resolve(QStringLiteral("/clips/ack")));
    networkRequest.setHeader(QNetworkRequest::ContentTypeHeader,
                             QStringLiteral("application/json"));
    QNetworkReply* reply =
        m_network.post(networkRequest, QJsonDocument(body).toJson(QJsonDocument::Compact));
    connect(reply, &QNetworkReply::finished, reply, &QObject::deleteLater);
}

void AudioController::transcribeRecording(const QByteArray& wav, const QString& targetSession) {
    changeTranscriptionCount(targetSession, 1);
    const QString transcriptionId = QUuid::createUuid().toString(QUuid::WithoutBraces);
    QNetworkRequest networkRequest = request(resolve(QStringLiteral("/transcribe")));
    networkRequest.setRawHeader("Content-Type", "audio/wav");
    networkRequest.setRawHeader("X-Hands-Free", "0");
    networkRequest.setRawHeader("X-Transcription-ID", transcriptionId.toUtf8());
    QNetworkReply* reply = m_network.post(networkRequest, wav);
    m_transcriptionSessions.insert(reply, targetSession);
    connect(reply, &QNetworkReply::finished, this, [this, reply, transcriptionId] {
        const QString capturedSession = m_transcriptionSessions.take(reply);
        const bool cancelled = m_cancelledTranscriptions.remove(reply);
        changeTranscriptionCount(capturedSession, -1);
        const int status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
        if (cancelled || reply->error() == QNetworkReply::OperationCanceledError) {
            reply->deleteLater();
            return;
        }
        const QByteArray response = reply->readAll();
        if (reply->error() != QNetworkReply::NoError || status < 200 || status >= 300) {
            emit mediaError(reply->errorString());
            reply->deleteLater();
            return;
        }
        QJsonParseError error;
        const QJsonDocument document = QJsonDocument::fromJson(response, &error);
        reply->deleteLater();
        if (error.error != QJsonParseError::NoError || !document.isObject()) {
            emit mediaError(QStringLiteral("Transcription returned invalid JSON"));
            return;
        }
        const QJsonObject result = document.object();
        const QString text = result.value(QStringLiteral("text")).toString().trimmed();
        if (!text.isEmpty()) {
            emit transcriptionReady(
                text, result.value(QStringLiteral("trace_id")).toString(),
                result.value(QStringLiteral("transcription_id")).toString(transcriptionId),
                result.value(QStringLiteral("hands_free")).toBool(), capturedSession);
        }
    });
}

} // namespace clarp
