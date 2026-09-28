#include "views/TranscriptRows.h"

#include <QSignalSpy>
#include <QStandardItemModel>
#include <QTest>

namespace {
enum Roles { Body = Qt::UserRole + 1, Kind, Author, Activity, MessageId };

QString table(int rows) {
    QString text = QStringLiteral("| n | name |\n|---|---|");
    for (int i = 0; i < rows; ++i) text += QStringLiteral("\n| %1 | row %1 |").arg(i);
    return text;
}

QStandardItem* message(const QString& id, const QString& body, const QString& kind = QStringLiteral("final")) {
    auto* item = new QStandardItem;
    item->setData(id, MessageId);
    item->setData(body, Body);
    item->setData(kind, Kind);
    item->setData(QStringLiteral("assistant"), Author);
    item->setData(false, Activity);
    return item;
}

QStandardItemModel* sourceModel(QObject* parent) {
    auto* model = new QStandardItemModel(parent);
    model->setItemRoleNames({{Body, "body"}, {Kind, "messageKind"}, {Author, "authorRole"},
                             {Activity, "activity"}, {MessageId, "messageId"}});
    return model;
}

QString role(const clarp::TranscriptRows& rows, int row, const QByteArray& name) {
    return rows.data(rows.index(row), rows.roleNames().key(name)).toString();
}
} // namespace

class TranscriptRowsTest : public QObject {
    Q_OBJECT
  private slots:
    void bigTablesSplitIntoChunksThatRepeatTheHeader() {
        const QStringList parts = clarp::TranscriptRows::splitMarkdown(QStringLiteral("Intro.\n\n") + table(140));
        QVERIFY(parts.size() >= 9);
        for (int i = 1; i < parts.size(); ++i)
            QVERIFY2(parts.at(i).startsWith(QStringLiteral("| n | name |\n|---|---|")), qPrintable(parts.at(i).left(40)));
        // Every table row appears exactly once across the parts.
        int rows = 0;
        for (const QString& part : parts) rows += static_cast<int>(part.count(QStringLiteral("| row ")));
        QCOMPARE(rows, 140);
    }

    void smallMessagesStayWhole() {
        QCOMPARE(clarp::TranscriptRows::splitMarkdown(QStringLiteral("Short.\n\n") + table(10)).size(), 1);
    }

    void longTextSplitsAtBlockBoundaries() {
        QString text;
        for (int i = 0; i < 40; ++i) text += QStringLiteral("Paragraph %1 ").arg(i) + QString(300, u'x') + QStringLiteral("\n\n");
        const QStringList parts = clarp::TranscriptRows::splitMarkdown(text.trimmed());
        QVERIFY(parts.size() >= 4);
        QCOMPARE(parts.join(QStringLiteral("\n\n")), text.trimmed());
    }

    void finishingMessageSplitsWithoutResetting() {
        QObject owner;
        QStandardItemModel* source = sourceModel(&owner);
        source->appendRow(message(QStringLiteral("a"), QStringLiteral("Hello.")));
        source->appendRow(message(QStringLiteral("b"), table(140), QStringLiteral("live")));
        clarp::TranscriptRows rows;
        rows.setSourceModel(source);
        QCOMPARE(rows.count(), 2); // a live message is never split
        QSignalSpy resets(&rows, &QAbstractItemModel::modelReset);
        QSignalSpy inserted(&rows, &QAbstractItemModel::rowsInserted);
        source->item(1)->setData(QStringLiteral("final"), Kind);
        QCOMPARE(resets.count(), 0);
        QVERIFY(inserted.count() >= 1);
        QVERIFY(rows.count() > 9);
        QCOMPARE(role(rows, 1, "rowKey"), QStringLiteral("b#0"));
        QCOMPARE(role(rows, 2, "rowKey"), QStringLiteral("b#1"));
        QCOMPARE(role(rows, 1, "fullBody"), table(140));
        QCOMPARE(rows.data(rows.index(1), rows.roleNames().key("partCount")).toInt(), rows.count() - 1);
    }

    void insertsAndRemovesMapAroundSplitMessages() {
        QObject owner;
        QStandardItemModel* source = sourceModel(&owner);
        source->appendRow(message(QStringLiteral("a"), QStringLiteral("One.")));
        source->appendRow(message(QStringLiteral("big"), table(100)));
        source->appendRow(message(QStringLiteral("c"), QStringLiteral("Three.")));
        clarp::TranscriptRows rows;
        rows.setSourceModel(source);
        const int bigParts = rows.count() - 2;
        QVERIFY(bigParts > 1);
        source->insertRow(0, message(QStringLiteral("older"), QStringLiteral("Zero.")));
        QCOMPARE(rows.count(), bigParts + 3);
        QCOMPARE(role(rows, 0, "rowKey"), QStringLiteral("older"));
        QCOMPARE(rows.rowForSource(2), 2);
        QCOMPARE(rows.rowForSource(3), 2 + bigParts);
        QCOMPARE(role(rows, rows.rowForSource(3), "rowKey"), QStringLiteral("c"));
        source->removeRow(2); // the split message
        QCOMPARE(rows.count(), 3);
        QCOMPARE(role(rows, 2, "rowKey"), QStringLiteral("c"));
        QCOMPARE(rows.sourceRow(2), 2);
    }
};

QTEST_MAIN(TranscriptRowsTest)
#include "tst_transcript_rows.moc"
