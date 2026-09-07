#pragma once

#include <QObject>
#include <memory>

class QQuickWindow;

namespace clarp {

class AudioController;
class MprisPlayerAdaptor;
class MprisRootAdaptor;

class MprisIntegration final : public QObject {
    Q_OBJECT

  public:
    MprisIntegration(QQuickWindow* window, AudioController* audio, QObject* parent = nullptr);
    ~MprisIntegration() override;

    [[nodiscard]] QString playbackStatus() const;
    [[nodiscard]] QVariantMap metadata() const;
    void raiseWindow();
    void pausePlayback();
    void resumePlayback();
    void togglePlaybackPause();
    void stopPlayback();

  private:
    void publishPlaybackStatus() const;

    QQuickWindow* m_window = nullptr;
    AudioController* m_audio = nullptr;
    std::unique_ptr<MprisRootAdaptor> m_rootAdaptor;
    std::unique_ptr<MprisPlayerAdaptor> m_playerAdaptor;
    bool m_registered = false;
};

} // namespace clarp
