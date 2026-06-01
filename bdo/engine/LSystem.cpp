#include "LSystem.h"

LSystem::LSystem(std::string startAxiom) {
    axiom = startAxiom;
    currentString = startAxiom;
}

void LSystem::addRule(char predecessor, std::string successor) {
    productionRules[predecessor] = successor;
}

std::string LSystem::generate(int iterations) {
    for (int i = 0; i < iterations; ++i) {
        std::string nextString = "";
        
        // Dizgideki her bir karakteri (token) kontrol et
        for (char c : currentString) {
            // Eğer karakterin bir üretim kuralı varsa, o kuralı uygula
            if (productionRules.find(c) != productionRules.end()) {
                nextString += productionRules[c];
            } else {
                // Kural yoksa karakteri olduğu gibi bırak (örn: +, -, [, ])
                nextString += c;
            }
        }
        currentString = nextString;
    }
    return currentString;
}

std::string LSystem::getCurrentString() {
    return currentString;
}