#include "platform/DesktopIntegration.h"

#include "app/AppController.h"
#include "platform/MprisIntegration.h"

#include <QAction>
#include <QApplication>
#include <QIcon>
#include <QMenu>
#include <QQuickWindow>
#include <QSystemTrayIcon>

namespace clarp {

DesktopIntegration::DesktopIntegration(QQuickWindow* window, AppController* controller,
                                       QObject* parent)
    : QObject(parent), m_window(window) {
    if (m_window == nullptr || controller == nullptr) {
        return;
    }
    m_mpris = std::make_unique<MprisIntegration>(m_window, controller->audio(), this);
    if (!QSystemTrayIcon::isSystemTrayAvailable()) {
        return;
    }

    m_menu = std::make_unique<QMenu>();
    QAction* showAction = m_menu->addAction(QStringLiteral("Show Clarp"));
    QAction* muteAction = m_menu->addAction(QStringLiteral("Mute voice replies"));
    muteAction->setCheckable(true);
    muteAction->setChecked(controller->muted());
    m_menu->addSeparator();
    QAction* quitAction = m_menu->addAction(QStringLiteral("Quit"));

    m_tray = std::make_unique<QSystemTrayIcon>(
        QIcon(QStringLiteral(":/qt/qml/Clarp/Desktop/resources/clarp.svg")));
    m_tray->setToolTip(QStringLiteral("Clarp"));
    m_tray->setContextMenu(m_menu.get());
    m_tray->show();

    connect(showAction, &QAction::triggered, this, [this] {
        m_window->show();
        m_window->raise();
        m_window->requestActivate();
    });
    connect(muteAction, &QAction::toggled, controller, &AppController::setMuted);
    connect(controller, &AppController::mutedChanged, muteAction,
            [controller, muteAction] { muteAction->setChecked(controller->muted()); });
    connect(quitAction, &QAction::triggered, qApp, &QApplication::quit);
    connect(m_tray.get(), &QSystemTrayIcon::activated, this,
            [this](QSystemTrayIcon::ActivationReason reason) {
                if (reason == QSystemTrayIcon::Trigger || reason == QSystemTrayIcon::DoubleClick) {
                    m_window->show();
                    m_window->raise();
                    m_window->requestActivate();
                }
            });
    connect(controller, &AppController::notificationRequested, this,
            [this](const QString& title, const QString& body) {
                if (m_tray != nullptr) {
                    m_tray->showMessage(title, body, QSystemTrayIcon::Information, 8'000);
                }
            });
}

DesktopIntegration::~DesktopIntegration() = default;

} // namespace clarp
