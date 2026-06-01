#include "ToonExporter.h"
#include <sstream>
#include <stack>
#include <cmath>
#include <vector>
#include <iostream>
#include <random>

struct TurtleState { double x, y, angle; std::string nodeId; };
struct NodeData    { std::string id; double x, y; };

std::string ToonExporter::generateMockToonData(
    const std::string& lSystemString,
    double snapThreshold,
    float  oneWayProb,
    int    seed,
    double angleStep                  // ← YENİ parametre
) {
    std::stringstream toon;
    std::stack<TurtleState> stateStack;
    std::vector<NodeData> nodes;
    std::vector<std::string> edges;

    // Deterministik rastgelelik motoru — aynı seed = aynı sonuç
    std::mt19937 gen(seed);
    std::uniform_real_distribution<> dis(0.0, 1.0);

    double currentX = 0.0, currentY = 0.0, currentAngle = 0.0;
    int nodeCounter = 1, edgeCounter = 1;

    std::string currentNode = "N" + std::to_string(nodeCounter++);
    nodes.push_back({currentNode, currentX, currentY});

    const double distance = 30.0;            // segment uzunluğu (metre)
    const double PI       = 3.141592653589793;

    // stderr'a debug çıktısı (stdout'u Python parse ediyor, kirletme)
    std::cerr << "[EXPORTER] angleStep=" << angleStep
              << "°, snap=" << snapThreshold
              << "m, distance=" << distance << "m\n";

    for (char c : lSystemString) {
        if (c == 'F') {
            // Bir segment ileri git
            double rad  = currentAngle * PI / 180.0;
            double newX = currentX + distance * std::cos(rad);
            double newY = currentY + distance * std::sin(rad);

            // Yuvarla — float gürültüsünü engelle (snapping tutarlılığı)
            newX = std::round(newX * 100.0) / 100.0;
            newY = std::round(newY * 100.0) / 100.0;

            // Bu noktaya yakın bir düğüm var mı? (kavşak birleştirme)
            // Önemli: currentNode'u atla, yoksa kısa segmentler kendi
            // başlangıçlarına snap olabiliyor.
            std::string targetNode = "";
            for (const auto& n : nodes) {
                if (n.id == currentNode) continue;            // kendin değil
                double dist = std::hypot(newX - n.x, newY - n.y);
                if (dist < snapThreshold) {
                    targetNode = n.id;
                    break;
                }
            }

            // Yakın düğüm yok → yeni düğüm oluştur
            if (targetNode.empty()) {
                targetNode = "N" + std::to_string(nodeCounter++);
                nodes.push_back({targetNode, newX, newY});
            }

            // Kenar oluştur (self-loop değilse)
            if (currentNode != targetNode) {
                // 1. YÖN — oneWayProb olasılıkla tek yön
                std::string direction = "BOTH";
                float r_dir = dis(gen);
                if (r_dir < oneWayProb) {
                    direction = (dis(gen) < 0.5) ? "FWD" : "BWD";
                }

                // 2. HIZ — yol tipi dağılımı
                //   %15 ana bulvar (70 km/h)
                //   %30 bağlantı yolu (50 km/h)
                //   %55 ara sokak (30 km/h)
                int speed = 30;
                float r_speed = dis(gen);
                if      (r_speed < 0.15) speed = 70;
                else if (r_speed < 0.45) speed = 50;

                // Format: EDGE;id;from;to;name;direction;speed
                edges.push_back(
                    "EDGE;E" + std::to_string(edgeCounter++) +
                    ";" + currentNode +
                    ";" + targetNode +
                    ";;" + direction +
                    ";" + std::to_string(speed)
                );
            }

            currentX    = newX;
            currentY    = newY;
            currentNode = targetNode;
        }
        else if (c == '+') {
            currentAngle -= angleStep;
        }
        else if (c == '-') {
            currentAngle += angleStep;
        }
        else if (c == '[') {
            // Dal başlat — turtle durumunu yığına it
            stateStack.push({currentX, currentY, currentAngle, currentNode});
        }
        else if (c == ']') {
            // Dal bitir — turtle durumunu geri yükle
            if (stateStack.empty()) continue;     // bozuk dizgi koruması
            TurtleState prev = stateStack.top();
            stateStack.pop();
            currentX     = prev.x;
            currentY     = prev.y;
            currentAngle = prev.angle;
            currentNode  = prev.nodeId;
        }
        // Bilinmeyen karakterler (X gibi dallanma sembolleri) sessizce
        // atlanır — yalnızca üretim kurallarında genişlerler, çizim yapmazlar
    }

    // ── TOON çıktısı ─────────────────────────────────────────────────────
    toon << "NETWORK_START\n";
    for (const auto& n : nodes) {
        toon << "NODE;" << n.id << ";" << n.x << ";" << n.y << "\n";
    }
    for (const auto& e : edges) {
        toon << e << "\n";
    }
    toon << "NETWORK_END\n";

    std::cerr << "[EXPORTER] Üretildi: " << nodes.size()
              << " düğüm, " << edges.size() << " kenar\n";

    return toon.str();
}