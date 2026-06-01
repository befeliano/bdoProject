cmake_minimum_required(VERSION 3.10)
project(engine)

set(CMAKE_CXX_STANDARD 17)              # ← YENİ
set(CMAKE_CXX_STANDARD_REQUIRED ON)     # ← YENİ

add_executable(engine main.cpp LSystem.cpp ToonExporter.cpp)