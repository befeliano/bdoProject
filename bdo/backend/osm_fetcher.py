# backend/osm_fetcher.py
"""
OpenStreetMap → BDO Optima TOON dönüştürücü.

Bu modül OpenStreetMap'in Overpass API'si üzerinden:
  • Araç yolları (highway taglı way'ler)
  • Bina poligonları (building taglı way'ler)
  • Trafik sinyali kavşakları (highway=traffic_signals node'ları)
çeker, sadeleştirir ve TOON formatına dönüştürür.

TOON formatı (genişletilmiş, geri uyumlu):
    NODE;id;x;y                                          (eski)
    NODE;id;x;y;lat;lon                                  (OSM)
    NODE;id;x;y;lat;lon;has_signal                       (YENİ — 0 ya da 1)

    EDGE;id;from;to                                      (eski)
    EDGE;id;from;to;name                                 (OSM eski)
    EDGE;id;from;to;name;highway;maxspeed;oneway         (OSM önceki)
    EDGE;id;from;to;name;highway;maxspeed;oneway;lanes;bridge   (YENİ)

Tasarım kararları:
  - Tek Overpass çağrısı (yol + bina + sinyal birlikte) → 3 yerine 1 round-trip
  - 3 mirror denenir (ana sunucu yoğunsa otomatik geçiş)
  - Çekirdek dataclass'larda yapılandırılmış veri
  - Hata mesajları kullanıcı odaklı (frontend bunu doğrudan gösterebilir)
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import requests

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logger.addHandler(h)


# ── Yapılandırma ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OverpassConfig:
    """Overpass API ayarları — tek yerde toplu, kolayca değiştirilebilir."""
    # Ana ve yedek sunucular (sırayla denenir)
    mirrors: tuple = (
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
        "https://overpass.openstreetmap.fr/api/interpreter",
    )
    timeout_seconds: int = 30
    user_agent: str = "BDO-Optima/2.0 (academic project; contact via GitHub)"
    max_retries_per_mirror: int = 1


@dataclass(frozen=True)
class NetworkConfig:
    """Yol ağı çekme parametreleri."""
    # Sadece araç yolları (yaya/bisiklet yolu hariç)
    highway_types: tuple = (
        "motorway", "trunk", "primary", "secondary", "tertiary",
        "unclassified", "residential",
        "motorway_link", "trunk_link", "primary_link",
        "secondary_link", "tertiary_link",
    )
    # OSM'de maxspeed yoksa Türkiye için varsayılanlar (km/h)
    default_maxspeed: dict = field(default_factory=lambda: {
        "motorway": 110,        "motorway_link": 60,
        "trunk":     90,        "trunk_link":    50,
        "primary":   70,        "primary_link":  40,
        "secondary": 50,        "secondary_link": 40,
        "tertiary":  50,        "tertiary_link":  30,
        "residential": 30,      "unclassified":   30,
    })
    # Şerit sayısı yoksa varsayılan (kapasite hesabı için)
    default_lanes: dict = field(default_factory=lambda: {
        "motorway": 3, "trunk": 2, "primary": 2,
        "secondary": 2, "tertiary": 1,
        "motorway_link": 1, "trunk_link": 1, "primary_link": 1,
        "secondary_link": 1, "tertiary_link": 1,
        "residential": 1, "unclassified": 1,
    })
    min_speed_kmh: int = 5
    max_speed_kmh: int = 130


OVERPASS = OverpassConfig()
NETWORK = NetworkConfig()


# ── Veri sınıfları ──────────────────────────────────────────────────────────

@dataclass
class OsmNode:
    """OSM'den gelen tek bir nokta + uygulamamızdaki rolü."""
    osm_id: int
    lat: float
    lon: float
    has_signal: bool = False  # highway=traffic_signals etiketi varsa

    def to_local_xy(self, ref_lat: float, ref_lon: float) -> tuple[float, float]:
        """Equirectangular projeksiyon — küçük alanlar için yeterli."""
        R = 6_371_000
        x = math.radians(self.lon - ref_lon) * R * math.cos(math.radians(ref_lat))
        y = math.radians(self.lat - ref_lat) * R
        return x, y


@dataclass
class OsmWay:
    """OSM'den gelen bir yol (highway) ya da poligon (building)."""
    osm_id: int
    nodes: list[int]
    tags: dict

    @property
    def is_highway(self) -> bool:
        return self.tags.get("highway") in NETWORK.highway_types

    @property
    def is_building(self) -> bool:
        return "building" in self.tags

    @property
    def obstacle_type(self) -> Optional[str]:
        """
        Eğer bu way bir 'engel' poligonsa kategori döner; değilse None.
        Engel = bypass'ın geçemeyeceği tesis/alan.

        Sonuç stringleri istemci tarafında etiket olarak kullanılabilir.
        """
        t = self.tags

        # Sırayla en spesifikten en geneline kontrol
        leisure = t.get("leisure", "")
        if leisure in ("stadium", "pitch", "sports_centre",
                       "swimming_pool", "track"):
            return f"leisure:{leisure}"

        amenity = t.get("amenity", "")
        if amenity in ("hospital", "school", "university",
                       "college", "kindergarten"):
            return f"amenity:{amenity}"

        landuse = t.get("landuse", "")
        if landuse in ("cemetery", "park", "forest", "military",
                       "industrial", "recreation_ground"):
            return f"landuse:{landuse}"

        natural = t.get("natural", "")
        if natural in ("water", "wood"):
            return f"natural:{natural}"

        if t.get("waterway") == "riverbank":
            return "waterway:riverbank"

        return None

    @property
    def is_obstacle(self) -> bool:
        """Bypass'ın üzerinden geçemeyeceği büyük poligon mu?"""
        return self.obstacle_type is not None

    def maxspeed_kmh(self) -> int:
        raw = self.tags.get("maxspeed", "")
        highway = self.tags.get("highway", "")
        return _parse_maxspeed(raw, highway)

    def is_oneway(self) -> bool:
        return _parse_oneway(self.tags.get("oneway", ""), self.tags.get("highway", ""))

    def num_lanes(self) -> int:
        raw = self.tags.get("lanes", "")
        if raw and raw.isdigit():
            return max(1, min(int(raw), 8))
        return NETWORK.default_lanes.get(self.tags.get("highway", ""), 1)

    def is_bridge(self) -> bool:
        return self.tags.get("bridge", "no") not in ("no", "")

    def is_tunnel(self) -> bool:
        return self.tags.get("tunnel", "no") not in ("no", "")


# ── Etiket parser yardımcıları ──────────────────────────────────────────────

def _parse_maxspeed(raw: str, highway: str) -> int:
    """OSM maxspeed string'ini km/h sayıya dönüştür ('50 mph', 'RU:urban', '50' vs.)"""
    if not raw:
        return NETWORK.default_maxspeed.get(highway, 40)

    raw = str(raw).strip().lower()

    # İlk ardışık rakam grubunu yakala
    digits = ""
    for ch in raw:
        if ch.isdigit():
            digits += ch
        elif digits:
            break

    if not digits:
        return NETWORK.default_maxspeed.get(highway, 40)

    val = int(digits)
    if "mph" in raw:
        val = round(val * 1.60934)

    return max(NETWORK.min_speed_kmh, min(val, NETWORK.max_speed_kmh))


def _parse_oneway(raw: str, highway: str) -> bool:
    """OSM oneway etiketini bool'a dönüştür."""
    if not raw:
        # Belirtilmemişse: link ve otoyol genelde tek-yönlüdür
        return highway in ("motorway", "motorway_link", "trunk_link", "primary_link")
    raw = str(raw).strip().lower()
    return raw in ("yes", "true", "1", "-1", "reverse")


# ── Overpass HTTP katmanı (mirror + retry) ──────────────────────────────────

class OverpassError(RuntimeError):
    """Overpass'a özgü hata; kullanıcıya gösterilebilir mesaj taşır."""


def _post_overpass(query: str) -> dict:
    """
    Overpass mirror'larını sırayla dene. İlk başarılı cevap döner.
    Tümü başarısız olursa OverpassError fırlatır.
    """
    last_error = None

    for mirror in OVERPASS.mirrors:
        for attempt in range(OVERPASS.max_retries_per_mirror + 1):
            try:
                logger.info("Overpass deneniyor: %s (deneme %d)", mirror, attempt + 1)
                response = requests.post(
                    mirror,
                    data={"data": query},
                    timeout=OVERPASS.timeout_seconds,
                    headers={"User-Agent": OVERPASS.user_agent},
                )
                response.raise_for_status()
                return response.json()

            except requests.exceptions.Timeout as e:
                last_error = f"Zaman aşımı ({OVERPASS.timeout_seconds}s)"
                logger.warning("Mirror %s zaman aşımı verdi", mirror)

            except requests.exceptions.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                if code == 429:
                    # Rate limited — Retry-After varsa biraz bekleyip dene
                    retry_after = int(e.response.headers.get("Retry-After", "5"))
                    retry_after = min(retry_after, 10)  # makul üst sınır
                    logger.warning("Mirror %s rate-limit verdi, %ds bekleniyor",
                                   mirror, retry_after)
                    last_error = f"Rate limit ({code})"
                    if attempt < OVERPASS.max_retries_per_mirror:
                        time.sleep(retry_after)
                        continue
                else:
                    last_error = f"HTTP {code}"
                    logger.warning("Mirror %s HTTP %d", mirror, code)

            except requests.exceptions.RequestException as e:
                last_error = f"Bağlantı: {e}"
                logger.warning("Mirror %s bağlantı hatası: %s", mirror, e)

            except ValueError as e:
                last_error = f"Geçersiz JSON: {e}"
                logger.warning("Mirror %s bozuk JSON döndürdü", mirror)

            # Bu deneme başarısız — sonraki denemeye/mirror'a geç
            break  # aynı mirror'da retry değil, sonraki mirror'a geç

    raise OverpassError(
        f"Tüm Overpass mirror'ları başarısız oldu. Son hata: {last_error}"
    )


# ── Ana çekme fonksiyonu (yol + bina + sinyal tek seferde) ──────────────────

def _build_combined_query(lat: float, lon: float, radius_m: int) -> str:
    """
    Yol ağını, binaları, sinyalleri VE 'engelleri' tek sorguda çek.

    'Engel' = bypass'ın geçemeyeceği büyük poligonal alanlar:
      - leisure=stadium / pitch / sports_centre  (stadyum, spor sahası)
      - amenity=hospital / school / university   (hastane, okul, kampüs)
      - landuse=cemetery / park / forest / military / industrial
      - natural=water / wood
      - waterway=riverbank
      - boundary=protected_area

    Bunlar 'building' olmadığı için `way["building"]` sorgusunda yakalanmaz.
    Ayrı sorgu lazım. Hepsi tek HTTP çağrısında geliyor (combined query).

    Overpass syntax notu:
        - 'out body;' tag + node referans listesi
        - '>;' way içindeki node'ları çöz (recurse down)
        - 'out skel qt;' bu node'ların koordinatları (tag yok)
    """
    highway_regex = "|".join(NETWORK.highway_types)
    return f"""
    [out:json][timeout:{OVERPASS.timeout_seconds}];
    (
      // 1. Araç yolları
      way(around:{radius_m},{lat},{lon})["highway"~"^({highway_regex})$"];
      // 2. Binalar (geleneksel yapılar)
      way(around:{radius_m},{lat},{lon})["building"];
      // 3. Engel: stadyum / spor sahası / yüzme havuzu
      way(around:{radius_m},{lat},{lon})["leisure"~"^(stadium|pitch|sports_centre|swimming_pool|track)$"];
      // 4. Engel: hastane / okul / kampüs
      way(around:{radius_m},{lat},{lon})["amenity"~"^(hospital|school|university|college|kindergarten)$"];
      // 5. Engel: park / mezarlık / askeri / endüstriyel alan / orman
      way(around:{radius_m},{lat},{lon})["landuse"~"^(cemetery|park|forest|military|industrial|recreation_ground)$"];
      // 6. Engel: su (göl, nehir kenarı)
      way(around:{radius_m},{lat},{lon})["natural"~"^(water|wood)$"];
      way(around:{radius_m},{lat},{lon})["waterway"="riverbank"];
    )->.ways;
    (
      // 7. Trafik sinyali node'ları
      node(around:{radius_m},{lat},{lon})["highway"="traffic_signals"];
    )->.signals;

    // Way'leri tag'leriyle yaz
    .ways out body;
    // Way'lerin içindeki node'ları koordinat olarak yaz
    .ways >;
    out skel qt;
    // Sinyal node'larını ayrıca tag'leriyle yaz
    .signals out body;
    """


def _split_overpass_elements(elements: list) -> tuple[
    dict[int, OsmNode], list[OsmWay], list[OsmWay], list[OsmWay], set[int]
]:
    """
    Overpass cevabını ayrıştır:
      - osm_nodes: tüm node'ların id → OsmNode haritası
      - highway_ways: araç yolları
      - building_ways: binalar
      - obstacle_ways: stadyum/park/hastane/su gibi geçilemez alanlar
      - signal_node_ids: trafik sinyali olan node id'leri

    Overpass cevabı KARMA olabilir:
      - Bazı node'lar 'skel' (sadece koordinat) — way içindeki node'lar
      - Bazı node'lar 'body' (koordinat + tag) — sinyal node'ları
      - Aynı node ikisi olarak iki kez gelebilir

    İki geçişli yaklaşım: önce hepsini topla, sonra birleştir.
    """
    # Geçici toplama: id → {lat, lon, tags}
    node_data: dict[int, dict] = {}
    highway_ways: list[OsmWay] = []
    building_ways: list[OsmWay] = []
    obstacle_ways: list[OsmWay] = []

    for el in elements:
        el_type = el.get("type")
        el_id = el.get("id")

        if el_type == "node":
            entry = node_data.setdefault(el_id, {})
            if el.get("lat") is not None:
                entry["lat"] = el["lat"]
            if el.get("lon") is not None:
                entry["lon"] = el["lon"]
            tags = el.get("tags") or {}
            if tags:
                entry.setdefault("tags", {}).update(tags)

        elif el_type == "way" and "nodes" in el:
            tags = el.get("tags") or {}
            way = OsmWay(osm_id=el_id, nodes=el["nodes"], tags=tags)
            # Sıra önemli: highway > obstacle > building
            # (bir alan hem leisure=stadium hem building olabilir; engel sayalım)
            if way.is_highway:
                highway_ways.append(way)
            elif way.is_obstacle:
                obstacle_ways.append(way)
            elif way.is_building:
                building_ways.append(way)

    # Toplanan veriden OsmNode'lara dönüştür
    osm_nodes: dict[int, OsmNode] = {}
    signal_node_ids: set[int] = set()

    for nid, data in node_data.items():
        lat = data.get("lat")
        lon = data.get("lon")
        if lat is None or lon is None:
            continue  # koordinatsız node işe yaramaz
        tags = data.get("tags", {})
        has_signal = tags.get("highway") == "traffic_signals"
        if has_signal:
            signal_node_ids.add(nid)
        osm_nodes[nid] = OsmNode(
            osm_id=nid, lat=lat, lon=lon, has_signal=has_signal
        )

    return osm_nodes, highway_ways, building_ways, obstacle_ways, signal_node_ids


# ── Yol ağı sadeleştirme ────────────────────────────────────────────────────

def _identify_junctions(highway_ways: list[OsmWay]) -> tuple[set[int], set[int]]:
    """
    Hangi node'ların gerçek kavşak olduğunu bul.
    Bir node 2+ farklı way'de geçiyorsa → kavşak.
    Way'in başı/sonu → endpoint (dead-end, mutlaka tutulur).
    """
    node_way_count: dict[int, int] = {}
    endpoints: set[int] = set()

    for w in highway_ways:
        if not w.nodes:
            continue
        for nid in set(w.nodes):
            node_way_count[nid] = node_way_count.get(nid, 0) + 1
        endpoints.add(w.nodes[0])
        endpoints.add(w.nodes[-1])

    junctions = {nid for nid, c in node_way_count.items() if c >= 2}
    junctions |= endpoints
    return junctions, endpoints


def _segment_ways(
    highway_ways: list[OsmWay],
    junctions: set[int],
) -> list[tuple[int, int, OsmWay]]:
    """
    Way'leri ardışık kavşak ikilileri halinde segmentlere böl.
    Her segment = bir EDGE adayı.
    """
    segments: list[tuple[int, int, OsmWay]] = []
    for w in highway_ways:
        important = [nid for nid in w.nodes if nid in junctions]
        for a, b in zip(important, important[1:]):
            if a != b:
                segments.append((a, b, w))
    return segments


def _format_node_line(local_id: str, node: OsmNode,
                      ref_lat: float, ref_lon: float) -> str:
    """TOON NODE satırı: NODE;id;x;y;lat;lon;has_signal"""
    x, y = node.to_local_xy(ref_lat, ref_lon)
    sig = 1 if node.has_signal else 0
    return f"NODE;{local_id};{round(x, 2)};{round(y, 2)};{node.lat};{node.lon};{sig}"


def _format_edge_line(edge_id: str, from_id: str, to_id: str,
                      way: OsmWay) -> str:
    """
    TOON EDGE satırı:
        EDGE;id;from;to;name;highway;maxspeed;oneway;lanes;bridge
    """
    name = (way.tags.get("name") or "").replace(";", ",").strip()
    highway = way.tags.get("highway", "")
    maxspeed = way.maxspeed_kmh()
    oneway = 1 if way.is_oneway() else 0
    lanes = way.num_lanes()
    bridge = 1 if (way.is_bridge() or way.is_tunnel()) else 0
    return (f"EDGE;{edge_id};{from_id};{to_id};"
            f"{name};{highway};{maxspeed};{oneway};{lanes};{bridge}")


def _build_toon(
    osm_nodes: dict[int, OsmNode],
    segments: list[tuple[int, int, OsmWay]],
    ref_lat: float,
    ref_lon: float,
) -> str:
    """Segmentleri ve node'ları TOON string'ine dönüştür, izole olanları ele."""
    # ID atama
    id_map: dict[int, str] = {}
    node_lines: list[str] = []

    significant = set()
    for a, b, _ in segments:
        significant.add(a)
        significant.add(b)

    for osm_id in significant:
        if osm_id not in osm_nodes:
            continue
        new_id = f"N{len(id_map) + 1}"
        id_map[osm_id] = new_id
        node_lines.append(_format_node_line(new_id, osm_nodes[osm_id],
                                            ref_lat, ref_lon))

    # EDGE: yönlü/yönsüz duplicate'leri ele
    seen_directional: set[tuple[str, str]] = set()
    seen_undirected: set[frozenset] = set()
    edge_lines: list[str] = []
    edge_counter = 1

    for a, b, way in segments:
        if a not in id_map or b not in id_map:
            continue
        na, nb = id_map[a], id_map[b]

        if way.is_oneway():
            key = (na, nb)
            if key in seen_directional:
                continue
            seen_directional.add(key)
        else:
            key = frozenset([na, nb])
            if key in seen_undirected:
                continue
            seen_undirected.add(key)

        edge_lines.append(_format_edge_line(f"E{edge_counter}", na, nb, way))
        edge_counter += 1

    # İzole node'ları (hiç kenarı olmayan) ele
    used_ids: set[str] = set()
    for ln in edge_lines:
        parts = ln.split(";")
        used_ids.add(parts[2])
        used_ids.add(parts[3])
    node_lines = [ln for ln in node_lines if ln.split(";")[1] in used_ids]

    if not node_lines:
        raise OverpassError("Sadeleştirme sonrası boş ağ kaldı")

    # TOON string'ini birleştir
    output = ["NETWORK_START"]
    output.extend(node_lines)
    output.extend(edge_lines)
    output.append("NETWORK_END")
    return "\n".join(output) + "\n"


# ── Bina dönüştürme ─────────────────────────────────────────────────────────

def _buildings_to_polygons(
    building_ways: list[OsmWay],
    osm_nodes: dict[int, OsmNode],
    ref_lat: float,
    ref_lon: float,
) -> dict:
    """
    Bina way'lerini hem yerel XY (geometri kontrolü için)
    hem coğrafi (Leaflet için) poligon listelerine dönüştür.
    """
    local_polygons: list[list[tuple[float, float]]] = []
    geo_polygons: list[list[tuple[float, float]]] = []
    metadata: list[dict] = []  # her bina için ek bilgi

    for way in building_ways:
        local_pts: list[tuple[float, float]] = []
        geo_pts: list[tuple[float, float]] = []
        for nid in way.nodes:
            node = osm_nodes.get(nid)
            if node is None:
                continue
            x, y = node.to_local_xy(ref_lat, ref_lon)
            local_pts.append((round(x, 2), round(y, 2)))
            geo_pts.append((node.lat, node.lon))

        if len(local_pts) < 3:
            continue  # geçersiz poligon

        local_polygons.append(local_pts)
        geo_polygons.append(geo_pts)

        # Bina meta verisi (görselleştirme için ileride kullanılabilir)
        levels = way.tags.get("building:levels", "")
        height = way.tags.get("height", "")
        metadata.append({
            "building_type": way.tags.get("building", "yes"),
            "levels": int(levels) if levels.isdigit() else None,
            "height_m": _parse_height(height),
            "name": way.tags.get("name", ""),
        })

    return {
        "local_polygons": local_polygons,
        "geo_polygons": geo_polygons,
        "metadata": metadata,
        "count": len(local_polygons),
    }


def _obstacles_to_polygons(
    obstacle_ways: list[OsmWay],
    osm_nodes: dict[int, OsmNode],
    ref_lat: float,
    ref_lon: float,
) -> dict:
    """
    Engel way'lerini (stadyum, park, hastane, su, vs.) poligonlara dönüştür.
    Yapısı _buildings_to_polygons'a benzer ama her engele 'kategori' ekler
    (örn. "leisure:stadium", "amenity:hospital").
    """
    local_polygons: list[list[tuple[float, float]]] = []
    geo_polygons: list[list[tuple[float, float]]] = []
    metadata: list[dict] = []

    for way in obstacle_ways:
        local_pts: list[tuple[float, float]] = []
        geo_pts: list[tuple[float, float]] = []
        for nid in way.nodes:
            node = osm_nodes.get(nid)
            if node is None:
                continue
            x, y = node.to_local_xy(ref_lat, ref_lon)
            local_pts.append((round(x, 2), round(y, 2)))
            geo_pts.append((node.lat, node.lon))

        if len(local_pts) < 3:
            continue

        local_polygons.append(local_pts)
        geo_polygons.append(geo_pts)
        metadata.append({
            "obstacle_type": way.obstacle_type,  # "leisure:stadium" gibi
            "name": way.tags.get("name", ""),
        })

    return {
        "local_polygons": local_polygons,
        "geo_polygons": geo_polygons,
        "metadata": metadata,
        "count": len(local_polygons),
    }


def _parse_height(raw: str) -> Optional[float]:
    """'12 m' veya '12' → 12.0; geçersizse None."""
    if not raw:
        return None
    digits = ""
    for ch in str(raw):
        if ch.isdigit() or ch == ".":
            digits += ch
        elif digits:
            break
    try:
        return float(digits) if digits else None
    except ValueError:
        return None


# ── ANA API: tek fonksiyon, her şeyi getirir ────────────────────────────────

def fetch_osm_area(
    lat: float,
    lon: float,
    radius_m: int = 500,
    auto_expand: bool = True,
) -> dict:
    """
    Belirtilen alanı tek Overpass çağrısıyla çek: yollar + binalar + sinyaller.

    Args:
        lat, lon: merkez koordinatı (WGS84)
        radius_m: yarıçap (metre)
        auto_expand: True ise yol bulunamazsa yarıçap ×1.5 ile bir kez tekrar dener

    Returns:
        {
            "toon":       "NETWORK_START\\n...\\nNETWORK_END\\n",
            "buildings": {
                "local_polygons": [[(x,y), ...], ...],
                "geo_polygons":   [[(lat,lon), ...], ...],
                "metadata":       [{"building_type", "levels", ...}, ...],
                "count": int,
            },
            "stats": {
                "raw_nodes": int,
                "raw_highways": int,
                "raw_buildings": int,
                "signal_nodes": int,
                "kept_junctions": int,
                "edges": int,
                "fetch_time_seconds": float,
                "radius_used": int,
            }
        }

    Raises:
        OverpassError: Overpass'a ulaşılamadı veya alan tamamen boş.
    """
    logger.info("Alan çekiliyor: (%s, %s) r=%dm", lat, lon, radius_m)
    start = time.time()

    # 1. Overpass'tan tek seferde her şeyi al
    query = _build_combined_query(lat, lon, radius_m)
    data = _post_overpass(query)
    elements = data.get("elements", [])

    if not elements:
        # Boş alan — auto_expand denemesi
        if auto_expand:
            new_radius = int(radius_m * 1.5)
            logger.warning("Boş cevap — yarıçap %dm → %dm ile tekrar deneniyor",
                           radius_m, new_radius)
            return fetch_osm_area(lat, lon, new_radius, auto_expand=False)
        raise OverpassError(
            f"({lat}, {lon}) konumunda {radius_m}m içinde hiç OSM verisi yok. "
            "Bu nokta deniz, çöl veya OSM'nin haritalanmadığı bir bölge olabilir."
        )

    # 2. Cevabı kategorilere ayır
    osm_nodes, highway_ways, building_ways, obstacle_ways, signal_ids = \
        _split_overpass_elements(elements)
    logger.info("Çiğ veri: %d node, %d yol, %d bina, %d engel, %d sinyal",
                len(osm_nodes), len(highway_ways),
                len(building_ways), len(obstacle_ways), len(signal_ids))

    # 3. Yol ağı kontrolü
    if not highway_ways:
        if auto_expand:
            new_radius = int(radius_m * 1.5)
            logger.warning("Araç yolu yok — yarıçap %dm → %dm",
                           radius_m, new_radius)
            return fetch_osm_area(lat, lon, new_radius, auto_expand=False)
        raise OverpassError(
            f"Bu noktada ({lat}, {lon}) {radius_m}m yarıçapta araç yolu yok. "
            f"Sadece {len(building_ways)} bina, {len(signal_ids)} sinyal bulundu. "
            "Muhtemelen bir kampüs içi, park veya kırsal alan. "
            "Şehir merkezine yakın bir konum seçin."
        )

    # 4. Yol ağını sadeleştir → TOON
    junctions, _ = _identify_junctions(highway_ways)
    segments = _segment_ways(highway_ways, junctions)
    toon = _build_toon(osm_nodes, segments, ref_lat=lat, ref_lon=lon)

    # 5. Binaları dönüştür
    buildings = _buildings_to_polygons(building_ways, osm_nodes, lat, lon)

    # 5b. Engelleri dönüştür (binalardan ayrı, kategorili)
    obstacles = _obstacles_to_polygons(obstacle_ways, osm_nodes, lat, lon)

    # 6. İstatistikler
    fetch_time = round(time.time() - start, 2)

    n_nodes = sum(1 for ln in toon.split("\n") if ln.startswith("NODE;"))
    n_edges = sum(1 for ln in toon.split("\n") if ln.startswith("EDGE;"))

    logger.info("Bitti: %d kavşak, %d kenar, %d bina, %d engel (%.2fs)",
                n_nodes, n_edges, buildings["count"], obstacles["count"],
                fetch_time)

    return {
        "toon": toon,
        "buildings": buildings,
        "obstacles": obstacles,
        "stats": {
            "raw_nodes":      len(osm_nodes),
            "raw_highways":   len(highway_ways),
            "raw_buildings":  len(building_ways),
            "raw_obstacles":  len(obstacle_ways),
            "signal_nodes":   len(signal_ids),
            "kept_junctions": n_nodes,
            "edges":          n_edges,
            "fetch_time_seconds": fetch_time,
            "radius_used":    radius_m,
        },
    }


# ── GERİYE DÖNÜK UYUMLULUK (eski API) ───────────────────────────────────────
# api_server.py mevcut fonksiyon adlarını çağırıyor, kırmayalım.

def fetch_osm_network(lat: float, lon: float, radius_m: int = 500) -> str:
    """
    ESKİ API: sadece TOON string döndürür.
    Yeni kodda fetch_osm_area kullan — daha zengin veri verir.
    """
    return fetch_osm_area(lat, lon, radius_m)["toon"]


def fetch_buildings(lat: float, lon: float, radius_m: int) -> dict:
    """
    ESKİ API: sadece bina verisi döndürür.
    Yeni kodda fetch_osm_area kullan — tek HTTP request, üç sonuç birden.

    DİKKAT: Bu fonksiyon eski koddaki dict şekline uygun cevap dönüyor;
    yeni 'metadata' ve 'count' alanları da var, eski kullanıcılar bunları
    görmezden gelebilir.
    """
    try:
        return fetch_osm_area(lat, lon, radius_m, auto_expand=False)["buildings"]
    except OverpassError as e:
        logger.warning("Bina çekme başarısız (boş döndürülüyor): %s", e)
        return {
            "local_polygons": [],
            "geo_polygons": [],
            "metadata": [],
            "count": 0,
        }


# ── Manuel test ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Odunpazarı testi (yoğun, yol bol)
    print("\n=== Odunpazarı (39.7839, 30.5144) ===")
    result = fetch_osm_area(lat=39.7839, lon=30.5144, radius_m=500)
    print(f"İstatistikler: {result['stats']}")
    print(f"İlk 5 satır TOON:")
    for ln in result["toon"].split("\n")[:5]:
        print(f"  {ln}")
    print(f"Bina sayısı: {result['buildings']['count']}")
    if result['buildings']['metadata']:
        print(f"İlk binanın meta: {result['buildings']['metadata'][0]}")

    # Boş alan testi (deniz ortası gibi)
    print("\n=== Boş Test (denizde, auto_expand devrede) ===")
    try:
        r2 = fetch_osm_area(lat=40.0, lon=29.0, radius_m=200)
        print(f"Otomatik genişledi mi? Yarıçap: {r2['stats']['radius_used']}")
    except OverpassError as e:
        print(f"Beklendiği gibi: {e}")