# backend/api_server.py
"""
BDO Optima — REST API

Pipeline akışı (ADIM 1: yönlü graf desteği eklendi):

    [Kaynak]                              [Pipeline]
    ────────                              ──────────
    L-System (C++ engine)        ┐
                                 ├──→ TOON parse (yön bilgisiyle)
    OpenStreetMap (Overpass)     ┘
                                       ↓
                                 SIM #1 (orijinal yükler)
                                       ↓
                                 Gemini (darboğaz raporuyla)
                                       ↓
                                 SIM #2 (yeni yükler, eğer bypass uygulandıysa)
                                       ↓
                                 SUMO XML üretimi
                                       ↓
                                 JSON response

YENİ: Yönlü graf desteği. C++ artık her edge'e direction (BOTH/FWD/BWD)
ekliyor, simulator bunu Dijkstra ağırlıklarına yansıtıyor.
"""
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
import subprocess
import os

from backend.gemini_client import optimize_network_with_simulation
from backend.sumo_converter import export_to_sumo_xml
from backend.osm_fetcher import fetch_osm_network
from backend.traffic_sim import (
    parse_toon,
    simulate_traffic,
    compare_simulations,
    format_bottleneck_summary_for_llm,
)


app = FastAPI(title="BDO Optima API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─────────────────────────────────────────────────────────────────────────────
#  YARDIMCILAR
# ─────────────────────────────────────────────────────────────────────────────
def _validate_llm_edge(llm_edge: str, node_ids: set, edge_pairs: set) -> bool:
    """
    Gemini'nin önerdiği bypass'ı doğrula:
      - Format: EDGE;<id>;<from>;<to>[;name][;direction]
      - from ≠ to
      - Her iki düğüm ağda var
      - Aynı (from,to) çifti zaten yok (yönsüz karşılaştırma — duplicate olmaz)
    """
    if not llm_edge or not llm_edge.startswith("EDGE;"):
        return False
    parts = llm_edge.split(";")
    if len(parts) < 4:
        return False
    a, b = parts[2], parts[3]
    if a == b or a not in node_ids or b not in node_ids:
        return False
    if frozenset([a, b]) in edge_pairs:
        return False
    return True


def _stats_summary(toon_data: str) -> dict:
    """Hızlı özet (parse maliyeti olmadan, satır sayma)."""
    nc = sum(1 for ln in toon_data.split("\n") if ln.startswith("NODE;"))
    ec = sum(1 for ln in toon_data.split("\n") if ln.startswith("EDGE;"))
    return {
        "nodes": nc,
        "edges": ec,
        "avg_degree": round((2 * ec) / nc, 2) if nc else 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ANA PİPELİNE
# ─────────────────────────────────────────────────────────────────────────────
# backend/api_server.py — _run_pipeline güncellemesi

from backend.sumo_runner import run_sumo_simulation, compare_sumo_metrics  # YENİ

def _run_pipeline(
    raw_toon_data: str,
    source_label: str,
    num_trips: int = 300,
    seed: int = 42,
    run_traci: bool = True,            # YENİ: TraCI koşulsun mu
    traci_duration: int = 150,         # YENİ: TraCI sim süresi (saniye)
    traci_compare: bool = True,        # YENİ: önce+sonra mi sadece sonra mı
) -> dict:
    """TOON → SIM #1 → Gemini → SIM #2 → SUMO + TraCI → JSON."""

    # ── Parse ────────────────────────────────────────────────────────────
    nodes, edges = parse_toon(raw_toon_data)
    print(f"[PARSE] {len(nodes)} düğüm, {len(edges)} kenar")

    if edges:
        dirs = {"BOTH": 0, "FWD": 0, "BWD": 0}
        for e in edges:
            dirs[e.get("direction", "BOTH")] += 1
        print(f"[PARSE] Yön: BOTH={dirs['BOTH']}, FWD={dirs['FWD']}, BWD={dirs['BWD']}")

    # ── SIM #1: Python (darboğaz bul) ───────────────────────────────────
    sim_before = None
    if len(nodes) >= 2 and len(edges) >= 1:
        print("[2a/5] Python simülasyonu (orijinal)...")
        sim_before = simulate_traffic(nodes, edges, num_trips=num_trips, seed=seed)
        print(f"[SIM-PY] Önce: max={sim_before['max_load']}, "
              f"avg={sim_before['avg_load']}, "
              f"başarılı={sim_before['total_trips']}/{sim_before['demand_count']}")

    # ── Gemini ──────────────────────────────────────────────────────────
    llm_edge = ""
    if sim_before is not None:
        print("[2b/5] Gemini AI darboğaz analizi...")
        bottleneck_text = format_bottleneck_summary_for_llm(sim_before)
        try:
            llm_edge = optimize_network_with_simulation(
                raw_toon_data, bottleneck_text
            )
        except Exception as e:
            print(f"[AI] Hata: {e}")
            llm_edge = "HATA"

    # ── Bypass uygula ────────────────────────────────────────────────────
    final_toon_data = raw_toon_data
    ai_bypass_added = False
    ai_bypass_edges = []

    if sim_before is not None and llm_edge and llm_edge != "HATA":
        node_ids = {n["id"] for n in nodes}
        edge_pairs = {frozenset([e["from"], e["to"]]) for e in edges}

        # Gemini'den gelen satırları tek tek işle
        llm_lines = [ln.strip() for ln in llm_edge.split("\n") if ln.strip()]
        
        bypass_count = 0
        inserted_edges_text = ""

        for line in llm_lines:
            if line.startswith("EDGE;") and _validate_llm_edge(line, node_ids, edge_pairs):
                parts = line.split(";")
                a, b = parts[2], parts[3]
                
                bypass_count += 1
                # Formatı ve isimleri standartlaştır
                normalized = f"EDGE;LLM_BYPASS_{bypass_count};{a};{b};;BOTH"
                print(f"✅ [AI] Öneri {bypass_count}: {normalized}")
                
                inserted_edges_text += f"{normalized}\n"
                edge_pairs.add(frozenset([a, b]))  # Aynı düğümlere 2. kez bypass eklenmesin
                ai_bypass_edges.append({"from": a, "to": b, "direction": "BOTH"})
                ai_bypass_added = True
                
                if bypass_count >= 3:  # En fazla 3 bypass ekle
                    break
            else:
                if line and line != "HATA":
                    print(f"⚠️  [AI] Geçersiz öneri atlandı: {line[:80]}")

        if ai_bypass_added:
            final_toon_data = raw_toon_data.replace(
                "NETWORK_END", f"{inserted_edges_text}NETWORK_END"
            )

    # ── SIM #2: Python (bypass'lı) ──────────────────────────────────────
    sim_after = None
    comparison = None
    if ai_bypass_added and sim_before is not None:
        print("[2c/5] Python simülasyonu (bypass'lı)...")
        nodes_after, edges_after = parse_toon(final_toon_data)
        sim_after = simulate_traffic(nodes_after, edges_after,
                                     num_trips=num_trips, seed=seed)
        comparison = compare_simulations(sim_before, sim_after)
        print(f"[SIM-PY] Sonra: max={sim_after['max_load']}, "
              f"iyileşme %{comparison['max_load_improvement_pct']}")

    # ── SUMO + TraCI ─────────────────────────────────────────────────────
    sumo_metrics = None
    sumo_comparison = None
    sumo_folders = {}

    if run_traci:
        print("[3/5] SUMO ağ dosyaları üretiliyor...")

        # BEFORE klasörü (orijinal ağ)
        before_folder = "sumo_files/before"
        sumo_before_result = export_to_sumo_xml(
            raw_toon_data,
            output_folder=before_folder,
            num_trips=num_trips,
            seed=seed,
            sim_duration=traci_duration,
        )
        sumo_folders["before"] = before_folder

        # AFTER klasörü (bypass'lı)
        after_folder = "sumo_files/after"
        if ai_bypass_added:
            sumo_after_result = export_to_sumo_xml(
                final_toon_data,
                output_folder=after_folder,
                num_trips=num_trips,
                seed=seed,
                sim_duration=traci_duration,
            )
            sumo_folders["after"] = after_folder
        else:
            sumo_after_result = None

        # ── TraCI ölçümleri ─────────────────────────────────────────────
        print(f"[4/5] TraCI ölçümü (BEFORE, ~{traci_duration}s)...")
        traci_before = None
        traci_after = None

        if sumo_before_result.get("net_xml") and sumo_before_result.get("sumocfg"):
            cfg_path = os.path.join(before_folder, "network.sumocfg")
            traci_before = run_sumo_simulation(cfg_path, sim_duration=traci_duration)
            if traci_before["ok"]:
                print(f"[TraCI-BEFORE] avg_wait={traci_before['avg_wait_time']}s, "
                      f"avg_speed={traci_before['avg_speed']} km/h, "
                      f"completed={traci_before['completed_vehicles']}/{traci_before['total_vehicles']}")
            else:
                print(f"[TraCI-BEFORE] HATA: {traci_before['error']}")

        if ai_bypass_added and traci_compare and sumo_after_result and sumo_after_result.get("net_xml"):
            print(f"[5/5] TraCI ölçümü (AFTER, ~{traci_duration}s)...")
            cfg_path = os.path.join(after_folder, "network.sumocfg")
            traci_after = run_sumo_simulation(cfg_path, sim_duration=traci_duration)
            if traci_after["ok"]:
                print(f"[TraCI-AFTER]  avg_wait={traci_after['avg_wait_time']}s, "
                      f"avg_speed={traci_after['avg_speed']} km/h, "
                      f"completed={traci_after['completed_vehicles']}/{traci_after['total_vehicles']}")

                if traci_before and traci_before["ok"]:
                    sumo_comparison = compare_sumo_metrics(traci_before, traci_after)
                    print(f"[TraCI] İyileşme:")
                    print(f"  - Bekleme süresi: %{sumo_comparison['avg_wait_improvement_pct']}")
                    print(f"  - Ort hız:        %{sumo_comparison['avg_speed_improvement_pct']}")
                    print(f"  - CO2:            %{sumo_comparison['co2_improvement_pct']}")

        sumo_metrics = {
            "before": traci_before,
            "after":  traci_after,
        }
    else:
        # TraCI istenmedi → eski davranış
        print("[3/5] SUMO ağ dosyaları üretiliyor (TraCI atlandı)...")
        sumo_before_result = export_to_sumo_xml(
            final_toon_data, num_trips=num_trips, seed=seed
        )

    # ── Frontend için Python sim payload ────────────────────────────────
    sim_payload = None
    if sim_before is not None:
        edge_loads_merged = {}
        for eid, load in sim_before["edge_loads"].items():
            edge_loads_merged[eid] = {"before": load, "after": load}
        if sim_after is not None:
            for eid, load in sim_after["edge_loads"].items():
                if eid in edge_loads_merged:
                    edge_loads_merged[eid]["after"] = load
                else:
                    edge_loads_merged[eid] = {"before": 0, "after": load}

        sim_payload = {
            "before": {
                "max_load":             sim_before["max_load"],
                "avg_load":             sim_before["avg_load"],
                "avg_path_length":      sim_before["avg_path_length"],
                "demand_count":         sim_before["demand_count"],
                "total_trips":          sim_before["total_trips"],
                "failed_trips":         sim_before["failed_trips"],
                "top_bottleneck_edges": sim_before["top_bottleneck_edges"],
                "top_bottleneck_nodes": sim_before["top_bottleneck_nodes"],
            },
            "after": None if sim_after is None else {
                "max_load":             sim_after["max_load"],
                "avg_load":             sim_after["avg_load"],
                "avg_path_length":      sim_after["avg_path_length"],
                "total_trips":          sim_after["total_trips"],
                "failed_trips":         sim_after["failed_trips"],
                "top_bottleneck_edges": sim_after["top_bottleneck_edges"],
                "top_bottleneck_nodes": sim_after["top_bottleneck_nodes"],
            },
            "comparison":         comparison,
            "edge_loads":         edge_loads_merged,
            "node_betweenness":   sim_before["node_betweenness"],
            "ai_bypass_edges":     ai_bypass_edges,
            "seed":               seed,
            "num_trips":          num_trips,
        }

    stats = _stats_summary(final_toon_data)
    stats["ai_bypass_added"] = ai_bypass_added

    return {
        "status":     "success",
        "source":     source_label,
        "data":       final_toon_data,
        "stats":      stats,
        "simulation": sim_payload,
        "sumo_metrics": sumo_metrics,       # YENİ: TraCI ölçümleri
        "sumo_comparison": sumo_comparison, # YENİ: önce/sonra karşılaştırma
        "sumo_folders":    sumo_folders,    # YENİ: kullanıcı sumo-gui açabilsin
    }


# ─────────────────────────────────────────────────────────────────────────────
#  ENDPOINT: L-SYSTEM
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/generate")
def generate_network(
    iterations:    int   = 2,
    snapping:      float = 5.0,
    one_way_prob:  float = 0.05,
    direction_seed: int  = 42,
    preset:        str   = "default",
    num_trips:     int   = 800,
    sim_seed:      int   = 42,
    run_traci:     bool  = True,     # YENİ
    traci_duration: int  = 300,      # YENİ (saniye)
    traci_compare:  bool = True,     # YENİ
):
    """
    C++ L-System motoru → TOON (yönlü) → pipeline.

    Parametreler:
      iterations:     L-System tekrar sayısı (1-5)
      snapping:       Düğüm birleştirme eşiği
      one_way_prob:   Tek yön olma olasılığı (0.0-1.0, varsayılan 0.20)
      direction_seed: C++ tarafında yön ataması için seed (deterministik)
      num_trips:      Simülasyondaki araç sayısı
      sim_seed:       Simülasyon RNG seed
    """
    print(f"\n[SİSTEM] L-System: iter={iterations}, snap={snapping}, "
          f"oneway={one_way_prob}, dseed={direction_seed}, "
          f"trips={num_trips}, sseed={sim_seed}")

    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)

    # Hem .exe (Windows) hem de uzantısız (Linux) executable'ı dene
    candidates = [
        os.path.join(project_root, "engine", "engine.exe"),
        os.path.join(project_root, "engine", "engine"),
    ]
    engine_path = next((p for p in candidates if os.path.exists(p)), None)
    if not engine_path:
        raise HTTPException(
            status_code=500,
            detail="engine binary'si bulunamadı (engine.exe veya engine)",
        )

    try:
        print(f"[1/4] C++ motoru → {engine_path}")
        result = subprocess.run(
            [
                engine_path,
                str(iterations),
                str(snapping),
                str(one_way_prob),
                str(direction_seed),
                str(preset),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        return _run_pipeline(
            result.stdout,
            source_label="L-System",
            num_trips=num_trips,
            seed=sim_seed,
            run_traci=run_traci,
            traci_duration=traci_duration,
            traci_compare=traci_compare,
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="C++ motoru timeout")
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"engine hatası: {e.stderr}")
    except Exception as e:
        err = str(e)
        if "429" in err:
            return {"status": "error",
                    "message": "Gemini hız sınırı doldu. Biraz bekleyip tekrar deneyin."}
        print(f"❌ [HATA]: {err}")
        raise HTTPException(status_code=500, detail=err)


# ─────────────────────────────────────────────────────────────────────────────
#  ENDPOINT: OSM
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/generate_osm")
def generate_osm_network(
    lat:       float = 39.7504,
    lon:       float = 30.4833,
    radius:    int   = 500,
    num_trips: int   = 300,
    sim_seed:  int   = 42,
    run_traci:     bool = True,        # ← YENİ
    traci_duration: int = 300,         # ← YENİ
    traci_compare:  bool = True,       # ← YENİ
):
    print(f"\n[SİSTEM] OSM: ({lat}, {lon}), r={radius}m, trips={num_trips}, "
          f"traci={run_traci}")

    if radius > 2000:
        raise HTTPException(status_code=400, detail="Yarıçap 2000m'den büyük olamaz")
    if radius < 100:
        raise HTTPException(status_code=400, detail="Yarıçap en az 100m olmalı")

    try:
        print("[1/5] OpenStreetMap'ten yol ağı çekiliyor...")
        raw_toon_data = fetch_osm_network(lat=lat, lon=lon, radius_m=radius)
        return _run_pipeline(
            raw_toon_data,
            source_label="OSM",
            num_trips=num_trips,
            seed=sim_seed,
            run_traci=run_traci,
            traci_duration=traci_duration,
            traci_compare=traci_compare,
        )

    except RuntimeError as e:
        print(f"❌ [OSM]: {e}")
        return {"status": "error", "message": str(e)}
    except Exception as e:
        err = str(e)
        if "429" in err:
            return {"status": "error",
                    "message": "Gemini hız sınırı doldu. Biraz bekleyip tekrar deneyin."}
        print(f"❌ [HATA]: {err}")
        raise HTTPException(status_code=500, detail=err)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)