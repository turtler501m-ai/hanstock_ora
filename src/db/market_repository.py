from __future__ import annotations

import functools
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.config import config
from src.utils.logger import logger
from src.db import repository as _root

KST = timezone(timedelta(hours=9))

def connect_db():
    return _root.connect_db()

def init_db() -> None:
    _root.init_db()

WATCHLIST_FILE = Path(".runtime/watchlist.json")
CUSTOM_STRATEGY_PREFIXES = ("\uc0ac\uc6a9\uc790\uc804\ub7b5", "\U0001f50c", "\U0001f9e0", "\u2699", "\U0001f4c8", "\U0001f4ca", "\U0001f6e1")

STOCK_NAMES: dict[str, str] = {
    "005930": "삼성전자", "000660": "SK하이닉스", "035420": "NAVER", "035720": "카카오", 
    "018260": "삼성에스디에스", "009150": "삼성전기", "066570": "LG전자", "034220": "LG디스플레이", 
    "000990": "DB하이텍", "042700": "한미반도체", "036930": "주성엔지니어링", "240810": "원익IPS", 
    "058470": "리노공업", "357780": "솔브레인", "039030": "이오테크닉스", "056190": "에스에프에이", 
    "067310": "하나마이크론", "005290": "동진쎄미켐", "012510": "더존비즈온", "053800": "안랩", 
    "263750": "펄어비스", "078340": "컴투스", "112040": "위메이드", "293490": "카카오게임즈", 
    "192080": "더블유게임즈", "251270": "넷마블", "036570": "엔씨소프트", "259960": "크래프톤",
    "005380": "현대차", "000270": "기아", "012330": "현대모비스", "011210": "현대위아", 
    "018880": "한온시스템", "161390": "한국타이어앤테크놀로지", "073240": "금호타이어", 
    "204320": "HL만도", "003490": "대한항공", "020560": "아시아나항공", "011200": "HMM", 
    "028670": "팬오션", "086280": "현대글로비스", "000120": "CJ대한통운", "012450": "한화에어로스페이스", 
    "047810": "한국항공우주", "079550": "LIG넥스원", "064350": "현대로템", "042660": "한화오션", 
    "329180": "HD현대중공업", "010140": "삼성중공업", "034020": "두산에너빌리티", "267250": "HD현대", 
    "082740": "HSD엔진", "272210": "한화시스템", "047820": "하림지주", "180640": "한진칼", "001800": "삼양홀딩스",
    "207940": "삼성바이오로직스", "068270": "셀트리온", "000100": "유한양행", "128940": "한미약품", 
    "006280": "녹십자", "069620": "대웅제약", "185750": "종근당", "009290": "광동제약", 
    "170900": "동아에스티", "068760": "셀트리온제약", "028300": "HLB", "196170": "알테오젠", 
    "145020": "휴젤", "086900": "메디톡스", "237690": "에스티팜", "141080": "리그켐바이오", 
    "143860": "케어젠", "096530": "씨젠", "091990": "셀트리온헬스케어",
    "373220": "LG에너지솔루션", "006400": "삼성SDI", "051910": "LG화학", "003670": "포스코퓨처엠", 
    "247540": "에코프로비엠", "086520": "에코프로", "066970": "엘앤에프", "096770": "SK이노베이션", 
    "010950": "S-Oil", "043260": "HD현대일렉트릭", "112610": "씨에스윈드", "009830": "한화솔루션", 
    "001570": "금양", "011170": "롯데케미칼", "011780": "금호석유", "377300": "카카오페이",
    "105560": "KB금융", "055550": "신한지주", "086790": "하나금융지주", "316140": "우리금융지주", 
    "024110": "기업은행", "138040": "메리츠금융지주", "032830": "삼성생명", "088350": "한화생명", 
    "082640": "동양생명", "001450": "현대해상", "005830": "DB손해보험", "000810": "삼성화재", 
    "006800": "미래에셋증권", "005940": "NH투자증권", "071050": "한국금융지주", "016360": "삼성증권", 
    "030200": "KT", "017670": "SK텔레콤", "032640": "LG유플러스",
    "005490": "POSCO홀딩스", "010130": "고려아연", "004020": "현대제철", "001230": "동국제강", 
    "103140": "풍산", "000670": "영풍", "001390": "KG케미칼", "300720": "한일시멘트", 
    "003410": "쌍용C&E", "015760": "한국전력", "036460": "한국가스공사", "071320": "지역난방공사", 
    "000720": "현대건설", "047040": "대우건설", "375500": "DL이앤씨", "006360": "GS건설",
    "097950": "CJ제일제당", "007310": "오뚜기", "004370": "농심", "003230": "삼양식품", 
    "000080": "하이트진로", "001680": "대상", "026960": "동서", "005610": "SPC삼립", 
    "090430": "아모레퍼시픽", "051900": "LG생활건강", "018250": "애경산업", "192820": "코스맥스", 
    "161890": "한국콜마", "004170": "신세계", "069960": "현대백화점", "023530": "롯데쇼핑", 
    "282330": "BGF리테일", "007070": "GS리테일", "139480": "이마트", "008770": "호텔신라", 
    "035250": "강원랜드", "039130": "하나투어", "080160": "모두투어", "352820": "하이브", 
    "035900": "JYP Ent.", "041510": "에스엠", "122870": "와이지엔터테인먼트", "035760": "CJ ENM", 
    "253450": "스튜디오드래곤", "033780": "KT&G", "021240": "코웨이", "003550": "LG", 
    "034730": "SK", "028260": "삼성물산", "000150": "두산", "047050": "포스코인터내셔널",
    "001040": "CJ", "078930": "GS", "000880": "한화", "006260": "LS", "004800": "효성",
    "004990": "롯데지주", "000210": "DL", "002020": "코오롱", "003240": "태광산업",
    "009540": "HD한국조선해양", "005250": "녹십자홀딩스", "011070": "LG이노텍", "002710": "TCC스틸",
    "010060": "OCI홀딩스", "019170": "신풍제약", "005385": "현대차우"
}

STOCK_SECTORS: dict[str, str] = {
    "005930": "반도체", "000660": "반도체", "035420": "플랫폼", "035720": "플랫폼",
    "018260": "IT서비스", "009150": "IT부품", "066570": "가전/IT", "034220": "가전/IT",
    "000990": "반도체", "042700": "반도체", "036930": "반도체", "240810": "반도체",
    "058470": "반도체", "357780": "IT소재", "039030": "반도체", "056190": "IT부품",
    "067310": "반도체", "005290": "IT소재", "012510": "소프트웨어", "053800": "소프트웨어",
    "263750": "게임", "078340": "게임", "112040": "게임", "293490": "게임",
    "192080": "게임", "251270": "게임", "036570": "게임", "259960": "게임",
    "005380": "자동차", "000270": "자동차", "012330": "자동차부품", "011210": "자동차부품",
    "018880": "자동차부품", "161390": "자동차부품", "073240": "자동차부품", "204320": "자동차부품",
    "003490": "항공", "020560": "항공", "011200": "해운", "028670": "해운",
    "086280": "물류", "000120": "물류", "012450": "방산/우주", "047810": "방산/우주",
    "079550": "방산", "064350": "방산/철도", "042660": "조선", "329180": "조선",
    "010140": "조선", "034020": "원자력/중공업", "082740": "선박엔진", "272210": "방산/IT",
    "207940": "바이오", "068270": "바이오", "000100": "제약", "128940": "제약",
    "006280": "제약", "069620": "제약", "185750": "제약", "009290": "제약",
    "170900": "제약", "068760": "바이오", "028300": "바이오", "196170": "바이오",
    "145020": "바이오", "086900": "바이오", "237690": "바이오", "141080": "바이오",
    "143860": "바이오", "096530": "바이오", "091990": "바이오", "019170": "제약",
    "373220": "2차전지", "006400": "2차전지", "051910": "배터리/화학", "003670": "2차전지소재",
    "247540": "2차전지소재", "086520": "2차전지소재", "066970": "2차전지소재", "096770": "에너지/화학",
    "010950": "정유", "043260": "전력인프라", "112610": "풍력에너지", "009830": "태양광/화학",
    "001570": "2차전지", "011170": "화학", "011780": "화학",
    "105560": "은행지주", "055550": "은행지주", "086790": "은행지주", "316140": "우리금융지주", 
    "024110": "기업은행", "138040": "메리츠금융지주", "032830": "삼성생명", "088350": "한화생명", 
    "082640": "동양생명", "001450": "현대해상", "005830": "DB손해보험", "000810": "삼성화재", 
    "006800": "미래에셋증권", "005940": "NH투자증권", "071050": "한국금융지주", "016360": "삼성증권", 
    "030200": "KT", "017670": "SK텔레콤", "032640": "LG유플러스",
    "005490": "POSCO홀딩스", "010130": "고려아연", "004020": "현대제철", "001230": "동국제강", 
    "103140": "풍산", "000670": "영풍", "001390": "KG케미칼", "300720": "한일시멘트", 
    "003410": "쌍용C&E", "015760": "한국전력", "036460": "한국가스공사", "071320": "지역난방공사", 
    "000720": "현대건설", "047040": "대우건설", "375500": "DL이앤씨", "006360": "GS건설",
    "097950": "CJ제일제당", "007310": "오뚜기", "004370": "농심", "003230": "삼양식품", 
    "000080": "하이트진로", "001680": "대상", "026960": "동서", "005610": "SPC삼립", 
    "090430": "아모레퍼시픽", "051900": "LG생활건강", "018250": "애경산업", "192820": "코스맥스", 
    "161890": "한국콜마", "004170": "신세계", "069960": "현대백화점", "023530": "롯데쇼핑", 
    "282330": "BGF리테일", "007070": "GS리테일", "139480": "이마트", "008770": "호텔신라", 
    "035250": "강원랜드", "039130": "하나투어", "080160": "모두투어", "352820": "하이브", 
    "035900": "JYP Ent.", "041510": "에스엠", "122870": "와이지엔터테인먼트", "035760": "CJ ENM", 
    "253450": "스튜디오드래곤", "033780": "KT&G", "021240": "코웨이", "003550": "LG", 
    "034730": "SK", "028260": "삼성물산", "000150": "두산", "047050": "포스코인터내셔널",
    "001040": "CJ", "078930": "GS", "000880": "한화", "006260": "LS", "004800": "효성",
    "004990": "롯데지주", "000210": "DL", "002020": "코오롱", "003240": "태광산업",
    "009540": "HD한국조선해양", "005250": "녹십자홀딩스", "011070": "LG이노텍", "002710": "TCC스틸",
    "010060": "OCI홀딩스", "019170": "신풍제약", "005385": "현대차우"
}

STOCK_SECTORS: dict[str, str] = {
    "005930": "반도체", "000660": "반도체", "035420": "플랫폼", "035720": "플랫폼",
    "018260": "IT서비스", "009150": "IT부품", "066570": "가전/IT", "034220": "가전/IT",
    "000990": "반도체", "042700": "반도체", "036930": "반도체", "240810": "반도체",
    "058470": "반도체", "357780": "IT소재", "039030": "반도체", "056190": "IT부품",
    "067310": "반도체", "005290": "IT소재", "012510": "소프트웨어", "053800": "소프트웨어",
    "263750": "게임", "078340": "게임", "112040": "게임", "293490": "게임",
    "192080": "게임", "251270": "게임", "036570": "게임", "259960": "게임",
    "005380": "자동차", "000270": "자동차", "012330": "자동차부품", "011210": "자동차부품",
    "018880": "자동차부품", "161390": "자동차부품", "073240": "자동차부품", "204320": "자동차부품",
    "003490": "항공", "020560": "항공", "011200": "해운", "028670": "해운",
    "086280": "물류", "000120": "물류", "012450": "방산/우주", "047810": "방산/우주",
    "079550": "방산", "064350": "방산/철도", "042660": "조선", "329180": "조선",
    "010140": "조선", "034020": "원자력/중공업", "082740": "선박엔진", "272210": "방산/IT",
    "207940": "바이오", "068270": "바이오", "000100": "제약", "128940": "제약",
    "006280": "제약", "069620": "제약", "185750": "제약", "009290": "제약",
    "170900": "제약", "068760": "바이오", "028300": "바이오", "196170": "바이오",
    "145020": "바이오", "086900": "바이오", "237690": "바이오", "141080": "바이오",
    "143860": "바이오", "096530": "바이오", "091990": "바이오", "019170": "제약",
    "373220": "2차전지", "006400": "2차전지", "051910": "배터리/화학", "003670": "2차전지소재",
    "247540": "2차전지소재", "086520": "2차전지소재", "066970": "2차전지소재", "096770": "에너지/화학",
    "010950": "정유", "043260": "전력인프라", "112610": "풍력에너지", "009830": "태양광/화학",
    "001570": "2차전지", "011170": "화학", "011780": "화학",
    "105560": "은행지주", "055550": "은행지주", "086790": "은행지주", "316140": "우리금융지주",
    "024110": "기업은행", "138040": "메리츠금융지주", "032830": "삼성생명", "088350": "한화생명",
    "082640": "동양생명", "001450": "현대해상", "005830": "DB손해보험", "000810": "삼성화재",
    "006800": "미래에셋증권", "005940": "NH투자증권", "071050": "한국금융지주", "016360": "삼성증권",
    "030200": "KT", "017670": "SK텔레콤", "032640": "LG유플러스",
    "005490": "POSCO홀딩스", "010130": "고려아연", "004020": "현대제철", "001230": "동국제강",
    "103140": "풍산", "000670": "영풍", "001390": "KG케미칼", "300720": "한일시멘트",
    "003410": "쌍용C&E", "015760": "한국전력", "036460": "한국가스공사", "071320": "지역난방공사",
    "000720": "현대건설", "047040": "대우건설", "375500": "DL이앤씨", "006360": "GS건설",
    "097950": "CJ제일제당", "007310": "오뚜기", "004370": "농심", "003230": "삼양식품",
    "000080": "하이트진로", "001680": "대상", "026960": "동서", "005610": "SPC삼립",
    "090430": "아모레퍼시픽", "051900": "LG생활건강", "018250": "애경산업", "192820": "코스맥스",
    "161890": "한국콜마", "004170": "신세계", "069960": "현대백화점", "023530": "롯데쇼핑",
    "282330": "BGF리테일", "007070": "GS리테일", "139480": "이마트", "008770": "호텔신라",
    "035250": "강원랜드", "039130": "하나투어", "080160": "모두투어", "352820": "하이브",
    "035900": "JYP Ent.", "041510": "에스엠", "122870": "와이지엔터테인먼트", "035760": "CJ ENM",
    "253450": "스튜디오드래곤", "033780": "KT&G", "021240": "코웨이", "003550": "LG",
    "034730": "SK", "028260": "삼성물산", "000150": "두산", "047050": "포스코인터내셔널",
    "001040": "CJ", "078930": "GS", "000880": "한화", "006260": "LS", "004800": "효성",
    "004990": "롯데지주", "000210": "DL", "002020": "코오롱", "003240": "태광산업",
    "009540": "HD한국조선해양", "005250": "녹십자홀딩스", "011070": "LG이노텍", "002710": "TCC스틸",
    "010060": "OCI홀딩스", "019170": "신풍제약", "005385": "현대차우"
}

KOSPI_UNIVERSE = [
    # 반도체/IT/빅테크
    "005930", "000660", "035420", "035720", "018260", "009150", "066570", 
    "034220", "000990", "042700", "036930", "240810", "058470", "357780", 
    "039030", "056190", "067310", "005290", "012510", "053800", "263750", 
    "078340", "112040", "293490", "192080", "251270", "036570", "259960",
    # 자동차/기계/조선/방산
    "005380", "000270", "012330", "011210", "018880", "161390", "073240", 
    "204320", "003490", "020560", "011200", "028670", "086280", "000120", 
    "012450", "047810", "079550", "064350", "042660", "329180", "010140", 
    "034020", "267250", "082740", "272210",
    # 바이오/헬스케어
    "207940", "068270", "000100", "128940", "006280", "069620", "185750", 
    "009290", "170900", "068760", "028300", "196170", "145020", "086900",
    "237690", "141080", "143860", "096530",
    # 2차전지/배터리/화학/에너지
    "373220", "006400", "051910", "003670", "247540", "086520", "066970", 
    "096770", "010950", "043260", "034020", "112610", "009830", "001570", 
    "011170", "011780", "377300",
    # 금융/은행/카드/지주
    "105560", "055550", "086790", "316140", "024110", "138040", "032830", 
    "088350", "082640", "001450", "005830", "000810", "006800", "005940", 
    "071050", "016360", "030200", "017670", "032640",
    # 철강/소재/비철/건설
    "005490", "010130", "004020", "001230", "103140", "000670", "001390",
    "300720", "015760", "036460", "071320", "000720", "047040",
    "375500", "006360",
    # 유통/음식료/화장품/엔터/레저
    "097950", "007310", "004370", "003230", "000080", "001680", "026960", 
    "005610", "090430", "051900", "018250", "192820", "161890", "004170", 
    "069960", "023530", "282330", "007070", "139480", "008770", "035250", 
    "039130", "080160", "352820", "035900", "041510", "122870", "035760", 
    "253450", "033780", "021240", "003550", "034730", "028260", 
    "000150", "047050",
    # 추가 우량주 보강 (시총 상위 매칭)
    "001040", "078930", "000880", "006260", "004800",
    "004990", "000210", "002020", "003240", "009540",
    "005250", "011070", "002710", "010060", "019170",
    "005385", "047820", "180640", "001800"
]
KOSPI_UNIVERSE = list(dict.fromkeys(KOSPI_UNIVERSE))


def load_watchlist_data() -> dict:
    try:
        init_db()
        with connect_db() as conn:
            c = conn.execute("SELECT symbol, name FROM watchlist ORDER BY symbol ASC")
            rows = c.fetchall()
            symbols = [row[0] for row in rows]
            names = {row[0]: row[1] for row in rows if len(row) > 1 and row[1]}
            
            # 종목 개수가 아예 비었을 때(0개)만 대표 우량주 5종목 자동 마이그레이션
            if len(symbols) == 0:
                default_symbols = ["005930", "000660", "035420", "005380", "035720"]
                ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                for s in default_symbols:
                    name = STOCK_NAMES.get(s, "우량 종목")
                    conn.execute(
                        "INSERT OR IGNORE INTO watchlist (symbol, name, created_at) VALUES (?, ?, ?)",
                        (s, name, ts)
                    )
                conn.commit()
                symbols = default_symbols
                names = {s: STOCK_NAMES.get(s, "우량 종목") for s in default_symbols}
            
            c_set = conn.execute("SELECT value FROM watchlist_settings WHERE key = 'ai_auto_add'")
            row_set = c_set.fetchone()
            if row_set is None:
                conn.execute("INSERT OR IGNORE INTO watchlist_settings (key, value) VALUES ('ai_auto_add', '0')")
                conn.commit()
                ai_auto_add = False
            else:
                ai_auto_add = (row_set[0] == '1')
                
            c_thresh = conn.execute("SELECT value FROM watchlist_settings WHERE key = 'ai_auto_add_threshold'")
            row_thresh = c_thresh.fetchone()
            if row_thresh is None:
                conn.execute("INSERT OR IGNORE INTO watchlist_settings (key, value) VALUES ('ai_auto_add_threshold', '3.0')")
                conn.commit()
                ai_auto_add_threshold = 3.0
            else:
                try:
                    ai_auto_add_threshold = float(row_thresh[0])
                except ValueError:
                    ai_auto_add_threshold = 3.0
                
            return {
                "symbols": symbols,
                "names": names,
                "ai_auto_add": ai_auto_add,
                "ai_auto_add_threshold": ai_auto_add_threshold
            }
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load watchlist from DB: {e}")
        return {
            "symbols": KOSPI_UNIVERSE,
            "names": {s: STOCK_NAMES.get(s, "우량 종목") for s in KOSPI_UNIVERSE},
            "ai_auto_add": False,
            "ai_auto_add_threshold": 3.0
        }


def save_watchlist_data(data: dict) -> None:
    try:
        init_db()
        with connect_db() as conn:
            if "ai_auto_add" in data:
                ai_auto_add_val = "1" if data["ai_auto_add"] else "0"
                conn.execute(
                    "INSERT OR REPLACE INTO watchlist_settings (key, value) VALUES ('ai_auto_add', ?)",
                    (ai_auto_add_val,)
                )
            
            if "ai_auto_add_threshold" in data:
                conn.execute(
                    "INSERT OR REPLACE INTO watchlist_settings (key, value) VALUES ('ai_auto_add_threshold', ?)",
                    (str(float(data["ai_auto_add_threshold"])),)
                )
            
            if "symbols" in data:
                conn.execute("DELETE FROM watchlist")
                ts = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
                for s in data["symbols"]:
                    name = STOCK_NAMES.get(s, "우량 종목")
                    conn.execute(
                        "INSERT OR IGNORE INTO watchlist (symbol, name, created_at) VALUES (?, ?, ?)",
                        (s, name, ts)
                    )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save watchlist to DB: {e}")


def save_daily_charts(symbol: str, data: list[dict]) -> None:
    """symbol에 해당하는 차트 목록을 daily_charts 테이블에 저장한다."""
    try:
        init_db()
        with connect_db() as conn:
            for row in data:
                date_str = row.get("date") or row.get("stck_bsop_date")
                # KIS API 날짜 포맷 'YYYYMMDD'을 'YYYY-MM-DD'로 규격화
                if date_str and len(date_str) == 8 and date_str.isdigit():
                    date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
                
                if not date_str:
                    continue
                
                open_val = float(row.get("open") or row.get("stck_opn_prpr") or 0.0)
                high_val = float(row.get("high") or row.get("stck_hgpr") or 0.0)
                low_val = float(row.get("low") or row.get("stck_lwpr") or 0.0)
                close_val = float(row.get("close") or row.get("stck_clpr") or row.get("stck_prpr") or 0.0)
                vol_val = float(row.get("volume") or row.get("acml_vol") or 0.0)
                
                conn.execute(
                    """
                    INSERT OR REPLACE INTO daily_charts (symbol, date, open, high, low, close, volume)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (symbol, date_str, open_val, high_val, low_val, close_val, vol_val)
                )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save daily charts for {symbol} to DB: {e}")


def load_daily_charts(symbol: str, limit: int = 120) -> list[dict]:
    """symbol에 해당하는 일별 차트 데이터를 날짜 정렬하여 로드한다."""
    try:
        init_db()
        with connect_db() as conn:
            c = conn.execute(
                """
                SELECT date, open, high, low, close, volume 
                FROM daily_charts 
                WHERE symbol = ? 
                ORDER BY date ASC
                """,
                (symbol,)
            )
            rows = c.fetchall()
            charts = []
            for r in rows:
                charts.append({
                    "date": r[0],
                    "open": r[1],
                    "high": r[2],
                    "low": r[3],
                    "close": r[4],
                    "volume": r[5]
                })
            if len(charts) > limit:
                charts = charts[-limit:]
            return charts
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load daily charts for {symbol} from DB: {e}")
        return []


def get_watchlist_extra_info(symbol: str) -> dict:
    """관심종목의 최신 분석 점수, 이유, 현재가, 거래량, 등락률, RSI, 갱신시각, 이평선 추세를 DB 캐시에서 조회해 반환한다."""
    init_db()
    res = {
        "score": None,
        "reason": "분석 정보 없음",
        "price": None,
        "volume": None,
        "change_rate": None,
        "rsi": None,
        "updated_at": None,
        "sma_trend": "데이터 없음"
    }
    try:
        with connect_db() as conn:
            # 1. scanned_candidates 테이블에서 최신 스코어, 이유, 가격, RSI, 갱신시각 조회
            c_cand = conn.execute(
                """
                SELECT score, reasons, price, rsi, scanned_at 
                FROM scanned_candidates 
                WHERE symbol = ? 
                ORDER BY scanned_at DESC 
                LIMIT 1
                """,
                (symbol,)
            )
            row_cand = c_cand.fetchone()
            if row_cand:
                res["score"] = row_cand[0]
                res["reason"] = row_cand[1] or "조건 미지정"
                res["price"] = row_cand[2]
                res["rsi"] = row_cand[3]
                res["updated_at"] = row_cand[4]
                
            # 2. daily_charts 테이블에서 최신 가격, 전일 대비 등락률, 거래량 조회
            c_chart = conn.execute(
                """
                SELECT close, volume, date 
                FROM daily_charts 
                WHERE symbol = ? 
                ORDER BY date DESC 
                LIMIT 2
                """,
                (symbol,)
            )
            rows_chart = c_chart.fetchall()
            if rows_chart:
                latest_chart = rows_chart[0]
                try:
                    current_price = float(res["price"]) if res["price"] is not None else None
                except (TypeError, ValueError):
                    current_price = None
                if current_price is None or current_price <= 0:
                    res["price"] = latest_chart[0]
                res["volume"] = latest_chart[1]
                if res["updated_at"] is None:
                    res["updated_at"] = latest_chart[2]
                
                # 등락률 계산
                if len(rows_chart) >= 2:
                    curr_close = rows_chart[0][0]
                    prev_close = rows_chart[1][0]
                    if prev_close > 0:
                        res["change_rate"] = round(((curr_close - prev_close) / prev_close) * 100, 2)
                        
            # 3. 이동평균 상태 계산 (SMA20 vs SMA60)
            c_ma = conn.execute(
                """
                SELECT close 
                FROM daily_charts 
                WHERE symbol = ? 
                ORDER BY date DESC 
                LIMIT 60
                """,
                (symbol,)
            )
            rows_ma = [r[0] for r in c_ma.fetchall()]
            if len(rows_ma) >= 60:
                rows_ma.reverse()  # 오래된 순 정렬
                sma20 = sum(rows_ma[-20:]) / 20
                sma60 = sum(rows_ma[-60:]) / 60
                curr_price = rows_ma[-1]
                
                if sma20 > sma60:
                    if curr_price > sma20:
                        res["sma_trend"] = "정배열 (상승)"
                    else:
                        res["sma_trend"] = "정배열 (조정)"
                else:
                    if curr_price > sma20:
                        res["sma_trend"] = "반등 시도"
                    else:
                        res["sma_trend"] = "역배열 (하락)"
            elif len(rows_ma) >= 20:
                rows_ma.reverse()
                sma20 = sum(rows_ma[-20:]) / 20
                curr_price = rows_ma[-1]
                if curr_price > sma20:
                    res["sma_trend"] = "20일선 위"
                else:
                    res["sma_trend"] = "20일선 아래"
            else:
                res["sma_trend"] = "자료 부족"
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to get watchlist extra info for {symbol}: {e}")
    return res


def sync_custom_rules_to_db(conn) -> None:
    from src.db.strategy_repository import (
        _default_strategy_profile,
        strategy_profile_hash,
    )

    import importlib.util
    import inspect
    import sys
    import json
    from pathlib import Path

    custom_dir = Path("src/strategy/custom_rules")
    if not custom_dir.exists():
        return

    project_root = str(Path("src").resolve().parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    for py_file in custom_dir.glob("*.py"):
        if py_file.name == "__init__.py":
            continue
        try:
            module_name = f"src.strategy.custom_rules.{py_file.stem}"
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if not spec or not spec.loader:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            for name, obj in inspect.getmembers(module, inspect.isclass):
                if obj.__module__ != module_name or "Strategy" not in name:
                    continue

                strat_id = py_file.stem
                doc = obj.__doc__ or ""
                doc_lines = [line.strip() for line in doc.split("\n") if line.strip()]
                strat_name = doc_lines[0] if doc_lines else name
                if not strat_name.startswith(CUSTOM_STRATEGY_PREFIXES):
                    strat_name = f"\uc0ac\uc6a9\uc790\uc804\ub7b5 {strat_name}"

                description = " ".join(doc_lines[1:]) if len(doc_lines) > 1 else doc
                if not description:
                    description = f"{py_file.name}\uc5d0\uc11c \ubd88\ub7ec\uc628 \uc0ac\uc6a9\uc790 \uc815\uc758 \uc804\ub7b5\uc785\ub2c8\ub2e4."

                c = conn.execute("SELECT id FROM ai_strategies WHERE id = ?", (strat_id,))
                row = c.fetchone()

                profile = _default_strategy_profile({
                    "id": strat_id,
                    "provider": "none",
                    "model": strat_id,
                    "weight": 0.0,
                })
                profile_json = json.dumps(profile, ensure_ascii=False, sort_keys=True)
                profile_hash = strategy_profile_hash(profile)

                if not row:
                    conn.execute(
                        """
                        INSERT INTO ai_strategies (
                            id, name, provider, model, weight, description, selected,
                            status, profile_json, strategy_version, profile_hash
                        )
                        VALUES (?, ?, 'none', ?, 0.0, ?, 0, 'verified', ?, 1, ?)
                        """,
                        (strat_id, strat_name, strat_id, description, profile_json, profile_hash),
                    )
                    logger.info(f"Registered new custom strategy: {strat_id} ({strat_name})")
                else:
                    conn.execute(
                        """
                        UPDATE ai_strategies
                        SET name = ?,
                            provider = 'none',
                            model = ?,
                            description = ?,
                            profile_json = ?,
                            profile_hash = ?
                        WHERE id = ?
                        """,
                        (strat_name, strat_id, description, profile_json, profile_hash, strat_id),
                    )
        except (sqlite3.Error, OSError, ValueError, TypeError) as e:
            logger.warning(f"Error loading custom strategy file {py_file.name}: {e}")


import functools

@functools.lru_cache(maxsize=128)
def get_custom_strategy_instance(strategy_id: str):
    import importlib.util
    import inspect
    import sys
    from pathlib import Path

    custom_dir = Path("src/strategy/custom_rules")
    py_file = custom_dir / f"{strategy_id}.py"
    if not py_file.exists():
        return None
        
    try:
        module_name = f"src.strategy.custom_rules.{strategy_id}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if not spec or not spec.loader:
            return None
        module = importlib.util.module_from_spec(spec)
        project_root = str(Path("src").resolve().parent)
        if project_root not in sys.path:
            sys.path.insert(0, project_root)
        spec.loader.exec_module(module)
        
        for name, obj in inspect.getmembers(module, inspect.isclass):
            if obj.__module__ == module_name and "Strategy" in name:
                return obj()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load custom strategy instance for {strategy_id}: {e}")
    return None


def get_watchlist_setting(key: str, default: str) -> str:
    try:
        init_db()
        with connect_db() as conn:
            c = conn.execute("SELECT value FROM watchlist_settings WHERE key = ?", (key,))
            row = c.fetchone()
            if row:
                return row[0]
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to load watchlist setting {key}: {e}")
    return default


def save_watchlist_setting(key: str, value: str) -> None:
    try:
        init_db()
        with connect_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO watchlist_settings (key, value) VALUES (?, ?)",
                (key, str(value))
            )
            conn.commit()
    except (sqlite3.Error, OSError, ValueError, TypeError) as e:
        logger.warning(f"Failed to save watchlist setting {key}: {e}")
__all__ = ['KST', 'WATCHLIST_FILE', 'STOCK_NAMES', 'STOCK_SECTORS', 'STOCK_SECTORS', 'KOSPI_UNIVERSE', 'KOSPI_UNIVERSE', 'load_watchlist_data', 'save_watchlist_data', 'save_daily_charts', 'load_daily_charts', 'get_watchlist_extra_info', 'sync_custom_rules_to_db', 'get_custom_strategy_instance', 'get_watchlist_setting', 'save_watchlist_setting']
