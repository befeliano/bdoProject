#include <iostream>
#include <fstream>
#include <string>
#include <map>
#include "LSystem.h"
#include "ToonExporter.h"

// ─────────────────────────────────────────────────────────────────────────────
//  ŞEHİR TİPİ KATALOĞU
// ─────────────────────────────────────────────────────────────────────────────
struct CityPreset {
    std::string axiom;
    std::map<char, std::string> rules;
    double angleStep;
    int    recommendedIter;
    std::string description;
};

CityPreset getPreset(const std::string& name) {
    CityPreset p;

    if (name == "grid") {
        p.axiom = "F+F+F+F";
        p.rules[ 'F' ] = "FF+F+F+F+FF";
        p.angleStep = 90.0;
        p.recommendedIter = 2;
        p.description = "Manhattan grid (paralel sokaklar)";
        return p;
    }
    if (name == "organic") {
        p.axiom = "X";
        p.rules[ 'X' ] = "F-[[X]+X]+F[+FX]-X";
        p.rules[ 'F' ] = "FF";
        p.angleStep = 25.0;
        p.recommendedIter = 4;
        p.description = "Organik dallanan (eski kasaba)";
        return p;
    }
    if (name == "radial") {
        p.axiom = "F[+F][-F][++F][--F]";
        p.rules[ 'F' ] = "F[+F]F[-F]F";
        p.angleStep = 72.0;
        p.recommendedIter = 3;
        p.description = "Radyal (merkezden yayilan)";
        return p;
    }

    // default
    p.axiom = "F+F+F+F";
    p.rules[ 'F' ] = "F[+F]F[-F]F";
    p.angleStep = 90.0;
    p.recommendedIter = 3;
    p.description = "Karisik sehir (varsayilan)";
    return p;
}

// ─────────────────────────────────────────────────────────────────────────────
//  ANA
// ─────────────────────────────────────────────────────────────────────────────
int main(int argc, char* argv[]) {
    int    iterations    = -1;
    double snapThreshold = 8.0;
    float  oneWayProb    = 0.20f;
    int    seed          = 42;
    std::string preset   = "default";

    if (argc >= 2) iterations    = std::stoi(argv[1]);
    if (argc >= 3) snapThreshold = std::stod(argv[2]);
    if (argc >= 4) oneWayProb    = std::stof(argv[3]);
    if (argc >= 5) seed          = std::stoi(argv[4]);
    if (argc >= 6) preset        = argv[5];

    CityPreset cfg = getPreset(preset);
    if (iterations == -1) iterations = cfg.recommendedIter;
    if (iterations > 5) iterations = 5;
    if (iterations < 1) iterations = 1;

    // Debug bilgisi stderr'a (stdout temiz kalsin)
    std::cerr << "[ENGINE] Preset: " << preset
              << " (" << cfg.description << ")\n";
    std::cerr << "[ENGINE] Aksiyom: " << cfg.axiom
              << ", iter: " << iterations
              << ", acistep: " << cfg.angleStep << " derece\n";
    std::cerr << "[ENGINE] Kurallar:\n";

    // C++14 uyumlu — structured binding YOK
    for (std::map<char, std::string>::iterator it = cfg.rules.begin();
         it != cfg.rules.end(); ++it) {
        std::cerr << "[ENGINE]   " << it->first
                  << " -> " << it->second << "\n";
    }

    // L-System'i kur
    LSystem cityGenerator(cfg.axiom);
    for (std::map<char, std::string>::iterator it = cfg.rules.begin();
         it != cfg.rules.end(); ++it) {
        cityGenerator.addRule(it->first, it->second);
    }

    std::string finalGraphString = cityGenerator.generate(iterations);
    std::cerr << "[ENGINE] Uretilen dizgi uzunlugu: "
              << finalGraphString.size() << " karakter\n";

    // TOON'a donustur
    std::string toonData = ToonExporter::generateMockToonData(
        finalGraphString,
        snapThreshold,
        oneWayProb,
        seed,
        cfg.angleStep
    );

    std::cout << toonData;

    std::ofstream outFile("output.toon");
    if (outFile.is_open()) {
        outFile << toonData;
        outFile.close();
    }

    return 0;
}