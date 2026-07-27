import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, patch

import pandas as pd
from src.config import config
from src.db.repository import save_daily_charts, load_daily_charts, connect_db, init_db
from src.strategy.seven_split import find_candidates

class TestHybridScanner(unittest.TestCase):
    def setUp(self):
        init_db()
        # 이전 캐시 클렌징
        with connect_db() as conn:
            conn.execute("DELETE FROM daily_charts WHERE symbol = '005930'")
            conn.commit()

    def test_daily_charts_persistence(self):
        # 1. Mock 데이터 준비
        mock_data = [
            {"date": "20260520", "open": 50000, "high": 51000, "low": 49000, "close": 50500, "volume": 1000000},
            {"date": "20260521", "open": 50600, "high": 51500, "low": 50100, "close": 51200, "volume": 1200000},
        ]
        
        # 2. 저장 테스트
        save_daily_charts("005930", mock_data)
        
        # 3. 로딩 테스트
        loaded = load_daily_charts("005930")
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[0]["date"], "2026-05-20")
        self.assertEqual(loaded[1]["close"], 51200.0)

    def test_hybrid_fallback_scans(self):
        # 최소 60개 데이터가 있어야 스캔을 건너뛰지 않으므로 60개 Mock 데이터 생성
        mock_data = []
        start_date = datetime(2026, 1, 1)
        for i in range(70):
            d_str = (start_date + timedelta(days=i)).strftime("%Y%m%d")
            mock_data.append({
                "date": d_str,
                "open": 50000 + i*10,
                "high": 50500 + i*10,
                "low": 49500 + i*10,
                "close": 50100 + i*10,
                "volume": 100000
            })
            
        save_daily_charts("005930", mock_data)
        
        # yfinance를 일부러 mock이나 bypass해서 Fallback DB가 도는지 테스트
        # universe=['005930'] 로 지정해 스캔 테스트
        api = Mock()
        api.get_daily.return_value = []
        with patch("src.strategy.seven_split.yf.download", return_value=pd.DataFrame()):
            res = find_candidates(
                held_symbols=set(),
                universe=["005930"],
                min_score=0,
                ranker="rule_only",
                api=api,
            )
        
        self.assertIn("scanned", res)
        # 005930이 정상적으로 1종목 scanned 완료되었는지 검증
        self.assertEqual(res["scanned"], 1)

    def test_kis_scan_source_skips_yfinance_and_uses_api(self):
        mock_data = []
        start_date = datetime(2026, 1, 1)
        for i in range(70):
            d_str = (start_date + timedelta(days=i)).strftime("%Y%m%d")
            mock_data.append({
                "date": d_str,
                "open": 50000 + i * 10,
                "high": 50500 + i * 10,
                "low": 49500 + i * 10,
                "close": 50100 + i * 10,
                "volume": 100000,
            })

        api = Mock()
        api.get_daily.return_value = mock_data
        with patch.object(config, "candidate_scan_source", "kis"), patch(
            "src.strategy.seven_split.yf.download"
        ) as yfinance_download:
            res = find_candidates(
                held_symbols=set(),
                universe=["005930"],
                min_score=0,
                ranker="rule_only",
                api=api,
            )

        yfinance_download.assert_not_called()
        api.get_daily.assert_called_once_with("005930", n=120)
        self.assertEqual(res["scanned"], 1)

if __name__ == "__main__":
    unittest.main()
