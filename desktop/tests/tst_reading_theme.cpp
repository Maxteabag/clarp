#include "app/ReadingTheme.h"
#include <QColor>
#include <QSet>
#include <QTest>

namespace {
double channel(double value) {
    return value <= 0.03928 ? value / 12.92 : std::pow((value + 0.055) / 1.055, 2.4);
}
double luminance(const QColor& color) {
    return 0.2126 * channel(color.redF()) + 0.7152 * channel(color.greenF())
        + 0.0722 * channel(color.blueF());
}
// WCAG 2.x contrast ratio.
double contrast(const QString& foreground, const QString& background) {
    const double a = luminance(QColor(foreground));
    const double b = luminance(QColor(background));
    return (std::max(a, b) + 0.05) / (std::min(a, b) + 0.05);
}
}  // namespace

class ReadingThemeTest : public QObject {
    Q_OBJECT
  private slots:
    void bodyTextMeetsAaaOnEverySurface() {
        for (const auto& theme : clarp::readingThemes()) {
            const auto label = theme.id.toUtf8();
            QVERIFY2(contrast(theme.text, theme.background) >= 7.0, label);
            QVERIFY2(contrast(theme.text, theme.bubble) >= 7.0, label);
            QVERIFY2(contrast(theme.selectedText, theme.selection) >= 4.5, label);
        }
    }
    void secondaryTextMeetsAa() {
        for (const auto& theme : clarp::readingThemes()) {
            const auto label = theme.id.toUtf8();
            QVERIFY2(contrast(theme.mutedText, theme.background) >= 4.5, label);
            QVERIFY2(contrast(theme.faintText, theme.background) >= 4.5, label);
            QVERIFY2(contrast(theme.link, theme.background) >= 4.5, label);
        }
    }
    void chromeRolesStayReadableOnEveryTheme() {
        for (const auto& theme : clarp::readingThemes()) {
            const auto label = theme.id.toUtf8();
            for (const auto& surface : {theme.window, theme.raised, theme.control, theme.sunken}) {
                QVERIFY2(contrast(theme.chromeText, surface) >= 7.0, label);
                QVERIFY2(contrast(theme.secondary, surface) >= 4.5, label);
            }
            QVERIFY2(contrast(theme.mutedText, theme.window) >= 4.5, label);
            QVERIFY2(contrast(theme.accent, theme.window) >= 3.0, label);
            QVERIFY2(contrast(theme.accentText, theme.accent) >= 4.5, label);
            QVERIFY2(contrast(theme.warning, theme.window) >= 4.5, label);
            QVERIFY2(contrast(theme.danger, theme.window) >= 4.5, label);
            QVERIFY2(contrast(theme.success, theme.window) >= 3.0, label);
            QVERIFY2(contrast(theme.border, theme.window) >= 1.3, label);
            QVERIFY2(theme.light == (QColor(theme.window).lightnessF() > 0.5), label);
        }
        QVERIFY(clarp::readingTheme(QStringLiteral("paper")).light);
        QVERIFY(!clarp::readingTheme(QStringLiteral("terminal")).light);
        const auto style = clarp::readingThemeStyle(QStringLiteral("paper"), [](const QString&) { return true; });
        QVERIFY(style.value(QStringLiteral("light")).toBool());
        QCOMPARE(style.value(QStringLiteral("window")).toString(), clarp::readingTheme(QStringLiteral("paper")).window);
    }
    void noPurePolarityExtremes() {
        // Pure white on black (or black on white) is harsher to read than a
        // softer pairing; every theme should stop short of both extremes.
        for (const auto& theme : clarp::readingThemes()) {
            const auto label = theme.id.toUtf8();
            QVERIFY2(contrast(theme.text, theme.background) < 18.0, label);
            QVERIFY2(QColor(theme.text) != QColor(Qt::white), label);
            QVERIFY2(QColor(theme.text) != QColor(Qt::black), label);
        }
    }
    void bodySizeAndMeasureStayInReadingRange() {
        for (const auto& theme : clarp::readingThemes()) {
            const auto label = theme.id.toUtf8();
            QVERIFY2(theme.fontPixelSize >= 15 && theme.fontPixelSize <= 18, label);
            // Roughly 0.5em average advance: 60–110 characters per line.
            const double characters = theme.measure / (theme.fontPixelSize * 0.5);
            QVERIFY2(characters >= 60 && characters <= 115, label);
        }
    }
    void idsAreUniqueAndUnknownFallsBackToTerminal() {
        QSet<QString> ids;
        for (const auto& theme : clarp::readingThemes()) {
            QVERIFY(!ids.contains(theme.id));
            ids.insert(theme.id);
            QVERIFY(!theme.fontFamilies.isEmpty());
        }
        QCOMPARE(clarp::normalizedReadingThemeId(QStringLiteral("nope")), QStringLiteral("terminal"));
        QCOMPARE(clarp::normalizedReadingThemeId(QString()), QStringLiteral("terminal"));
        QCOMPARE(clarp::normalizedReadingThemeId(QStringLiteral("paper")), QStringLiteral("paper"));
        QCOMPARE(clarp::defaultReadingThemeId(), QStringLiteral("terminal"));
    }
    void optionsMirrorThemes() {
        const auto options = clarp::readingThemeOptions();
        QCOMPARE(options.size(), clarp::readingThemes().size());
        for (int i = 0; i < options.size(); ++i) {
            const auto option = options[i].toMap();
            QCOMPARE(option.value(QStringLiteral("id")).toString(), clarp::readingThemes()[i].id);
            QVERIFY(!option.value(QStringLiteral("label")).toString().isEmpty());
            QVERIFY(!option.value(QStringLiteral("detail")).toString().isEmpty());
        }
    }
    void fontFallsBackToTheNextInstalledFamily() {
        const auto paper = clarp::readingTheme(QStringLiteral("paper"));
        QCOMPARE(clarp::resolveReadingFont(paper, [](const QString&) { return true; }),
                 QStringLiteral("Literata"));
        QCOMPARE(clarp::resolveReadingFont(paper, [](const QString& f) { return f == QStringLiteral("Noto Serif"); }),
                 QStringLiteral("Noto Serif"));
        // Nothing installed: the generic CSS family lets Qt choose a serif.
        QCOMPARE(clarp::resolveReadingFont(paper, [](const QString&) { return false; }),
                 QStringLiteral("serif"));
        const auto style = clarp::readingThemeStyle(QStringLiteral("paper"), [](const QString&) { return true; });
        QCOMPARE(style.value(QStringLiteral("fontFamily")).toString(), QStringLiteral("Literata"));
        QCOMPARE(style.value(QStringLiteral("background")).toString(), paper.background);
        QCOMPARE(style.value(QStringLiteral("fontPixelSize")).toInt(), 17);
        // Unknown ids style as the terminal default rather than an empty map.
        QCOMPARE(clarp::readingThemeStyle(QStringLiteral("x"), [](const QString&) { return true; })
                     .value(QStringLiteral("id")).toString(), QStringLiteral("terminal"));
    }
};
QTEST_GUILESS_MAIN(ReadingThemeTest)
#include "tst_reading_theme.moc"
