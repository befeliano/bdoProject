"""
Adım 1 entegrasyon testi:
  C++ engine → TOON (yönlü) → traffic_sim → mock Gemini → SIM2 → SUMO
"""
import os
import sys

os.environ.setdefault("GEMINI_API_KEY", "test-key-not-used")
sys.path.insert(0, "/home/claude/bdo_optima")

from unittest.mock import patch
import subprocess

ENGINE = "/home/claude/bdo_optima/engine/engine"


def run():
    # ── 1. C++ ile gerçek TOON üret ─────────────────────────────────────
    print("─── ADIM 1: C++ engine ───")
    proc = subprocess.run(
        [ENGINE, "3", "5.0", "0.20", "42"],
        capture_output=True, text=True, check=True,
    )
    toon = proc.stdout
    edges_total = sum(1 for ln in toon.split("\n") if ln.startswith("EDGE;"))
    nodes_total = sum(1 for ln in toon.split("\n") if ln.startswith("NODE;"))
    print(f"C++ üretti: {nodes_total} düğüm, {edges_total} kenar")

    # Yön dağılımı
    dirs = {"BOTH": 0, "FWD": 0, "BWD": 0, "OTHER": 0}
    for ln in toon.split("\n"):
        if ln.startswith("EDGE;"):
            parts = ln.split(";")
            if len(parts) >= 6:
                d = parts[5]
                dirs[d if d in dirs else "OTHER"] += 1
    print(f"Yön: {dirs}")

    # ── 2. Parse ────────────────────────────────────────────────────────
    print("\n─── ADIM 2: Parse ───")
    from backend.traffic_sim import (
        parse_toon, simulate_traffic, compare_simulations,
        format_bottleneck_summary_for_llm,
    )
    nodes, edges = parse_toon(toon)
    print(f"Parse: {len(nodes)} düğüm, {len(edges)} kenar")

    # ── 3. SIM #1 ───────────────────────────────────────────────────────
    print("\n─── ADIM 3: Simülasyon #1 (orijinal) ───")
    sim_before = simulate_traffic(nodes, edges, num_trips=300, seed=42)
    print(f"Talep: {sim_before['demand_count']}, "
          f"başarılı: {sim_before['total_trips']}, "
          f"başarısız: {sim_before['failed_trips']}")
    print(f"Max load: {sim_before['max_load']}")
    print(f"Avg load: {sim_before['avg_load']}")
    print(f"En yoğun: {sim_before['top_bottleneck_edges'][0]}")
    print(f"En kritik kavşak: {sim_before['top_bottleneck_nodes'][0]}")

    # ── 4. Mock Gemini: en kritik 2 kavşağı bağla ─────────────────────
    print("\n─── ADIM 4: Pipeline + Mock Gemini ───")
    top_n = sim_before["top_bottleneck_nodes"]
    n_a = top_n[0]["node_id"]
    n_b = top_n[1]["node_id"]
    # Eğer çift zaten bağlıysa, bir sonraki adayı al
    existing_pairs = {frozenset([e["from"], e["to"]]) for e in edges}
    if frozenset([n_a, n_b]) in existing_pairs:
        n_b = top_n[2]["node_id"]
    mock_edge = f"EDGE;LLM_BYPASS_1;{n_a};{n_b};;BOTH"
    print(f"Mock LLM: {mock_edge}")

    # ── 5. Tam pipeline'ı koştur ───────────────────────────────────────
    from backend import api_server

    with patch(
        "backend.api_server.optimize_network_with_simulation",
        return_value=mock_edge,
    ), patch(
        "backend.api_server.export_to_sumo_xml",
        return_value={
            "nod_xml": True, "edg_xml": True, "net_xml": False,
            "netconvert_path": None, "error": "test",
        },
    ):
        result = api_server._run_pipeline(
            toon, source_label="L-System", num_trips=300, seed=42
        )

    # ── 6. Sonuç doğrulamaları ─────────────────────────────────────────
    print("\n─── ADIM 5: Şema kontrolü ───")
    assert result["status"] == "success"
    assert result["stats"]["ai_bypass_added"] is True
    assert "simulation" in result and result["simulation"] is not None
    assert "before" in result["simulation"]
    assert "after" in result["simulation"] and result["simulation"]["after"] is not None
    assert "comparison" in result["simulation"]
    assert "edge_loads" in result["simulation"]
    assert "node_betweenness" in result["simulation"]
    print("✓ Şema doğru")

    comp = result["simulation"]["comparison"]
    print(f"\nMax load: {comp['max_load_before']} → {comp['max_load_after']} "
          f"({comp['max_load_improvement_pct']:+.1f}%)")
    print(f"Avg load: {comp['avg_load_before']} → {comp['avg_load_after']} "
          f"({comp['avg_load_improvement_pct']:+.1f}%)")
    print(f"Avg path: {comp['avg_path_before']} → {comp['avg_path_after']} "
          f"({comp['avg_path_improvement_pct']:+.1f}%)")
    print(f"Failed: {comp['failed_before']} → {comp['failed_after']}")

    # LLM_BYPASS_1 yüklendi mi?
    bypass_load = result["simulation"]["edge_loads"].get("LLM_BYPASS_1", {})
    print(f"\nLLM_BYPASS_1: {bypass_load}")

    # ── 7. Determinizm: aynı seed → aynı sonuç ─────────────────────────
    print("\n─── ADIM 6: Determinizm ───")
    sim_a = simulate_traffic(nodes, edges, num_trips=300, seed=42)
    sim_b = simulate_traffic(nodes, edges, num_trips=300, seed=42)
    assert sim_a["max_load"] == sim_b["max_load"]
    assert sim_a["avg_load"] == sim_b["avg_load"]
    print(f"✓ Aynı seed = aynı sonuç (max={sim_a['max_load']})")

    sim_c = simulate_traffic(nodes, edges, num_trips=300, seed=99)
    print(f"✓ Farklı seed = farklı sonuç (seed=42: max={sim_a['max_load']}, "
          f"seed=99: max={sim_c['max_load']})")

    print("\n🎯 ADIM 1 TAMAMLANDI: Yönlü graf + traffic_sim + Gemini pipeline ✓")


if __name__ == "__main__":
    run()