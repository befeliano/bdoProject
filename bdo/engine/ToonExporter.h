#ifndef TOONEXPORTER_H
#define TOONEXPORTER_H

#include <string>

class ToonExporter {
public:
    static std::string generateMockToonData(
        const std::string& lSystemString,
        double snapThreshold,
        float oneWayProb = 0.2f,
        int seed = 42,
        double angleStep = 90.0          // ← YENİ, varsayılan eskisiyle aynı
    );
};

#endif