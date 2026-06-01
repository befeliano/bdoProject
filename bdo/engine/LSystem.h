#ifndef LSYSTEM_H
#define LSYSTEM_H

#include <iostream>
#include <string>
#include <map>

class LSystem {
private:
    std::string axiom;                           // Başlangıç dizgisi
    std::string currentString;                   // Üretilen anlık dizgi
    std::map<char, std::string> productionRules; // CFG Üretim Kuralları

public:
    LSystem(std::string startAxiom);
    
    // Kural ekleme fonksiyonu (Örn: F -> F[+F]-F)
    void addRule(char predecessor, std::string successor);
    
    // N jenerasyon boyunca türetme işlemini yapar
    std::string generate(int iterations);
    
    // Anlık dizgiyi döndürür
    std::string getCurrentString();
};

#endif