#pragma once

#include <QObject>
#include <QMap>
#include <QString>
#include <QVariantList>
#include <QVariantMap>
#include <QtQmlIntegration/qqmlintegration.h>
#include <memory>
#include <vector>

namespace clarp {

class PaneTreeModel : public QObject {
    Q_OBJECT
    QML_ANONYMOUS
    Q_PROPERTY(QVariantMap rootNode READ rootNode NOTIFY treeChanged)
    Q_PROPERTY(QVariantMap displayRoot READ displayRoot NOTIFY treeChanged)
    Q_PROPERTY(QVariantList paneLayout READ paneLayout NOTIFY treeChanged)
    Q_PROPERTY(QVariantList splitLayout READ splitLayout NOTIFY treeChanged)
    Q_PROPERTY(QString activePaneId READ activePaneId NOTIFY activePaneChanged)
    Q_PROPERTY(QString activeSession READ activeSession NOTIFY activePaneChanged)
    Q_PROPERTY(QString zoomedPaneId READ zoomedPaneId NOTIFY treeChanged)
    Q_PROPERTY(QVariantList viewLayout READ viewLayout NOTIFY treeChanged)
    Q_PROPERTY(QVariantList workspaces READ workspaces NOTIFY treeChanged)
    Q_PROPERTY(QString activeWorkspace READ activeWorkspace NOTIFY treeChanged)
    Q_PROPERTY(QString workspaceSaveWarning READ workspaceSaveWarning NOTIFY workspaceSaveWarningChanged)
    Q_PROPERTY(int paneCount READ paneCount NOTIFY treeChanged)

  public:
    explicit PaneTreeModel(QObject* parent = nullptr);
    ~PaneTreeModel() override;

    [[nodiscard]] QVariantMap rootNode() const;
    [[nodiscard]] QVariantMap displayRoot() const;
    [[nodiscard]] QVariantList paneLayout() const;
    [[nodiscard]] QVariantList splitLayout() const;
    [[nodiscard]] QString activePaneId() const;
    [[nodiscard]] QString activeSession() const;
    [[nodiscard]] QString zoomedPaneId() const;
    [[nodiscard]] int paneCount() const;

    [[nodiscard]] QVariantList viewLayout() const;
    [[nodiscard]] QVariantList workspaces() const;
    [[nodiscard]] QString activeWorkspace() const { return m_activeWorkspace; }
    [[nodiscard]] QString workspaceSaveWarning() const { return m_workspaceSaveWarning; }
    Q_INVOKABLE void saveWorkspaceLayoutInstead();
    Q_INVOKABLE void createWorkspace(const QString& name);
    Q_INVOKABLE void switchWorkspace(const QString& id);
    Q_INVOKABLE void moveActiveToWorkspace(const QString& id);
    Q_INVOKABLE [[nodiscard]] QVariantMap saveState() const;
    Q_INVOKABLE bool loadState(const QVariantMap& state);
    Q_INVOKABLE void setActiveSession(const QString& session);
    Q_INVOKABLE void setPaneSession(const QString& paneId, const QString& session);
    Q_INVOKABLE void splitActive(const QString& direction, const QString& session = {});
    Q_INVOKABLE void closePane(const QString& paneId);
    Q_INVOKABLE void focusPane(const QString& paneId);
    Q_INVOKABLE void navigate(const QString& direction);
    Q_INVOKABLE void toggleZoom();
    Q_INVOKABLE void resizeActive(double delta);
    Q_INVOKABLE void setSplitRatio(const QString& splitId, double ratio);
    Q_INVOKABLE void equalize();

  signals:
    void workspaceSaveWarningChanged();
    void treeChanged();
    void activePaneChanged();

  private:
    struct Node {
        enum class Kind { Leaf, Split };
        Kind kind = Kind::Leaf;
        QString id;
        QString session;
        QString direction;
        double ratio = 0.5;
        std::unique_ptr<Node> first;
        std::unique_ptr<Node> second;
    };

    [[nodiscard]] QString nextId(const QString& prefix);
    [[nodiscard]] static QVariantMap serialize(const Node* node);
    [[nodiscard]] static Node* find(Node* node, const QString& id);
    [[nodiscard]] static const Node* find(const Node* node, const QString& id);
    static void collectLeaves(Node* node, std::vector<Node*>& output);
    static void collectLeaves(const Node* node, std::vector<const Node*>& output);
    static bool split(std::unique_ptr<Node>& node, const QString& targetId,
                      const QString& direction, std::unique_ptr<Node> newLeaf, QString& newActiveId,
                      const QString& splitId);
    static bool close(std::unique_ptr<Node>& node, const QString& targetId);
    static bool setRatio(Node* node, const QString& splitId, double ratio);
    static bool resizeNearest(Node* node, const QString& targetId, double delta);
    static void equalizeNode(Node* node);
    static void appendLayout(const Node* node, double x, double y, double width, double height,
                             QVariantList& panes, QVariantList& splits);
    [[nodiscard]] static std::unique_ptr<Node> deserialize(const QVariantMap& value, int depth,
                                                           int& leafCount, quint64& highestId);
    void restore();
    void persist() const;

    std::unique_ptr<Node> m_root;
    QString m_activePaneId;
    QString m_zoomedPaneId;
    quint64 m_nextId = 0;
    QByteArray m_lastCollection;
    QString m_workspaceSaveWarning;
    QString m_recoveryId;
    bool m_forceWorkspaceSave = false;
    bool m_persistenceEnabled = false;
    QString m_activeWorkspace = QStringLiteral("workspace-1");
    QMap<QString, QVariantMap> m_workspaceStates;
    QMap<QString, QString> m_workspaceNames{{QStringLiteral("workspace-1"), QStringLiteral("Main")}};
    void persistWorkspaces();
};

} // namespace clarp
