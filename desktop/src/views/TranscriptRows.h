#pragma once
#include <QAbstractListModel>
#include <QPointer>
#include <QtQmlIntegration/qqmlintegration.h>
#include <vector>

namespace clarp {
// The rows the transcript view shows. Most messages are one row. A large
// finished message is split into several rows at markdown block boundaries,
// and a large table into chunks of rows that repeat its header, so the view
// only ever lays out the parts on screen: one 140-row table laid out as a
// single text document blocked the GUI for 300-700 ms whenever it scrolled
// into view. Every part carries the message's roles; "body" is the part's own
// markdown, "fullBody" the whole message, "partIndex"/"partCount" its place,
// and "rowKey" identifies the part for the view's anchoring.
class TranscriptRows : public QAbstractListModel {
    Q_OBJECT
    QML_ELEMENT
    Q_PROPERTY(QAbstractItemModel* sourceModel READ sourceModel WRITE setSourceModel NOTIFY sourceModelChanged)
    Q_PROPERTY(int count READ count NOTIFY countChanged)

  public:
    explicit TranscriptRows(QObject* parent = nullptr);

    [[nodiscard]] QAbstractItemModel* sourceModel() const { return m_source; }
    void setSourceModel(QAbstractItemModel* model);
    [[nodiscard]] int count() const { return static_cast<int>(m_rows.size()); }

    [[nodiscard]] int rowCount(const QModelIndex& parent = {}) const override;
    [[nodiscard]] QVariant data(const QModelIndex& index, int role) const override;
    [[nodiscard]] QHash<int, QByteArray> roleNames() const override;

    // The first view row of a source row, for jumping to a message.
    Q_INVOKABLE [[nodiscard]] int rowForSource(int sourceRow) const;
    Q_INVOKABLE [[nodiscard]] int sourceRow(int row) const;

    // How a message is split; exposed for tests.
    [[nodiscard]] static QStringList splitMarkdown(const QString& markdown);

  signals:
    void sourceModelChanged();
    void countChanged();

  private:
    struct Row {
        int source = 0;
        int part = 0;
        int parts = 1;
        QString body;   // the part's markdown when split, else empty
    };
    [[nodiscard]] std::vector<Row> partsFor(int sourceRow) const;
    [[nodiscard]] int firstRowOf(int sourceRow) const;
    [[nodiscard]] int rowsOf(int sourceRow) const;
    void rebuild();
    void onRowsInserted(const QModelIndex& parent, int first, int last);
    void onRowsRemoved(const QModelIndex& parent, int first, int last);
    void onDataChanged(const QModelIndex& topLeft, const QModelIndex& bottomRight, const QList<int>& roles);
    void shiftSources(int from, int delta);

    QPointer<QAbstractItemModel> m_source;
    QList<QMetaObject::Connection> m_connections;
    std::vector<Row> m_rows;
    QHash<int, QByteArray> m_roleNames;
    int m_bodyRole = -1;
    int m_kindRole = -1;
    int m_activityRole = -1;
    int m_authorRole = -1;
    int m_messageIdRole = -1;
    int m_partIndexRole = -1;
    int m_partCountRole = -1;
    int m_fullBodyRole = -1;
    int m_rowKeyRole = -1;
};
} // namespace clarp
