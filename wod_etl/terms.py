"""Translate Japanese column-name terms to English, for matching only."""
import re

# Longest first, so 塩分濃度 is replaced before 塩分.
_TERMS = [
    ("度分表記", "degrees minutes"), ("度表記", "degrees"),
    ("溶存酸素", "dissolved oxygen"), ("クロロフィル", "chlorophyll"),
    ("亜硝酸塩", "nitrite"), ("硝酸塩", "nitrate"), ("リン酸塩", "phosphate"),
    ("ケイ酸塩", "silicate"), ("珪酸塩", "silicate"), ("アンモニウム", "ammonium"),
    ("電気伝導度", "conductivity"), ("伝導度", "conductivity"),
    ("塩分濃度", "salinity"), ("塩分", "salinity"),
    ("海水温", "temperature"), ("水温", "temperature"), ("気温", "air temperature"),
    ("温度", "temperature"), ("気圧", "air pressure"), ("水圧", "pressure"),
    ("圧力", "pressure"), ("水深", "depth"), ("深度", "depth"), ("深さ", "depth"),
    ("緯度", "latitude"), ("経度", "longitude"), ("観測点", "station"), ("測点", "station"),
    ("地点", "station"), ("ステーション", "station"), ("年月日", "date"), ("日付", "date"),
    ("日時", "date time"), ("時刻", "time"), ("酸素", "oxygen"), ("濁度", "turbidity"),
    ("蛍光", "fluorescence"), ("種名", "taxon"), ("学名", "scientific name"),
    ("個体数", "count"), ("湿重量", "wet weight"), ("乾重量", "dry weight"),
]
_CJK = re.compile(r"[぀-ヿ㐀-鿿＀-￯]")
# Full-width brackets and spaces to ASCII.
_WIDTH = str.maketrans({"（": "(", "）": ")", "［": "[", "］": "]", "　": " ", "，": ","})


def english_terms(name: str) -> str:
    """`name` with known Japanese terms in English: '緯度(度表記)' -> 'latitude (degrees)'."""
    text = str(name)
    if not _CJK.search(text):
        return text
    text = text.translate(_WIDTH)
    for jp, en in _TERMS:
        text = text.replace(jp, f" {en} ")
    return re.sub(r"\s+", " ", text).strip()
