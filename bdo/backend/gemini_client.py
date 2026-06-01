"""
backend/gemini_client.py — Gemini AI optimizasyon istemcisi

İki API:
  optimize_network_with_simulation(toon_data, bottleneck_summary)
      → ÖNERİLEN: simülasyon sonuçlarıyla zenginleştirilmiş prompt
      → Gemini'a "işte en yoğun yollar, en kritik kavşaklar — buna göre öner"
      → Veri-tabanlı kararla yüksek kaliteli bypass

  optimize_network_with_llm(prompt)
      → ESKİ API: simülasyon olmadan
      → Geriye dönük uyumluluk için tutuldu
"""
import os
import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY bulunamadı. Proje kökünde .env dosyası oluşturup "
        "GEMINI_API_KEY=... satırını eklemen gerekiyor. "
        "Örnek için .env.example dosyasına bak."
    )

genai.configure(api_key=API_KEY)

MODEL_NAME = "gemini-2.5-flash-lite"   # Hızlı + ucuz, demo için yeterli


# ─────────────────────────────────────────────────────────────────────────────
#  YENİ API: Simülasyon verisiyle zenginleştirilmiş
# ─────────────────────────────────────────────────────────────────────────────
def optimize_network_with_simulation(
    toon_data: str,
    bottleneck_summary: str,
) -> str:
    try:
        model = genai.GenerativeModel(MODEL_NAME)

        system_instruction = """
Sen uzman bir şehir trafik optimizasyon mühendisisin. Sana üç şey vereceğim:

1. AĞ VERİSİ: TOON formatında NODE ve EDGE listesi.
2. SİMÜLASYON RAPORU: en yoğun 5 yol ve en kritik 5 kavşak (darboğazlar).
3. AMAÇ: Darboğaz yolların yükünü azaltmak için EN FAZLA 3 ADET yeni bypass yolu (EDGE) eklemek.

STRATEJİK KURALLAR:
- İYİ BİR BYPASS = darboğaz kavşak ÇİFTİNİN ETRAFINDAN dolanan alternatif rotadır.
- En kritik kavşakların KOMŞULARINI birbirine bağlayan yollar öner.
- Ağı çok fazla değiştirmemek için 1 ile 3 adet arasında en mantıklı yolları seç.
- Aynı iki node arasında zaten edge varsa (yön ne olursa olsun), o çifti tekrar ÖNERME.

ÇIKTI FORMATI (KESİN KURAL):
- SADECE şu formatta, her öneri YENİ BİR SATIRDA olacak şekilde dön:
EDGE;LLM_BYPASS_1;NX;NY
EDGE;LLM_BYPASS_2;NZ;NW
- NX, NY vb., ağda VAR OLAN farklı node ID'leri olmalı.
- Açıklama, markdown kod bloğu, merhaba, hiçbir ek metin EKLEME. Sadece EDGE satırları.
"""

        user_content = (
            "SİMÜLASYON RAPORU:\n"
            f"{bottleneck_summary}\n\n"
            "AĞ VERİSİ (TOON):\n"
            f"{toon_data}"
        )

        response = model.generate_content(
            system_instruction + "\n\n" + user_content
        )
        return _sanitize_edge_response(response.text)

    except Exception as e:
        print(f"[GEMINI API HATA] {e}")
        return "HATA"


# ─────────────────────────────────────────────────────────────────────────────
#  ESKİ API (geriye dönük uyumluluk)
# ─────────────────────────────────────────────────────────────────────────────
def optimize_network_with_llm(prompt: str) -> str:
    """
    ESKİ API. Simülasyon verisi olmadan çağrılır.
    Yeni kod optimize_network_with_simulation kullanmalı.
    """
    try:
        model = genai.GenerativeModel(MODEL_NAME)

        system_instruction = """
        Sen uzman bir şehir plancısı ve trafik optimizasyon yapay zekasısın.
        Kullanıcı sana TOON formatında bir trafik ağı verisi gönderecek.
        Görevlerin:
        1. Ağaç yapısındaki çıkmaz sokakları veya mantıklı düğümleri bul.
        2. Trafiği rahatlatmak ve bir döngü (bypass/kavşak) oluşturmak için
           TEK BİR yeni kenar (EDGE) ekle.
        3. Çıktı formatı KESİNLİKLE VE SADECE şu şekilde olmalıdır:
           EDGE;LLM_BYPASS_1;[Baslangic_Node_ID];[Bitis_Node_ID]
        4. Çıktında asla merhaba, açıklama, fazladan yazı veya markdown kodu
           olmamalıdır. Sadece EDGE satırını yaz.
        """

        response = model.generate_content(
            system_instruction + "\n\nAĞ VERİSİ:\n" + prompt
        )
        return _sanitize_edge_response(response.text)

    except Exception as e:
        print(f"[GEMINI API HATA] {e}")
        return "HATA"


# ─────────────────────────────────────────────────────────────────────────────
#  YARDIMCI: Çıktı temizleme
# ─────────────────────────────────────────────────────────────────────────────
def _sanitize_edge_response(raw: str) -> str:
    """
    LLM bazen birden fazla satır yerine araya boşluk/markdown koyabilir.
    Sadece geçerli 'EDGE;' ile başlayan TÜM satırları topla ve geri dön.
    """
    if not raw:
        return "HATA"

    cleaned = raw.strip().replace("```", "").replace("`", "")
    valid_edges = []
    
    for line in cleaned.split("\n"):
        line = line.strip()
        if line.startswith("EDGE;"):
            valid_edges.append(line)
            
    if valid_edges:
        return "\n".join(valid_edges) # Geçerli tüm yolları alt alta birleştir
        
    return cleaned