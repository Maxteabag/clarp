#pragma once
#include <QAbstractItemModel>
#include <QPointer>
#include <QQmlComponent>
#include <QQuickItem>
#include <QVariantMap>
#include <QtQmlIntegration/qqmlintegration.h>
#include <vector>

class QQmlPropertyMap;

namespace clarp {
// The rows of a transcript, laid out from heights the view knows instead of
// from an average (see docs/transcript-view.md). Every row has its own
// height: measured once its delegate exists at this width, otherwise
// estimated from its own text. A row's top is the sum of the heights above
// it, so a height that changes above the viewport never moves what is on
// screen: the viewport is kept on an anchor (the row at its top and the
// offset into it), or on the end while following, and the flickable's
// contentY is corrected before the frame is drawn.
class TranscriptLayout : public QQuickItem {
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(QAbstractItemModel* model READ model WRITE setModel NOTIFY modelChanged)
    Q_PROPERTY(QQmlComponent* delegate READ delegate WRITE setDelegate NOTIFY delegateChanged)
    Q_PROPERTY(QQmlComponent* sectionDelegate READ sectionDelegate WRITE setSectionDelegate NOTIFY sectionDelegateChanged)
    Q_PROPERTY(QString sectionRole READ sectionRole WRITE setSectionRole NOTIFY sectionRoleChanged)
    Q_PROPERTY(QString estimateRole READ estimateRole WRITE setEstimateRole NOTIFY estimateRoleChanged)
    Q_PROPERTY(QQuickItem* header READ header WRITE setHeader NOTIFY headerChanged)
    Q_PROPERTY(QQuickItem* footer READ footer WRITE setFooter NOTIFY footerChanged)
    // The Flickable this layout is the content of: its contentY and height
    // are the viewport, and its contentY is corrected by the layout.
    Q_PROPERTY(QQuickItem* flickable READ flickable WRITE setFlickable NOTIFY flickableChanged)
    Q_PROPERTY(bool following READ following WRITE setFollowing NOTIFY followingChanged)
    Q_PROPERTY(qreal spacing READ spacing WRITE setSpacing NOTIFY spacingChanged)
    Q_PROPERTY(qreal leftMargin READ leftMargin WRITE setLeftMargin NOTIFY marginsChanged)
    Q_PROPERTY(qreal rightMargin READ rightMargin WRITE setRightMargin NOTIFY marginsChanged)
    Q_PROPERTY(qreal topMargin READ topMargin WRITE setTopMargin NOTIFY marginsChanged)
    Q_PROPERTY(qreal bottomMargin READ bottomMargin WRITE setBottomMargin NOTIFY marginsChanged)
    Q_PROPERTY(qreal cacheExtent READ cacheExtent WRITE setCacheExtent NOTIFY cacheExtentChanged)
    // Milliseconds per frame for creating rows inside the viewport; 0 means
    // create them all before the frame (opening a chat). While the reader
    // scrolls fast a budget keeps the scroll moving and fills rows in over
    // the next frames. Rows in the margin around the viewport always wait
    // for spare time.
    Q_PROPERTY(int creationBudget READ creationBudget WRITE setCreationBudget NOTIFY creationBudgetChanged)
    Q_PROPERTY(int count READ count NOTIFY countChanged)
    Q_PROPERTY(qreal contentHeight READ contentHeight NOTIFY contentHeightChanged)
    Q_PROPERTY(int measuredCount READ measuredCount NOTIFY contentHeightChanged)

  public:
    explicit TranscriptLayout(QQuickItem* parent = nullptr);
    ~TranscriptLayout() override;

    QAbstractItemModel* model() const { return m_model; }
    void setModel(QAbstractItemModel* model);
    QQmlComponent* delegate() const { return m_delegate; }
    void setDelegate(QQmlComponent* delegate);
    QQmlComponent* sectionDelegate() const { return m_sectionDelegate; }
    void setSectionDelegate(QQmlComponent* delegate);
    QString sectionRole() const { return m_sectionRole; }
    void setSectionRole(const QString& role);
    QString estimateRole() const { return m_estimateRole; }
    void setEstimateRole(const QString& role);
    QQuickItem* header() const { return m_header; }
    void setHeader(QQuickItem* item);
    QQuickItem* footer() const { return m_footer; }
    void setFooter(QQuickItem* item);
    QQuickItem* flickable() const { return m_flickable; }
    void setFlickable(QQuickItem* flickable);
    bool following() const { return m_following; }
    void setFollowing(bool following);
    qreal spacing() const { return m_spacing; }
    void setSpacing(qreal spacing);
    qreal leftMargin() const { return m_leftMargin; }
    void setLeftMargin(qreal margin);
    qreal rightMargin() const { return m_rightMargin; }
    void setRightMargin(qreal margin);
    qreal topMargin() const { return m_topMargin; }
    void setTopMargin(qreal margin);
    qreal bottomMargin() const { return m_bottomMargin; }
    void setBottomMargin(qreal margin);
    qreal cacheExtent() const { return m_cacheExtent; }
    void setCacheExtent(qreal extent);
    int creationBudget() const { return m_creationBudget; }
    void setCreationBudget(int milliseconds);
    int count() const { return static_cast<int>(m_rows.size()); }
    qreal contentHeight() const;
    int measuredCount() const;

    // Top of a row's block (its section heading, if any, then the row).
    Q_INVOKABLE qreal positionOf(int row) const;
    Q_INVOKABLE qreal heightOf(int row) const;
    // The row whose block contains y (content coordinates), clamped.
    Q_INVOKABLE int rowAt(qreal y) const;
    Q_INVOKABLE QQuickItem* itemAt(int row) const;
    Q_INVOKABLE bool isMeasured(int row) const;
    // The end of the content, where a following viewport sits.
    Q_INVOKABLE qreal endY() const;
    // The reader moved the viewport: remember where it is now.
    Q_INVOKABLE void anchorToViewport();
    // Put a row at the top (or bottom) of the viewport and anchor there.
    Q_INVOKABLE void positionAtRow(int row, bool atBottom);
    // Lay out now instead of before the next frame.
    Q_INVOKABLE void layoutNow();

  signals:
    void modelChanged();
    void delegateChanged();
    void sectionDelegateChanged();
    void sectionRoleChanged();
    void estimateRoleChanged();
    void headerChanged();
    void footerChanged();
    void flickableChanged();
    void followingChanged();
    void spacingChanged();
    void marginsChanged();
    void cacheExtentChanged();
    void creationBudgetChanged();
    void countChanged();
    void contentHeightChanged();
    // A correction moved the viewport to keep the anchor or the end in place.
    void viewportCorrected(qreal from, qreal to);

  private slots:
    void flickableMoved();

  protected:
    void updatePolish() override;
    void geometryChange(const QRectF& newGeometry, const QRectF& oldGeometry) override;
    void componentComplete() override;

  private:
    struct Row {
        qreal height = 0;          // the row's block: section + delegate
        bool measured = false;
        QQuickItem* item = nullptr;
        QQuickItem* section = nullptr;
        QQmlPropertyMap* modelData = nullptr;
        QString sectionLabel;
        bool sectionShown = false;
    };

    void resetRows();
    void onRowsInserted(const QModelIndex& parent, int first, int last);
    void onRowsAboutToBeRemoved(const QModelIndex& parent, int first, int last);
    void onRowsRemoved(const QModelIndex& parent, int first, int last);
    void onDataChanged(const QModelIndex& topLeft, const QModelIndex& bottomRight, const QList<int>& roles);
    void rememberAnchorIdentity();
    void restoreAnchorIdentity();
    void onItemHeightChanged();
    void scheduleLayout();
    void relayout();
    void rebuildPrefix() const;
    void correctViewport();
    bool placeVisibleRows();
    void releaseRow(Row& row);
    void createRow(int index);
    QVariantMap roleValues(int index) const;
    qreal estimate(int index) const;
    qreal rowWidth() const;
    qreal viewportY() const;
    qreal viewportHeight() const;
    qreal rowsTop() const;
    qreal headerHeight() const;
    qreal footerHeight() const;
    void updateSectionFlags(int from);
    void setViewportY(qreal y);

    QPointer<QAbstractItemModel> m_model;
    QList<QMetaObject::Connection> m_modelConnections;
    QPointer<QQmlComponent> m_delegate;
    QPointer<QQmlComponent> m_sectionDelegate;
    QString m_sectionRole;
    QString m_estimateRole = QStringLiteral("body");
    QPointer<QQuickItem> m_header;
    QPointer<QQuickItem> m_footer;
    QPointer<QQuickItem> m_flickable;
    QList<QMetaObject::Connection> m_flickableConnections;
    bool m_following = true;
    qreal m_spacing = 0;
    qreal m_leftMargin = 0;
    qreal m_rightMargin = 0;
    qreal m_topMargin = 0;
    qreal m_bottomMargin = 0;
    qreal m_cacheExtent = 400;
    int m_creationBudget = 0;
    bool m_creationPending = false;

    std::vector<Row> m_rows;
    mutable std::vector<qreal> m_prefix; // m_prefix[i] = sum of block heights before row i
    mutable bool m_prefixDirty = true;
    QHash<QByteArray, int> m_roleIds;    // role name -> role id
    QSet<QByteArray> m_delegateProperties;
    bool m_delegatePropertiesKnown = false;
    qreal m_sectionHeight = -1;

    int m_anchorRow = -1;
    qreal m_anchorOffset = 0;
    QString m_anchorIdentity;            // messageId of the anchor across resets
    qreal m_anchorIdentityOffset = 0;
    bool m_correcting = false;
    bool m_inLayout = false;
    qreal m_lastWidth = -1;
};
} // namespace clarp
