#pragma once

#include <QObject>
#include <memory>

class QAction;
class QMenu;
class QQuickWindow;
class QSystemTrayIcon;

namespace clarp {

class AppController;
class MprisIntegration;

class DesktopIntegration final : public QObject {
    Q_OBJECT

  public:
    DesktopIntegration(QQuickWindow* window, AppController* controller, QObject* parent = nullptr);
    ~DesktopIntegration() override;

  private:
    QQuickWindow* m_window = nullptr;
    std::unique_ptr<QMenu> m_menu;
    std::unique_ptr<QSystemTrayIcon> m_tray;
    std::unique_ptr<MprisIntegration> m_mpris;
};

} // namespace clarp
