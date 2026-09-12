#include "models/PaneTreeModel.h"
#include <QCoreApplication>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QSettings>
#include <QTemporaryDir>
#include <iostream>
using clarp::PaneTreeModel;
int main(int argc, char** argv) {
    QTemporaryDir config;
    if (!config.isValid())
        return 2;
    qputenv("XDG_CONFIG_HOME", config.path().toUtf8());
    QCoreApplication app(argc, argv);
    app.setOrganizationName("MaxTeaBag");
    app.setApplicationName("Clarp");
    int checks = 0;
    auto check = [&](bool v, const char* name) {
        ++checks;
        std::cout << (v ? "PASS " : "FAIL ") << name << '\n';
        if (!v)
            std::exit(1);
    };
    QString original;
    QString review;
    QString moved;
    {
        PaneTreeModel m;
        m.setActiveSession("Aura");
        original = m.activeWorkspace();
        m.splitActive("vertical", "Boreal");
        check(m.paneCount() == 2, "split two conversations");

        m.createWorkspace("Review");
        review = m.activeWorkspace();
        m.setActiveSession("Cedar");
        check(m.workspaces().size() == 2 && m.paneCount() == 1, "independent workspace");
        m.switchWorkspace(original);
        check(m.paneCount() == 2, "original topology restored");
        moved = m.activePaneId();
        m.moveActiveToWorkspace(review);
        check(m.activeWorkspace() == review && m.activePaneId() == moved && m.paneCount() == 2,
              "move retains pane identity");
        const auto tree = m.rootNode();
        m.toggleZoom();
        check(m.paneCount() == 2 && m.paneLayout().size() == 1, "zoom is projection");
        m.switchWorkspace(original);
        m.switchWorkspace(review);
        check(!m.zoomedPaneId().isEmpty() && m.rootNode() == tree, "workspace-local zoom retained");
        m.toggleZoom();
        check(m.rootNode() == tree, "restore leaves topology unchanged");
        const auto before = m.saveState();
        check(!m.loadState({}) && m.saveState() == before, "invalid layout rejected atomically");
        QSettings().sync();
    }
    {
        PaneTreeModel m;
        check(m.workspaces().size() == 2 && m.activeWorkspace() == review,
              "collection survives construction");
        check(m.activePaneId() == moved, "focus survives restart");
        m.switchWorkspace(original);
        check(m.paneCount() == 1, "move removed source placement");
    }
    {
        PaneTreeModel firstWindow;
        PaneTreeModel secondWindow;
        firstWindow.createWorkspace("First window edit");
        const auto newer =
            QSettings().value(QStringLiteral("workspace/collectionV1")).toByteArray();
        secondWindow.createWorkspace("Second window edit");
        check(!secondWindow.workspaceSaveWarning().isEmpty(), "stale window reports save conflict");
        check(QSettings().value(QStringLiteral("workspace/collectionV1")).toByteArray() == newer,
              "stale writer preserves newer collection");
        QSettings saved;
        saved.beginGroup(QStringLiteral("workspace/recovery"));
        check(!saved.childKeys().isEmpty(), "conflicting layout retained in recovery");
        saved.endGroup();
        secondWindow.saveWorkspaceLayoutInstead();
        check(secondWindow.workspaceSaveWarning().isEmpty(), "explicit save resolves conflict");
        check(QSettings().value(QStringLiteral("workspace/collectionV1")).toByteArray() != newer,
              "explicit save replaces collection");
    }
    std::cout << checks << " behavior assertions passed\n";
}
