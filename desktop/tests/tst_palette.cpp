#include "app/DesktopPalette.h"
#include <QTest>

class PaletteTest : public QObject {
    Q_OBJECT
  private slots:
    void overridesLightHostInEveryState() {
        QPalette host;
        for (const auto role : {QPalette::Window, QPalette::Base, QPalette::AlternateBase,
                               QPalette::Button, QPalette::ToolTipBase}) {
            host.setColor(QPalette::All, role, Qt::white);
        }
        const auto palette = clarp::desktopPalette(host);
        for (const auto group : {QPalette::Active, QPalette::Inactive, QPalette::Disabled}) {
            for (const auto role : {QPalette::Window, QPalette::Base, QPalette::AlternateBase,
                                   QPalette::Button, QPalette::ToolTipBase}) {
                QVERIFY2(palette.color(group, role).lightnessF() < 0.25,
                         "Host light palette leaked into a dark control surface");
            }
            QVERIFY(palette.color(group, QPalette::Text).lightnessF() > 0.45);
            QVERIFY(palette.color(group, QPalette::ButtonText).lightnessF() > 0.45);
        }
        QCOMPARE(palette.color(QPalette::Active, QPalette::Window), QColor("#1a1b26"));
    }
};
QTEST_GUILESS_MAIN(PaletteTest)
#include "tst_palette.moc"
