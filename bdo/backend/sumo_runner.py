# backend/sumo_runner.py
"""
SUMO mikro-simülasyon koşturucu (TraCI üzerinden).

Verilen bir .sumocfg'yi yükler, sim_duration saniye boyunca koşturur,
her step'te araç metriklerini toplar. Sonunda özet dict döndürür.

Kullanım:
    metrics = run_sumo_simulation("sumo_files/before/network.sumocfg")
    # → {"avg_wait": 28.4, "avg_speed": 22.1, ...}
"""
from __future__ import annotations
import os
import sys
import traceback


# ── SUMO_HOME kontrolü ────────────────────────────────────────────────────────
def _ensure_sumo_home():
    """TraCI'ı import edebilmek için $SUMO_HOME/tools PATH'de olmalı."""
    sumo_home = os.environ.get("SUMO_HOME")
    if not sumo_home:
        # Windows'ta tipik kurulum yerleri (sumo_converter'la tutarlı)
        for p in [r"D:\Eclipse", r"C:\Program Files (x86)\Eclipse\Sumo",
                  r"C:\Program Files\Eclipse\Sumo", r"C:\sumo"]:
            if os.path.isdir(p):
                sumo_home = p
                break

    if sumo_home:
        tools = os.path.join(sumo_home, "tools")
        if tools not in sys.path:
            sys.path.append(tools)
        return sumo_home

    raise RuntimeError(
        "SUMO_HOME ortam değişkeni tanımlı değil ve standart yerlerde bulunamadı. "
        "Windows: setx SUMO_HOME \"D:\\Eclipse\""
    )


# ── Ana fonksiyon ─────────────────────────────────────────────────────────────
def run_sumo_simulation(
    sumocfg_path: str,
    sim_duration: int = 600,
    use_gui: bool = False,
    step_length: float = 1.0,
) -> dict:
    """
    SUMO simülasyonunu koştur, metrikleri topla.

    Args:
        sumocfg_path: .sumocfg dosyasının yolu
        sim_duration: max step sayısı (saniye). Önce trafik bitse de durmaz —
                      tamamlanmamış yolculukları "stuck" sayar.
        use_gui:      True → sumo-gui (görsel), False → sumo (headless, hızlı)
        step_length:  her step kaç saniye (1.0 = gerçek zaman)

    Returns:
        {
          "ok": True,
          "total_vehicles":      300,
          "completed_vehicles":  287,    # hedefe ulaşan
          "stuck_vehicles":      13,     # hala yolda
          "avg_wait_time":       28.4,   # saniye (kırmızı ışıkta + yolda)
          "avg_travel_time":     145.2,  # saniye
          "avg_speed":           22.1,   # km/h
          "total_steps":         600,
          "total_co2":           4521.3, # mg (SUMO emission)
          "max_concurrent":      87,     # aynı anda yolda en çok kaç araç
          "error":               None,
        }

        Hata olursa: {"ok": False, "error": "..."}
    """
    result = {
        "ok": False, "total_vehicles": 0, "completed_vehicles": 0,
        "stuck_vehicles": 0, "avg_wait_time": 0.0, "avg_travel_time": 0.0,
        "avg_speed": 0.0, "total_steps": 0, "total_co2": 0.0,
        "max_concurrent": 0, "error": None,
    }

    try:
        sumo_home = _ensure_sumo_home()
        import traci

        # Binary seç
        binary_name = "sumo-gui" if use_gui else "sumo"
        binary_path = os.path.join(sumo_home, "bin", binary_name)
        if os.name == "nt":
            binary_path += ".exe"
        if not os.path.isfile(binary_path):
            # PATH'te bul
            import shutil
            binary_path = shutil.which(binary_name)
            if not binary_path:
                raise RuntimeError(f"{binary_name} bulunamadı")

        # Komut
        cmd = [
            binary_path,
            "-c", sumocfg_path,
            "--step-length", str(step_length),
            "--no-warnings",
            "--no-step-log",
            "--duration-log.disable",
        ]

        # ── Simülasyonu başlat ──────────────────────────────────────────────
        traci.start(cmd)

        # Metrik toplama
        per_vehicle_data = {}    # {veh_id: {"depart": t, "arrive": t, ...}}
        speeds = []              # her step'teki araç hızları
        co2_emissions = []
        max_concurrent = 0
        step = 0

        while step < sim_duration and traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()
            step += 1

            # Anlık araç listesi
            active = traci.vehicle.getIDList()
            if len(active) > max_concurrent:
                max_concurrent = len(active)

            # Her aktif araç için metrik
            for vid in active:
                if vid not in per_vehicle_data:
                    per_vehicle_data[vid] = {
                        "depart": traci.vehicle.getDeparture(vid),
                        "arrive": None,
                        "total_wait": 0.0,
                    }
                # Bekleme süresi birikimli
                per_vehicle_data[vid]["total_wait"] = \
                    traci.vehicle.getAccumulatedWaitingTime(vid)

                speeds.append(traci.vehicle.getSpeed(vid))
                co2_emissions.append(traci.vehicle.getCO2Emission(vid))

            # Bu step'te varan araçları işaretle
            arrived = traci.simulation.getArrivedIDList()
            for vid in arrived:
                if vid in per_vehicle_data:
                    per_vehicle_data[vid]["arrive"] = step

        traci.close()

        # ── Özet metrikler ──────────────────────────────────────────────────
        total = len(per_vehicle_data)
        completed = sum(1 for v in per_vehicle_data.values()
                        if v["arrive"] is not None)
        stuck = total - completed

        avg_wait = (sum(v["total_wait"] for v in per_vehicle_data.values()) / total
                    if total else 0.0)

        travel_times = [v["arrive"] - v["depart"]
                        for v in per_vehicle_data.values()
                        if v["arrive"] is not None]
        avg_travel = sum(travel_times) / len(travel_times) if travel_times else 0.0

        avg_speed_ms = sum(speeds) / len(speeds) if speeds else 0.0
        avg_speed_kmh = avg_speed_ms * 3.6

        total_co2 = sum(co2_emissions)  # mg

        result.update({
            "ok": True,
            "total_vehicles": total,
            "completed_vehicles": completed,
            "stuck_vehicles": stuck,
            "avg_wait_time": round(avg_wait, 2),
            "avg_travel_time": round(avg_travel, 2),
            "avg_speed": round(avg_speed_kmh, 2),
            "total_steps": step,
            "total_co2": round(total_co2, 2),
            "max_concurrent": max_concurrent,
        })

    except Exception as e:
        try:
            import traci
            traci.close()
        except Exception:
            pass
        result["error"] = f"{type(e).__name__}: {e}"
        print(f"[SUMO-RUNNER] ❌ {result['error']}")
        traceback.print_exc()

    return result


# ── Karşılaştırma yardımcısı ─────────────────────────────────────────────────
def compare_sumo_metrics(before: dict, after: dict) -> dict:
    """İki SUMO çalışmasını karşılaştır → yüzde değişim."""
    def pct_drop(old, new):
        if not old or old <= 0:
            return 0.0
        return round((old - new) / old * 100, 2)

    return {
        "avg_wait_before":          before["avg_wait_time"],
        "avg_wait_after":           after["avg_wait_time"],
        "avg_wait_improvement_pct": pct_drop(before["avg_wait_time"],
                                             after["avg_wait_time"]),

        "avg_travel_before":          before["avg_travel_time"],
        "avg_travel_after":           after["avg_travel_time"],
        "avg_travel_improvement_pct": pct_drop(before["avg_travel_time"],
                                               after["avg_travel_time"]),

        "avg_speed_before":          before["avg_speed"],
        "avg_speed_after":           after["avg_speed"],
        "avg_speed_improvement_pct": pct_drop(after["avg_speed"],
                                              before["avg_speed"]),  # ters: hız artışı iyi

        "completed_before":          before["completed_vehicles"],
        "completed_after":           after["completed_vehicles"],

        "co2_before":                before["total_co2"],
        "co2_after":                 after["total_co2"],
        "co2_improvement_pct":       pct_drop(before["total_co2"],
                                              after["total_co2"]),
    }


# ── Test ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default="sumo_files/network.sumocfg")
    ap.add_argument("--gui", action="store_true")
    ap.add_argument("--duration", type=int, default=600)
    args = ap.parse_args()

    print(f"Koşturuluyor: {args.cfg}")
    m = run_sumo_simulation(args.cfg, args.duration, args.gui)
    print("\n— METRİKLER —")
    for k, v in m.items():
        print(f"  {k:25s} = {v}")