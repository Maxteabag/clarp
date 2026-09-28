// Exercise the production Qt reducer without a window, display, or app instance.
#include "models/BackgroundJobTracker.h"
#include <QCoreApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <iostream>
#include <string>
int main(int argc, char** argv) {
    QCoreApplication app(argc, argv);
    clarp::BackgroundJobTracker tracker;
    tracker.applyList(QJsonObject{{"jobs", QJsonArray{}}});
    std::string line;
    while (std::getline(std::cin, line)) {
        tracker.applyEvent(QJsonDocument::fromJson(QByteArray::fromStdString(line)).object());
        QJsonArray ids;
        const auto counts = tracker.countsByAgent();
        for (auto it = counts.cbegin(); it != counts.cend(); ++it)
            for (const auto& job : tracker.activeJobs(it.key())) ids.append(job.value("job_id"));
        std::cout << QJsonDocument(QJsonObject{{"ids", ids}, {"count", ids.size()}})
            .toJson(QJsonDocument::Compact).toStdString() << std::endl;
    }
}
