import json
import hashlib
from pathlib import Path

import requests

from src.config import config
from src.strategy.features import FEATURE_VERSION, feature_contributions
from src.utils.logger import logger
from src.utils.openai_guard import record_rate_limit, require_available


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v) for v in value if v]


class ModelPredictor:
    def __init__(self, strategy_profile: dict | None = None, description: str = ""):
        self.enabled = bool(getattr(config, "ai_strategy_enabled", False))

        profile = strategy_profile if isinstance(strategy_profile, dict) else {}
        self.strategy_profile = profile
        self.strategy_description = str(description or profile.get("description", "") or "").strip()

        # AI 가중치는 profile.ai_weight를 우선하되 risk.max_ai_weight로 상한을 둔다.
        weight = _as_float(profile.get("ai_weight"), _as_float(getattr(config, "ai_score_weight", 0.4), 0.4))
        risk = profile.get("risk") if isinstance(profile.get("risk"), dict) else {}
        max_ai_weight = risk.get("max_ai_weight")
        if max_ai_weight is not None:
            weight = min(weight, _as_float(max_ai_weight, weight))
        self.score_weight = max(0.0, min(1.0, weight))

        self.min_confidence = _as_float(
            profile.get("min_ai_confidence"),
            _as_float(getattr(config, "ai_min_model_confidence", 0.6), 0.6),
        )

        # 후보 탐색 단계에서 참조하는 전략 정책 필드.
        self.strategy_type = str(profile.get("strategy_type", "") or "")
        self.risk_level = str(profile.get("risk_level", "") or "")
        self.focus = _as_str_list(profile.get("focus"))
        self.avoid = _as_str_list(profile.get("avoid"))
        self.min_rule_score_for_ai = _as_float(
            profile.get("min_rule_score_for_ai"),
            _as_float(getattr(config, "ai_min_rule_score", 1.5), 1.5),
        )
        self.allow_candidate_promotion = bool(
            profile.get("allow_candidate_promotion")
            if profile.get("allow_candidate_promotion") is not None
            else getattr(config, "ai_allow_candidate_promotion", False)
        )

        self.provider = "openai_responses"
        self.model_name = str(getattr(config, "openai_model", "gpt-5-mini") or "gpt-5-mini")
        self.api_key = str(getattr(config, "openai_api_key", "") or "").strip()
        self.timeout_seconds = float(getattr(config, "openai_timeout_seconds", 20.0) or 20.0)
        self.cache_path = Path(".runtime") / "openai_ai_cache.json"
        self.cache_ttl_seconds = 86400
        self.model_status = "ready" if self.api_key else "fallback"
        self.fallback_reason = "" if self.api_key else "OPENAI_API_KEY not configured"

        # 전략 시그니처: 전략 성격이 다르면 캐시/프롬프트가 달라지도록 한다.
        self.strategy_signature = self._build_strategy_signature()

    def _build_strategy_signature(self) -> str:
        payload = json.dumps(
            {
                "strategy_type": self.strategy_type,
                "risk_level": self.risk_level,
                "focus": sorted(self.focus),
                "avoid": sorted(self.avoid),
                "description": self.strategy_description,
                "score_weight": round(self.score_weight, 4),
                "min_confidence": round(self.min_confidence, 4),
                "model": self.model_name,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def _strategy_instructions(self) -> str:
        lines = [
            "You score a Korean stock candidate from 0.0 to 1.0. "
            "Return JSON only with keys probability and rationale. "
            "Probability means short-term buy quality confidence."
        ]
        if self.strategy_type:
            lines.append(f"Strategy type: {self.strategy_type}.")
        if self.risk_level:
            lines.append(f"Risk posture: {self.risk_level}.")
        if self.focus:
            lines.append("Reward candidates showing these signals: " + ", ".join(self.focus) + ".")
        if self.avoid:
            lines.append("Penalize candidates showing these signals: " + ", ".join(self.avoid) + ".")
        if self.strategy_description:
            lines.append("Strategy intent from the operator: " + self.strategy_description[:500])
        return " ".join(lines)

    def _cache_key(self, features: dict) -> str:
        payload = json.dumps(
            {
                "provider": self.provider,
                "model": self.model_name,
                "strategy_signature": self.strategy_signature,
                "feature_version": features.get("feature_version", FEATURE_VERSION),
                "strategy_score": round(float(features.get("strategy_score", 0.0) or 0.0), 4),
                "rsi": round(float(features.get("rsi", 0.0) or 0.0), 4),
                "rsi2": round(float(features.get("rsi2", 0.0) or 0.0), 4),
                "macd_hist": round(float(features.get("macd_hist", 0.0) or 0.0), 6),
                "sma20_gap": round(float(features.get("sma20_gap", 0.0) or 0.0), 6),
                "sma60_gap": round(float(features.get("sma60_gap", 0.0) or 0.0), 6),
                "bb_position": round(float(features.get("bb_position", 0.0) or 0.0), 6),
                "return_5d": round(float(features.get("return_5d", 0.0) or 0.0), 6),
                "return_20d": round(float(features.get("return_20d", 0.0) or 0.0), 6),
                "volatility_20d": round(float(features.get("volatility_20d", 0.0) or 0.0), 6),
                "volume_ratio_20d": round(float(features.get("volume_ratio_20d", 0.0) or 0.0), 6),
                "max_drawdown_20d": round(float(features.get("max_drawdown_20d", 0.0) or 0.0), 6),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _load_cache(self) -> dict:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save_cache(self, cache: dict) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.info(f"[AI] cache write skipped: {e}")

    def _prompt_payload(self, features: dict, contributions: list[dict]) -> str:
        compact = {
            "strategy_score": round(float(features.get("strategy_score", 0.0) or 0.0), 4),
            "rsi": round(float(features.get("rsi", 0.0) or 0.0), 4),
            "rsi2": round(float(features.get("rsi2", 0.0) or 0.0), 4),
            "macd_hist": round(float(features.get("macd_hist", 0.0) or 0.0), 6),
            "sma20_gap": round(float(features.get("sma20_gap", 0.0) or 0.0), 6),
            "sma60_gap": round(float(features.get("sma60_gap", 0.0) or 0.0), 6),
            "bb_position": round(float(features.get("bb_position", 0.0) or 0.0), 6),
            "return_5d": round(float(features.get("return_5d", 0.0) or 0.0), 6),
            "return_20d": round(float(features.get("return_20d", 0.0) or 0.0), 6),
            "volatility_20d": round(float(features.get("volatility_20d", 0.0) or 0.0), 6),
            "volume_ratio_20d": round(float(features.get("volume_ratio_20d", 0.0) or 0.0), 6),
            "max_drawdown_20d": round(float(features.get("max_drawdown_20d", 0.0) or 0.0), 6),
            "top_features": contributions,
        }
        return json.dumps(compact, ensure_ascii=True)

    def _predict_probability(self, features: dict, contributions: list[dict]) -> float:
        payload = {
            "model": self.model_name,
            "instructions": self._strategy_instructions(),
            "input": self._prompt_payload(features, contributions),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "stock_candidate_score",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "probability": {"type": "number", "minimum": 0, "maximum": 1},
                            "rationale": {"type": "string"},
                        },
                        "required": ["probability", "rationale"],
                        "additionalProperties": False,
                    },
                }
            },
        }
        from src.online_access import require_online_access

        require_online_access("OpenAI prediction")
        require_available()
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        if response.status_code == 429:
            record_rate_limit(response.headers.get("Retry-After"))
        response.raise_for_status()
        body = response.json()
        
        # Track OpenAI API token usage
        try:
            usage = body.get("usage") or {}
            prompt_tokens = usage.get("prompt_tokens")
            completion_tokens = usage.get("completion_tokens")
            total_tokens = usage.get("total_tokens")
            
            # Fallback if usage metadata is missing in proxy response
            if not prompt_tokens or not completion_tokens:
                prompt_tokens = 850
                completion_tokens = 150
                total_tokens = prompt_tokens + completion_tokens
                
            from src.db.repository import update_token_usage
            update_token_usage(prompt_tokens, completion_tokens, total_tokens)
        except Exception as ue:
            logger.warning(f"[AI] Failed to track token usage: {ue}")

        output_text = body.get("output_text", "")
        if not output_text:
            raise ValueError("OpenAI response missing output_text")
        parsed = json.loads(output_text)
        return max(0.0, min(1.0, float(parsed["probability"])))

    def predict(self, features: dict) -> dict:
        rule_score = float(features.get("strategy_score", 0.0) or 0.0)
        contributions = feature_contributions(features)
        cache_key = self._cache_key(features)
        result = {
            "rule_score": rule_score,
            "ml_score": None,
            "final_score": rule_score,
            "ai_enabled": self.enabled,
            "model_status": self.model_status,
            "model_version": self.model_name,
            "provider": self.provider,
            "feature_version": features.get("feature_version", FEATURE_VERSION),
            "score_weight": 0.0,
            "fallback_reason": None,
            "top_features": contributions,
        }

        if not self.enabled:
            result["model_status"] = "disabled"
            result["fallback_reason"] = "AI_STRATEGY_ENABLED=false"
            return result

        if not self.api_key:
            result["fallback_reason"] = self.fallback_reason
            return result

        cache = self._load_cache()
        cached = cache.get(cache_key)
        if cached and isinstance(cached, dict):
            result.update(
                {
                    "ml_score": cached.get("ml_score"),
                    "final_score": cached.get("final_score", rule_score),
                    "score_weight": cached.get("score_weight", self.score_weight),
                    "model_status": cached.get("model_status", "ready"),
                    "fallback_reason": cached.get("fallback_reason"),
                }
            )
            return result

        try:
            probability = self._predict_probability(features, contributions)
            confident = probability >= self.min_confidence
            if confident:
                # 신뢰도가 기준 이상일 때만 AI 점수를 룰 점수와 블렌딩한다.
                model_component = probability * 5.0
                final_score = (rule_score * (1 - self.score_weight)) + (model_component * self.score_weight)
                applied_weight = self.score_weight
                model_status = "ready"
                low_conf_reason = None
            else:
                # 신뢰도 미달이면 AI를 신뢰하지 않고 룰 점수로 폴백한다.
                # → 저신뢰 AI가 후보를 승격/강등시키지 못한다.
                final_score = rule_score
                applied_weight = 0.0
                model_status = "low_confidence"
                low_conf_reason = (
                    f"AI confidence {probability:.2f} < min_ai_confidence "
                    f"{self.min_confidence:.2f}; using rule score"
                )
            cache[cache_key] = {
                "ml_score": probability,
                "final_score": final_score,
                "score_weight": applied_weight,
                "model_status": model_status,
                "fallback_reason": low_conf_reason,
            }
            self._save_cache(cache)
            result.update(
                {
                    "ml_score": probability,
                    "final_score": final_score,
                    "score_weight": applied_weight,
                    "model_status": model_status,
                    "fallback_reason": low_conf_reason,
                }
            )
        except Exception as e:
            logger.error(f"[AI] OpenAI prediction error: {e}")
            result["model_status"] = "fallback"
            result["fallback_reason"] = str(e)
        return result

    def predict_score(self, features: dict) -> float:
        return float(self.predict(features)["final_score"])
