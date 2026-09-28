#include "views/TranscriptLayout.h"

#include <QQuickItem>
#include <QQuickView>
#include <QSignalSpy>
#include <QTemporaryDir>
#include <QTest>
#include <QQmlEngine>
#include <QtQml/qqml.h>

#include <cmath>
#include <memory>

namespace {
constexpr int ViewWidth = 320;
constexpr int ViewHeight = 240;

const char* const kScene = R"QML(
import QtQuick
import ClarpTest 1.0

Item {
    id: root
    width: 320
    height: 240
    property alias flickable: flick
    property alias layout: layout
    property alias rows: rows

    ListModel {
        id: rows
        ListElement { messageId: "__seed__"; rowHeight: 1 }
    }

    Flickable {
        id: flick
        objectName: "flick"
        anchors.fill: parent
        contentWidth: width
        contentHeight: layout.contentHeight
        boundsBehavior: Flickable.StopAtBounds
        clip: true

        TranscriptLayout {
            id: layout
            objectName: "layout"
            width: flick.width
            flickable: flick
            model: rows
            cacheExtent: 120
            creationBudget: 0
            estimateRole: "messageId"
            delegate: Component {
                Rectangle {
                    objectName: "rowDelegate"
                    property int index: -1
                    property var model: null
                    property string messageId: model ? model.messageId : ""
                    width: layout.width
                    height: model ? model.rowHeight : 0
                    color: "transparent"
                }
            }
        }
    }

    function appendRow(messageId, rowHeight) {
        rows.append({"messageId": messageId, "rowHeight": rowHeight})
    }
    function appendRows(count, prefix, rowHeight) {
        for (let i = 0; i < count; ++i)
            appendRow(prefix + i, rowHeight)
    }
    function insertRow(index, messageId, rowHeight) {
        rows.insert(index, {"messageId": messageId, "rowHeight": rowHeight})
    }
    function prependRows(count, prefix, rowHeight) {
        for (let i = count - 1; i >= 0; --i)
            rows.insert(0, {"messageId": prefix + i, "rowHeight": rowHeight})
    }
    function setRowHeight(index, rowHeight) {
        rows.setProperty(index, "rowHeight", rowHeight)
    }
    function removeRows(index, count) {
        for (let i = 0; i < count; ++i)
            rows.remove(index)
    }
    function resetRows(count, prefix, rowHeight, keepId, keepIndex) {
        rows.clear()
        for (let i = 0; i < count; ++i) {
            const id = i === keepIndex ? keepId : prefix + i
            rows.append({"messageId": id, "rowHeight": rowHeight})
        }
    }
    function indexOfMessage(messageId) {
        for (let i = 0; i < rows.count; ++i) {
            if (rows.get(i).messageId === messageId)
                return i
        }
        return -1
    }
    Component.onCompleted: rows.clear()
}
)QML";

class Fixture {
public:
    Fixture() {
        QVERIFY(dir.isValid());
        const QString path = dir.filePath(QStringLiteral("scene.qml"));
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text));
        file.write(kScene);
        file.close();

        view = std::make_unique<QQuickView>();
        view->setColor(Qt::transparent);
        view->setResizeMode(QQuickView::SizeRootObjectToView);
        view->resize(ViewWidth, ViewHeight);
        view->setSource(QUrl::fromLocalFile(path));
        QCOMPARE(view->status(), QQuickView::Ready);
        view->show();
        QVERIFY(QTest::qWaitForWindowExposed(view.get()));

        root = view->rootObject();
        QVERIFY(root != nullptr);
        layout = root->findChild<clarp::TranscriptLayout*>(QStringLiteral("layout"));
        flick = root->findChild<QQuickItem*>(QStringLiteral("flick"));
        QVERIFY(layout != nullptr);
        QVERIFY(flick != nullptr);
    }

    void call2(const char* method, const QVariant& a, const QVariant& b) const {
        QVariant ignored;
        QVERIFY(QMetaObject::invokeMethod(root, method, Q_RETURN_ARG(QVariant, ignored),
            Q_ARG(QVariant, a), Q_ARG(QVariant, b)));
    }

    void call3(const char* method, const QVariant& a, const QVariant& b, const QVariant& c) const {
        QVariant ignored;
        QVERIFY(QMetaObject::invokeMethod(root, method, Q_RETURN_ARG(QVariant, ignored),
            Q_ARG(QVariant, a), Q_ARG(QVariant, b), Q_ARG(QVariant, c)));
    }

    void call5(const char* method, const QVariant& a, const QVariant& b,
               const QVariant& c, const QVariant& d, const QVariant& e) const {
        QVariant ignored;
        QVERIFY(QMetaObject::invokeMethod(root, method, Q_RETURN_ARG(QVariant, ignored),
            Q_ARG(QVariant, a), Q_ARG(QVariant, b), Q_ARG(QVariant, c),
            Q_ARG(QVariant, d), Q_ARG(QVariant, e)));
    }

    [[nodiscard]] int indexOf(const QString& messageId) const {
        QVariant result;
        const bool invoked = QMetaObject::invokeMethod(root, "indexOfMessage", Q_RETURN_ARG(QVariant, result),
            Q_ARG(QVariant, messageId));
        if (!invoked) qFatal("indexOfMessage invocation failed");
        return result.toInt();
    }

    void appendRows(int count, const QString& prefix = QStringLiteral("m"), qreal height = 30) const {
        call3("appendRows", count, prefix, height);
        settle();
    }

    void settle(int passes = 4) const {
        if (layout == nullptr) return;
        for (int i = 0; i < passes; ++i) {
            layout->layoutNow();
            QCoreApplication::processEvents();
        }
    }

    [[nodiscard]] qreal contentY() const { return flick->property("contentY").toReal(); }
    void setContentY(qreal y) const {
        flick->setProperty("contentY", y);
        QCoreApplication::processEvents();
    }
    [[nodiscard]] qreal screenOffset(int row) const { return layout->positionOf(row) - contentY(); }

    QTemporaryDir dir;
    std::unique_ptr<QQuickView> view;
    QQuickItem* root = nullptr;
    clarp::TranscriptLayout* layout = nullptr;
    QQuickItem* flick = nullptr;
};

void compareNear(qreal actual, qreal expected, qreal tolerance, const char* what) {
    QVERIFY2(std::abs(actual - expected) <= tolerance,
        qPrintable(QStringLiteral("%1: actual=%2 expected=%3 tolerance=%4")
            .arg(QString::fromUtf8(what)).arg(actual).arg(expected).arg(tolerance)));
}
} // namespace

class TranscriptLayoutTest : public QObject {
    Q_OBJECT

private slots:
    void initTestCase() {
        qputenv("QT_QPA_PLATFORM", "offscreen");
        qputenv("QT_QUICK_BACKEND", "software");
        qmlRegisterType<clarp::TranscriptLayout>("ClarpTest", 1, 0, "TranscriptLayout");
    }

    void followingTracksEndAfterModelAndHeightChanges() {
        Fixture f;
        f.appendRows(20);
        if (f.layout == nullptr || f.flick == nullptr) QFAIL("fixture did not load");
        f.layout->setFollowing(true);
        f.settle();
        compareNear(f.contentY(), f.layout->endY(), 1, "initial following end");

        f.call3("appendRows", 5, QStringLiteral("a"), 32);
        f.settle();
        compareNear(f.contentY(), f.layout->endY(), 1, "following after append");

        f.call2("setRowHeight", 22, 90);
        f.settle();
        compareNear(f.contentY(), f.layout->endY(), 1, "following after row growth");

        f.call2("removeRows", 4, 3);
        f.settle();
        compareNear(f.contentY(), f.layout->endY(), 1, "following after removals");
    }

    void anchorSurvivesChangesAboveViewport() {
        Fixture f;
        f.appendRows(80);
        if (f.layout == nullptr || f.flick == nullptr) QFAIL("fixture did not load");
        f.layout->positionAtRow(40, false);
        f.settle();
        const QString anchor = QStringLiteral("m40");
        qreal offset = f.screenOffset(f.indexOf(anchor));

        f.call3("insertRow", 10, QStringLiteral("inserted"), 44);
        f.settle();
        compareNear(f.screenOffset(f.indexOf(anchor)), offset, 1, "insert above anchor");

        f.call2("setRowHeight", 5, 140);
        f.settle();
        compareNear(f.screenOffset(f.indexOf(anchor)), offset, 1, "grow above anchor");

        f.call2("setRowHeight", 5, 18);
        f.settle();
        compareNear(f.screenOffset(f.indexOf(anchor)), offset, 1, "shrink above anchor");

        f.call3("prependRows", 300, QStringLiteral("p"), 21);
        f.settle();
        compareNear(f.screenOffset(f.indexOf(anchor)), offset, 1, "prepend many rows");

        f.call2("removeRows", 20, 30);
        f.settle();
        compareNear(f.screenOffset(f.indexOf(anchor)), offset, 1, "remove rows above anchor");
    }

    void modelResetKeepsSameMessageId() {
        Fixture f;
        f.appendRows(60);
        if (f.layout == nullptr || f.flick == nullptr) QFAIL("fixture did not load");
        f.layout->positionAtRow(30, false);
        f.settle();
        const QString anchor = QStringLiteral("m30");
        const qreal offset = f.screenOffset(f.indexOf(anchor));

        f.call5("resetRows", 70, QStringLiteral("r"), 34, anchor, 44);
        f.settle();
        QCOMPARE(f.indexOf(anchor), 44);
        compareNear(f.screenOffset(44), offset, 1, "model reset anchor identity");
    }

    void widthChangeKeepsAnchorOnScreen() {
        Fixture f;
        f.appendRows(100);
        if (f.layout == nullptr || f.flick == nullptr) QFAIL("fixture did not load");
        f.layout->positionAtRow(50, false);
        f.settle();

        f.view->resize(520, ViewHeight);
        f.settle();
        const int row = f.indexOf(QStringLiteral("m50"));
        QVERIFY(row >= 0);
        const qreal offset = f.screenOffset(row);
        QVERIFY2(offset >= -1 && offset <= ViewHeight - 1,
            qPrintable(QStringLiteral("anchored row should remain on screen, offset=%1").arg(offset)));
    }

    void positionAtRowStaysAtTopAfterNeighbourMeasurement() {
        Fixture f;
        f.appendRows(120);
        if (f.layout == nullptr || f.flick == nullptr) QFAIL("fixture did not load");
        f.layout->positionAtRow(70, false);
        f.settle();
        compareNear(f.contentY(), f.layout->positionOf(70), 1, "row positioned at top");

        f.call2("setRowHeight", 69, 105);
        f.call2("setRowHeight", 71, 64);
        f.settle();
        compareNear(f.contentY(), f.layout->positionOf(70), 1, "row remains at top");
    }

    void rowsFarFromViewportAreNotCreated() {
        Fixture f;
        f.appendRows(120);
        if (f.layout == nullptr || f.flick == nullptr) QFAIL("fixture did not load");
        f.layout->positionAtRow(40, false);
        f.settle();

        const qreal top = f.contentY();
        const qreal bottom = top + ViewHeight;
        for (int i = 0; i < f.layout->count(); ++i) {
            if (f.layout->itemAt(i) == nullptr) continue;
            const qreal rowTop = f.layout->positionOf(i);
            const qreal rowBottom = rowTop + f.layout->heightOf(i);
            QVERIFY2(rowBottom >= top - 2 * ViewHeight && rowTop <= bottom + 2 * ViewHeight,
                qPrintable(QStringLiteral("row %1 was created far from viewport: rowTop=%2 rowBottom=%3 viewport=%4..%5")
                    .arg(i).arg(rowTop).arg(rowBottom).arg(top).arg(bottom)));
        }
        QVERIFY(f.layout->itemAt(0) == nullptr);
        QVERIFY(f.layout->itemAt(119) == nullptr);
    }

    void createdRowsMatchDelegateHeightsAndAreContiguous() {
        Fixture f;
        f.appendRows(50);
        if (f.layout == nullptr || f.flick == nullptr) QFAIL("fixture did not load");
        f.layout->positionAtRow(20, false);
        f.settle();

        int previous = -1;
        for (int i = 0; i < f.layout->count(); ++i) {
            QQuickItem* item = f.layout->itemAt(i);
            if (item == nullptr) continue;
            compareNear(f.layout->heightOf(i), item->height(), 0.5, "height equals delegate height");
            compareNear(item->y(), f.layout->positionOf(i), 0.5, "item y equals row position");
            if (previous >= 0) {
                compareNear(f.layout->positionOf(i), f.layout->positionOf(previous) + f.layout->heightOf(previous),
                    0.5, "created rows are contiguous");
            }
            previous = i;
        }
        QVERIFY(previous >= 0);
    }
};

QTEST_MAIN(TranscriptLayoutTest)
#include "tst_transcript_layout.moc"
