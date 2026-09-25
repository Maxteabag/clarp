#include "app/MarkdownStyle.h"
#include <QTest>
#include <QTextBlock>
#include <QTextDocument>

class MarkdownStyleTest : public QObject {
    Q_OBJECT
    static QTextDocument* fixture() {
        auto* document = new QTextDocument;
        document->setMarkdown(QStringLiteral(
            "# Title\n\nBody with `inline` code and a [link](https://example.com).\n\n"
            "## Second\n\n```cpp\nint main() {}\n```\n\n> quoted\n\n- one\n- two\n\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n"));
        return document;
    }
    static QTextBlock blockStarting(const QTextDocument* document, const QString& prefix) {
        for (QTextBlock block = document->begin(); block.isValid(); block = block.next())
            if (block.text().startsWith(prefix)) return block;
        return {};
    }
  private slots:
    void headingsScaleWithTheBodyNotTheWeb() {
        QScopedPointer<QTextDocument> document(fixture());
        clarp::MarkdownStyleOptions options;
        options.bodyPixelSize = 16;
        QVERIFY(clarp::applyMarkdownStyle(document.data(), options));
        const QTextBlock h1 = blockStarting(document.data(), QStringLiteral("Title"));
        const QTextBlock h2 = blockStarting(document.data(), QStringLiteral("Second"));
        QCOMPARE(h1.begin().fragment().charFormat().intProperty(QTextFormat::FontPixelSize), 21);  // 1.3x
        QCOMPARE(h2.begin().fragment().charFormat().intProperty(QTextFormat::FontPixelSize), 19);  // 1.18x
        QCOMPARE(h1.begin().fragment().charFormat().intProperty(QTextFormat::FontSizeAdjustment), 0);
        QVERIFY(h1.begin().fragment().charFormat().fontWeight() >= QFont::DemiBold);
    }
    void codeUsesMonoAndBackground() {
        QScopedPointer<QTextDocument> document(fixture());
        clarp::MarkdownStyleOptions options;
        options.monoFamily = QStringLiteral("Mono Test");
        options.codeBackground = QColor(QStringLiteral("#123456"));
        clarp::applyMarkdownStyle(document.data(), options);
        const QTextBlock code = blockStarting(document.data(), QStringLiteral("int main"));
        QVERIFY(code.isValid());
        QCOMPARE(code.blockFormat().background().color(), QColor(QStringLiteral("#123456")));
        QCOMPARE(code.begin().fragment().charFormat().fontFamilies().toStringList().first(), QStringLiteral("Mono Test"));
        // Inline code keeps its own tint inside a normal paragraph.
        const QTextBlock body = blockStarting(document.data(), QStringLiteral("Body"));
        bool inlineFound = false;
        for (auto it = body.begin(); !it.atEnd(); ++it) {
            if (it.fragment().text() == QStringLiteral("inline")) {
                inlineFound = true;
                QCOMPARE(it.fragment().charFormat().background().color(), QColor(QStringLiteral("#123456")));
            }
            if (it.fragment().charFormat().isAnchor())
                QCOMPARE(it.fragment().charFormat().foreground().color(), options.link);
        }
        QVERIFY(inlineFound);
    }
    void quotesAndTablesAreStyled() {
        QScopedPointer<QTextDocument> document(fixture());
        clarp::MarkdownStyleOptions options;
        options.quoteText = QColor(QStringLiteral("#abcdef"));
        clarp::applyMarkdownStyle(document.data(), options);
        const QTextBlock quote = blockStarting(document.data(), QStringLiteral("quoted"));
        QVERIFY(quote.isValid());
        QCOMPARE(quote.begin().fragment().charFormat().foreground().color(), QColor(QStringLiteral("#abcdef")));
        QVERIFY(quote.blockFormat().leftMargin() > 0);
        QVERIFY(!document->rootFrame()->childFrames().isEmpty());
    }
    void secondPassIsANoOp() {
        QScopedPointer<QTextDocument> document(fixture());
        clarp::MarkdownStyleOptions options;
        QVERIFY(clarp::applyMarkdownStyle(document.data(), options));
        QVERIFY(!clarp::applyMarkdownStyle(document.data(), options));
        options.bodyPixelSize = 17;
        QVERIFY(clarp::applyMarkdownStyle(document.data(), options));  // Changed options restyle.
    }
    void optionsParseFromQml() {
        const auto options = clarp::markdownStyleOptions({{QStringLiteral("bodyPixelSize"), 17},
                                                          {QStringLiteral("link"), QStringLiteral("#1f7a78")},
                                                          {QStringLiteral("codeBackground"), QStringLiteral("not a colour")}});
        QCOMPARE(options.bodyPixelSize, 17);
        QCOMPARE(options.link, QColor(QStringLiteral("#1f7a78")));
        QCOMPARE(options.codeBackground, QColor(QStringLiteral("#20212e")));  // invalid input keeps the default
    }
};
QTEST_GUILESS_MAIN(MarkdownStyleTest)
#include "tst_markdown_style.moc"
