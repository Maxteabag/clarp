#include "app/WorkspaceContext.h"
#include <QDir>
#include <QProcess>
#include <QTemporaryDir>
#include <QTest>

class WorkspaceContextTest final : public QObject {
    Q_OBJECT
  private slots:
    void ordinaryDirectoryAndRealGitWorktree() {
        QTemporaryDir temp;
        QVERIFY(temp.isValid());
        const QString repo = temp.path() + QStringLiteral("/project");
        const QString linked = temp.path() + QStringLiteral("/feature-view");
        QVERIFY(QDir().mkpath(repo));
        auto git = [&repo](const QStringList& arguments) {
            QProcess process;
            process.setWorkingDirectory(repo);
            process.start(QStringLiteral("git"), QStringList{QStringLiteral("-c"), QStringLiteral("core.hooksPath=/dev/null")} + arguments);
            return process.waitForFinished(10000) && process.exitCode() == 0;
        };
        QVERIFY(git({QStringLiteral("init")}));
        QVERIFY(git({QStringLiteral("-c"), QStringLiteral("user.name=Fixture"), QStringLiteral("-c"),
            QStringLiteral("user.email=fixture@example.invalid"), QStringLiteral("-c"), QStringLiteral("commit.gpgsign=false"),
            QStringLiteral("commit"), QStringLiteral("--allow-empty"), QStringLiteral("-m"), QStringLiteral("fixture")}));
        QVERIFY(git({QStringLiteral("worktree"), QStringLiteral("add"), QStringLiteral("--detach"), linked}));
        QVERIFY(QDir().mkpath(linked + QStringLiteral("/src")));
        clarp::WorkspaceContext context;
        const auto plain = context.describe(temp.path(), true);
        QCOMPARE(plain.value(QStringLiteral("kind")).toString(), QStringLiteral("directory"));
        const auto main = context.describe(repo, true);
        QCOMPARE(main.value(QStringLiteral("kind")).toString(), QStringLiteral("repo"));
        QCOMPARE(main.value(QStringLiteral("repository")).toString(), QStringLiteral("project"));
        const auto branch = context.describe(linked + QStringLiteral("/src"), true);
        QCOMPARE(branch.value(QStringLiteral("kind")).toString(), QStringLiteral("worktree"));
        QCOMPARE(branch.value(QStringLiteral("label")).toString(), QStringLiteral("project / feature-view / src"));
        QCOMPARE(branch.value(QStringLiteral("root")).toString(), linked);
        const auto remote = context.describe(linked, false);
        QCOMPARE(remote.value(QStringLiteral("kind")).toString(), QStringLiteral("directory"));
        QVERIFY(!remote.value(QStringLiteral("verified")).toBool());
        QVERIFY(!remote.contains(QStringLiteral("repository")));
    }
};
QTEST_GUILESS_MAIN(WorkspaceContextTest)
#include "tst_workspace_context.moc"
