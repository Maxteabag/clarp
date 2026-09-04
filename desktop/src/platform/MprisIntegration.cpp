#include "platform/MprisIntegration.h"

#include "media/AudioController.h"

#include <QApplication>
#include <QDBusAbstractAdaptor>
#include <QDBusConnection>
#include <QDBusMessage>
#include <QDBusObjectPath>
#include <QQuickWindow>
#include <QStringList>
#include <QVariantMap>

namespace clarp {
namespace {

constexpr auto MprisService = QLatin1StringView("org.mpris.MediaPlayer2.Clarp");
constexpr auto MprisPath = QLatin1StringView("/org/mpris/MediaPlayer2");

} // namespace

class MprisRootAdaptor final : public QDBusAbstractAdaptor {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.mpris.MediaPlayer2")
    Q_PROPERTY(bool CanQuit READ canQuit CONSTANT)
    Q_PROPERTY(bool CanRaise READ canRaise CONSTANT)
    Q_PROPERTY(bool HasTrackList READ hasTrackList CONSTANT)
    Q_PROPERTY(QString Identity READ identity CONSTANT)
    Q_PROPERTY(QString DesktopEntry READ desktopEntry CONSTANT)
    Q_PROPERTY(QStringList SupportedUriSchemes READ supportedUriSchemes CONSTANT)
    Q_PROPERTY(QStringList SupportedMimeTypes READ supportedMimeTypes CONSTANT)

  public:
    explicit MprisRootAdaptor(MprisIntegration* integration)
        : QDBusAbstractAdaptor(integration), m_integration(integration) {}

    [[nodiscard]] bool canQuit() const { return true; }
    [[nodiscard]] bool canRaise() const { return true; }
    [[nodiscard]] bool hasTrackList() const { return false; }
    [[nodiscard]] QString identity() const { return QStringLiteral("Clarp"); }
    [[nodiscard]] QString desktopEntry() const { return QStringLiteral("com.maxteabag.Clarp"); }
    [[nodiscard]] QStringList supportedUriSchemes() const { return {}; }
    [[nodiscard]] QStringList supportedMimeTypes() const {
        return {QStringLiteral("audio/mpeg"), QStringLiteral("audio/mp4")};
    }

  private:
    MprisIntegration* m_integration = nullptr;

  public slots:
    void Raise() { m_integration->raiseWindow(); }
    void Quit() { QApplication::quit(); }
};

class MprisPlayerAdaptor final : public QDBusAbstractAdaptor {
    Q_OBJECT
    Q_CLASSINFO("D-Bus Interface", "org.mpris.MediaPlayer2.Player")
    Q_PROPERTY(QString PlaybackStatus READ playbackStatus)
    Q_PROPERTY(double Rate READ rate CONSTANT)
    Q_PROPERTY(QVariantMap Metadata READ metadata)
    Q_PROPERTY(double Volume READ volume CONSTANT)
    Q_PROPERTY(qint64 Position READ position CONSTANT)
    Q_PROPERTY(double MinimumRate READ minimumRate CONSTANT)
    Q_PROPERTY(double MaximumRate READ maximumRate CONSTANT)
    Q_PROPERTY(bool CanGoNext READ canGoNext CONSTANT)
    Q_PROPERTY(bool CanGoPrevious READ canGoPrevious CONSTANT)
    Q_PROPERTY(bool CanPlay READ canPlay)
    Q_PROPERTY(bool CanPause READ canPause)
    Q_PROPERTY(bool CanSeek READ canSeek CONSTANT)
    Q_PROPERTY(bool CanControl READ canControl CONSTANT)

  public:
    explicit MprisPlayerAdaptor(MprisIntegration* integration)
        : QDBusAbstractAdaptor(integration), m_integration(integration) {}

    [[nodiscard]] QString playbackStatus() const { return m_integration->playbackStatus(); }
    [[nodiscard]] double rate() const { return 1.0; }
    [[nodiscard]] QVariantMap metadata() const { return m_integration->metadata(); }
    [[nodiscard]] double volume() const { return 1.0; }
    [[nodiscard]] qint64 position() const { return 0; }
    [[nodiscard]] double minimumRate() const { return 1.0; }
    [[nodiscard]] double maximumRate() const { return 1.0; }
    [[nodiscard]] bool canGoNext() const { return false; }
    [[nodiscard]] bool canGoPrevious() const { return false; }
    [[nodiscard]] bool canPlay() const {
        return m_integration->playbackStatus() == QStringLiteral("Paused");
    }
    [[nodiscard]] bool canPause() const {
        return m_integration->playbackStatus() == QStringLiteral("Playing");
    }
    [[nodiscard]] bool canSeek() const { return false; }
    [[nodiscard]] bool canControl() const { return true; }

  private:
    MprisIntegration* m_integration = nullptr;

  public slots:
    void Next() {}
    void Previous() {}
    void Pause() { m_integration->pausePlayback(); }
    void PlayPause() { m_integration->togglePlaybackPause(); }
    void Stop() { m_integration->stopPlayback(); }
    void Play() { m_integration->resumePlayback(); }
    void Seek(qint64 offset) { Q_UNUSED(offset); }
    void SetPosition(const QDBusObjectPath& trackId, qint64 position) {
        Q_UNUSED(trackId);
        Q_UNUSED(position);
    }
    void OpenUri(const QString& uri) { Q_UNUSED(uri); }
};

MprisIntegration::MprisIntegration(QQuickWindow* window, AudioController* audio, QObject* parent)
    : QObject(parent), m_window(window), m_audio(audio),
      m_rootAdaptor(std::make_unique<MprisRootAdaptor>(this)),
      m_playerAdaptor(std::make_unique<MprisPlayerAdaptor>(this)) {
    QDBusConnection bus = QDBusConnection::sessionBus();
    if (!bus.isConnected() || !bus.registerService(MprisService.toString())) {
        return;
    }
    if (!bus.registerObject(MprisPath.toString(), this, QDBusConnection::ExportAdaptors)) {
        bus.unregisterService(MprisService.toString());
        return;
    }
    m_registered = true;
    connect(m_audio, &AudioController::playingChanged, this,
            &MprisIntegration::publishPlaybackStatus);
}

MprisIntegration::~MprisIntegration() {
    if (!m_registered) {
        return;
    }
    QDBusConnection bus = QDBusConnection::sessionBus();
    bus.unregisterObject(MprisPath.toString());
    bus.unregisterService(MprisService.toString());
}

QString MprisIntegration::playbackStatus() const {
    if (m_audio != nullptr && m_audio->playing()) {
        return QStringLiteral("Playing");
    }
    return m_audio != nullptr && m_audio->paused() ? QStringLiteral("Paused")
                                                   : QStringLiteral("Stopped");
}

QVariantMap MprisIntegration::metadata() const {
    if (m_audio == nullptr || !m_audio->playbackAvailable()) {
        return {};
    }
    return {
        {QStringLiteral("mpris:trackid"), QVariant::fromValue(QDBusObjectPath(QStringLiteral(
                                              "/org/mpris/MediaPlayer2/Track/Voice")))},
        {QStringLiteral("xesam:title"), QStringLiteral("Clarp voice reply")},
        {QStringLiteral("xesam:artist"), QStringList{QStringLiteral("Clarp")}},
    };
}

void MprisIntegration::raiseWindow() {
    if (m_window == nullptr) {
        return;
    }
    m_window->show();
    m_window->raise();
    m_window->requestActivate();
}

void MprisIntegration::stopPlayback() {
    if (m_audio != nullptr && m_audio->playbackAvailable()) {
        m_audio->silence();
    }
}

void MprisIntegration::pausePlayback() {
    if (m_audio != nullptr) {
        m_audio->pausePlayback();
    }
}

void MprisIntegration::resumePlayback() {
    if (m_audio != nullptr) {
        m_audio->resumePlayback();
    }
}

void MprisIntegration::togglePlaybackPause() {
    if (m_audio != nullptr) {
        m_audio->togglePlaybackPause();
    }
}

void MprisIntegration::publishPlaybackStatus() const {
    if (!m_registered) {
        return;
    }
    QDBusMessage message = QDBusMessage::createSignal(
        MprisPath.toString(), QStringLiteral("org.freedesktop.DBus.Properties"),
        QStringLiteral("PropertiesChanged"));
    message
        << QStringLiteral("org.mpris.MediaPlayer2.Player")
        << QVariantMap{{QStringLiteral("PlaybackStatus"), playbackStatus()},
                       {QStringLiteral("Metadata"), metadata()},
                       {QStringLiteral("CanPlay"), playbackStatus() == QStringLiteral("Paused")},
                       {QStringLiteral("CanPause"), playbackStatus() == QStringLiteral("Playing")}}
        << QStringList{};
    QDBusConnection::sessionBus().send(message);
}

} // namespace clarp

#include "MprisIntegration.moc"
