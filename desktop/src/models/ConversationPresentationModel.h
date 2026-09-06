#pragma once
#include <QSortFilterProxyModel>
#include <QtQmlIntegration/qqmlintegration.h>
namespace clarp {
class ConversationPresentationModel : public QSortFilterProxyModel {
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(bool showWhenReady READ showWhenReady WRITE setShowWhenReady NOTIFY showWhenReadyChanged)
    Q_PROPERTY(int count READ rowCount NOTIFY countChanged)
  public:
    explicit ConversationPresentationModel(QObject* parent = nullptr);
    bool showWhenReady() const { return m_showWhenReady; }
    void setShowWhenReady(bool value);
    QVariant data(const QModelIndex& index, int role) const override;
    Q_INVOKABLE int indexOfMessage(const QString& id) const;
  signals:
    void showWhenReadyChanged();
    void countChanged();
    void rowsAppended(bool fromCurrentUser);
  protected:
    bool filterAcceptsRow(int row, const QModelIndex& parent) const override;
  private:
    bool m_showWhenReady = false;
};
}
