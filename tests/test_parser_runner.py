"""
Тесты для manager/parser_runner.py
"""
from __future__ import annotations

import sys
import types
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

import pytest

# Подменяем requests до импорта модуля, чтобы не делать реальных сетевых запросов
sys.modules.setdefault("requests", MagicMock())

from manager.parser_runner import (
    TAGS_FALLBACK,
    _apply_kalshi_filter,
    _market_ok,
    fetch_tags,
    parse_end_date,
    fetch_markets_parallel,
    run_pipeline,
    _get_pages,
    _poly_date_cache,
    _CACHE_MISS,
    _polymarket_end_date,
)


# ── parse_end_date ─────────────────────────────────────────────────────────────

class TestParseEndDate:
    def test_iso_string(self):
        d = parse_end_date({"endDate": "2025-12-31"})
        assert d == date(2025, 12, 31)

    def test_iso_with_time_and_z(self):
        d = parse_end_date({"endDate": "2025-06-15T00:00:00Z"})
        assert d == date(2025, 6, 15)

    def test_priority_order(self):
        # boostEndsAt берётся первым
        d = parse_end_date({"boostEndsAt": "2025-01-01", "endDate": "2025-12-31"})
        assert d == date(2025, 1, 1)

    def test_none_when_all_missing(self):
        assert parse_end_date({}) is None

    def test_skips_empty_string(self):
        d = parse_end_date({"boostEndsAt": "", "endDate": "2025-03-01"})
        assert d == date(2025, 3, 1)

    def test_invalid_format_skipped(self):
        d = parse_end_date({"boostEndsAt": "not-a-date", "endDate": "2025-09-09"})
        assert d == date(2025, 9, 9)


# ── _market_ok ─────────────────────────────────────────────────────────────────

class TestMarketOk:
    def test_none_market_passes_without_status(self):
        assert _market_ok(None, None, None, date.today()) is True

    def test_none_market_fails_with_status(self):
        assert _market_ok(None, "ACTIVE", None, date.today()) is False

    def test_status_filter_case_insensitive(self):
        market = {"status": "active"}
        assert _market_ok(market, "ACTIVE", None, date.today()) is True

    def test_status_filter_rejects_wrong(self):
        market = {"status": "RESOLVED"}
        assert _market_ok(market, "ACTIVE", None, date.today()) is False

    def test_min_days_filter_passes(self):
        future = date.today() + timedelta(days=30)
        market = {"endDate": future.isoformat()}
        assert _market_ok(market, None, 7, date.today()) is True

    def test_min_days_filter_rejects_soon(self):
        near = date.today() + timedelta(days=3)
        market = {"endDate": near.isoformat()}
        assert _market_ok(market, None, 7, date.today()) is False

    def test_no_end_date_passes_min_days(self):
        # Если у маркета нет даты — безопаснее отклонить его
        market = {"status": "ACTIVE"}
        assert _market_ok(market, None, 7, date.today()) is False


# ── fetch_tags ─────────────────────────────────────────────────────────────────

class TestFetchTags:
    def _make_response(self, tags_data, success=True):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"success": success, "data": tags_data}
        return resp

    def test_returns_fallback_on_exception(self):
        import requests as req
        req.get.side_effect = Exception("timeout")
        result = fetch_tags("test-key")
        assert result == TAGS_FALLBACK
        req.get.side_effect = None

    def test_returns_fallback_on_empty_list(self):
        import requests as req
        req.get.return_value = self._make_response([])
        result = fetch_tags("test-key")
        assert result == TAGS_FALLBACK

    def test_filters_skip_prefixes(self):
        import requests as req
        req.get.return_value = self._make_response([
            {"id": 1, "name": "Sports"},
            {"id": 2, "name": "x - Internal"},
        ])
        result = fetch_tags("test-key")
        names = [t["name"] for t in result]
        assert "Sports" in names
        assert "x - Internal" not in names

    def test_filters_skip_names(self):
        import requests as req
        req.get.return_value = self._make_response([
            {"id": 1, "name": "Politics"},
            {"id": 2, "name": "Up/Down"},
            {"id": 3, "name": "Daily"},
        ])
        result = fetch_tags("test-key")
        names = [t["name"] for t in result]
        assert "Politics" in names
        assert "Up/Down" not in names
        assert "Daily" not in names

    def test_returns_fallback_when_success_false(self):
        import requests as req
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"success": False, "data": [{"id": 1, "name": "Sports"}]}
        req.get.return_value = resp
        result = fetch_tags("test-key")
        assert result == TAGS_FALLBACK


# ── fetch_markets_parallel ─────────────────────────────────────────────────────

class TestFetchMarketsParallel:
    def test_handles_exception_in_future(self):
        """Если fetch_market бросает исключение — оно не должно падать наверх."""
        with patch("manager.parser_runner.fetch_market", side_effect=RuntimeError("oops")):
            result = fetch_markets_parallel("key", [1, 2, 3])
        assert result == {}

    def test_returns_successful_results(self):
        def fake_fetch(api_key, mid):
            return {"id": mid, "title": f"Market {mid}"}

        with patch("manager.parser_runner.fetch_market", side_effect=fake_fetch):
            result = fetch_markets_parallel("key", [10, 20])
        assert 10 in result
        assert 20 in result

    def test_skips_none_results(self):
        with patch("manager.parser_runner.fetch_market", return_value=None):
            result = fetch_markets_parallel("key", [1, 2])
        assert result == {}


# ── _get_pages ─────────────────────────────────────────────────────────────────

class TestGetPages:
    def _make_resp(self, data, cursor=None):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"success": True, "data": data, "cursor": cursor}
        return resp

    def test_single_page(self):
        import requests as req
        req.get.return_value = self._make_resp([{"id": 1}, {"id": 2}])
        result = _get_pages("key", "/v1/markets")
        assert len(result) == 2

    def test_stops_on_empty_batch_with_cursor(self):
        """Не уходит в бесконечный цикл если data=[] но cursor есть."""
        import requests as req
        req.get.reset_mock()
        req.get.return_value = self._make_resp([], cursor="abc123")
        result = _get_pages("key", "/v1/markets")
        assert result == []
        # Должен был вызван только один раз
        assert req.get.call_count == 1

    def test_paginates_correctly(self):
        import requests as req
        call_count = 0
        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return self._make_resp([{"id": 1}], cursor="page2")
            return self._make_resp([{"id": 2}], cursor=None)
        req.get.side_effect = side_effect
        result = _get_pages("key", "/v1/markets")
        assert len(result) == 2
        assert call_count == 2
        req.get.side_effect = None


# ── _apply_kalshi_filter ───────────────────────────────────────────────────────

class TestApplyKalshiFilter:
    def test_drops_ids_without_title(self):
        """Маркеты без заголовка должны отбрасываться — невозможно проверить через Kalshi."""
        market_data = {1: {}, 2: {"question": ""}}
        result, removed = _apply_kalshi_filter([1, 2], market_data, None)
        assert 1 not in result
        assert 2 not in result

    def test_removes_market_not_on_kalshi(self):
        import requests as req
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"events": []}
        req.get.return_value = resp

        market_data = {1: {"question": "Will Bitcoin reach 100k by year end?"}}
        result, removed = _apply_kalshi_filter([1], market_data, None)
        assert 1 not in result
        assert removed == 1

    def test_keeps_market_on_kalshi(self):
        import requests as req
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "events": [{"title": "Will Bitcoin reach 100k by end?"}]
        }
        req.get.return_value = resp

        market_data = {1: {"question": "Will Bitcoin reach 100k by end?"}}
        result, removed = _apply_kalshi_filter([1], market_data, None)
        assert 1 in result
        assert removed == 0

    def test_rejects_kalshi_match_without_date_when_min_days_set(self):
        market_data = {1: {"question": "Will Bitcoin reach 100k by end?"}}
        with patch("manager.parser_runner._kalshi_has_market", return_value=True):
            result, removed = _apply_kalshi_filter([1], market_data, 30)
        assert 1 not in result
        assert removed == 1

    def test_rejects_kalshi_match_with_soon_end_date(self):
        near = date.today() + timedelta(days=3)
        market_data = {1: {"question": "Will Bitcoin reach 100k by end?", "endDate": near.isoformat()}}
        with patch("manager.parser_runner._kalshi_has_market", return_value=True):
            result, removed = _apply_kalshi_filter([1], market_data, 30)
        assert 1 not in result
        assert removed == 1

    def test_keeps_kalshi_match_with_future_end_date(self):
        future = date.today() + timedelta(days=40)
        market_data = {1: {"question": "Will Bitcoin reach 100k by end?", "endDate": future.isoformat()}}
        with patch("manager.parser_runner._kalshi_has_market", return_value=True):
            result, removed = _apply_kalshi_filter([1], market_data, 30)
        assert 1 in result
        assert removed == 0


# ── run_pipeline ───────────────────────────────────────────────────────────────

class TestRunPipeline:
    def test_k0_no_crash(self):
        """Пустой список тегов не должен вызывать ValueError."""
        steps = []
        def cb(step, status, detail):
            steps.append((step, status))

        with patch("manager.parser_runner.fetch_all_markets", return_value=[{"id": 1}]):
            result, err = run_pipeline(
                api_key="key",
                use_all_markets=True,
                market_ids_input=None,
                exclude_tag_ids=[],
                exclude_tag_names=[],
                require_status=None,
                min_days=None,
                use_kalshi=False,
                step_callback=cb,
            )
        assert err is None
        assert result == [1]

    def test_empty_market_list_returns_error(self):
        steps = []
        result, err = run_pipeline(
            api_key="key",
            use_all_markets=False,
            market_ids_input=[],
            exclude_tag_ids=[],
            exclude_tag_names=[],
            require_status=None,
            min_days=None,
            use_kalshi=False,
            step_callback=lambda *a: steps.append(a),
        )
        assert err is not None
        assert result == []

    def test_status_filter_applied(self):
        steps = []
        markets = [
            {"id": 1, "status": "ACTIVE"},
            {"id": 2, "status": "RESOLVED"},
        ]

        with patch("manager.parser_runner.fetch_all_markets", return_value=markets):
            result, err = run_pipeline(
                api_key="key",
                use_all_markets=True,
                market_ids_input=None,
                exclude_tag_ids=[],
                exclude_tag_names=[],
                require_status="ACTIVE",
                min_days=None,
                use_kalshi=False,
                step_callback=lambda *a: steps.append(a),
            )
        assert err is None
        assert 1 in result
        assert 2 not in result


# ── _poly_date_cache sentinel ──────────────────────────────────────────────────

class TestPolydateCache:
    def test_cache_miss_sentinel_is_not_none(self):
        assert _CACHE_MISS is not None

    def test_failed_request_not_cached(self):
        """Ошибка API не должна кэшировать None — следующий запрос попробует снова."""
        _poly_date_cache.clear()
        import requests as req
        req.get.side_effect = Exception("timeout")
        _polymarket_end_date("test-condition-id")
        assert "test-condition-id" not in _poly_date_cache
        req.get.side_effect = None

    def test_successful_date_is_cached(self):
        _poly_date_cache.clear()
        import requests as req
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"end_date_iso": "2025-12-31T00:00:00Z"}
        req.get.return_value = resp

        result = _polymarket_end_date("cond-xyz")
        assert result == date(2025, 12, 31)
        assert _poly_date_cache.get("cond-xyz") == date(2025, 12, 31)
