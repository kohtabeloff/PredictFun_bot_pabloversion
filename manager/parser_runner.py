"""
Логика парсера маркетов Predict.fun для менеджера.
Запускается в отдельном потоке, шаги сообщаются через step_callback.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from typing import Any, Callable

import requests

BASE_URL = "https://api.predict.fun"
POLYMARKET_CLOB_API = "https://clob.polymarket.com"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Запасной список на случай если API недоступен
TAGS_FALLBACK = [
    {"name": "Sports",   "id": "4"},
    {"name": "Politics", "id": "1"},
    {"name": "Crypto",   "id": "2"},
    {"name": "Culture",  "id": "13"},
    {"name": "Economy",  "id": "6"},
    {"name": "Finance",  "id": "11"},
    {"name": "Esports",  "id": "83"},
    {"name": "NBA",      "id": "78"},
    {"name": "NFL",      "id": "45"},
    {"name": "NHL",      "id": "79"},
    {"name": "Soccer",   "id": "14"},
    {"name": "CS2",      "id": "93"},
    {"name": "LoL",      "id": "84"},
    {"name": "Dota 2",   "id": "96"},
    {"name": "Cricket",  "id": "97"},
    {"name": "Olympics", "id": "94"},
    {"name": "NCAAM",    "id": "82"},
    {"name": "Oscars",   "id": "87"},
]

DATE_FIELDS = ["boostEndsAt", "endDate", "resolutionDate"]

# Теги, которые не имеют смысла в интерфейсе фильтрации
_SKIP_TAG_PREFIXES = ("x -",)
_SKIP_TAG_NAMES = {
    "Up/Down", "Hit Price", "Price Range", "Price Above",
    "UpDown_Binance", "HitPrice_Binance", "HoK",
    "15 Min", "1 Hour", "Daily", "5 Min",
}

_STOP_WORDS = {
    "will", "the", "a", "an", "in", "on", "by", "to", "of", "for",
    "is", "be", "or", "and", "at", "this", "that", "with", "from",
    "it", "its", "are", "was", "were", "has", "have", "had", "not",
    "any", "all", "more", "than", "before", "after", "when", "if",
    "can", "could", "would", "should", "may", "get", "out", "over",
    "new", "year", "month", "day", "end", "first", "last", "ever",
    "who", "what", "which", "how", "their", "they", "them", "his",
    "her", "our", "your", "win", "lose", "make", "take", "come",
}

_poly_date_cache: dict[str, date] = {}
_CACHE_MISS = object()  # sentinel для отличия "не кэшировано" от None

StepCallback = Callable[[int, str, str], None]


def fetch_tags(api_key: str) -> list[dict]:
    """Загружает актуальные теги из API. Фильтрует технические и внутренние."""
    try:
        r = requests.get(
            f"{BASE_URL}/v1/tags",
            headers={"x-api-key": api_key},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if not data.get("success"):
            return TAGS_FALLBACK
        tags = []
        for tag in data.get("data") or []:
            name = tag.get("name", "")
            if any(name.startswith(p) for p in _SKIP_TAG_PREFIXES):
                continue
            if name in _SKIP_TAG_NAMES:
                continue
            tags.append({"name": name, "id": str(tag["id"])})
        return tags if tags else TAGS_FALLBACK
    except Exception:
        return TAGS_FALLBACK


# ── API запросы ───────────────────────────────────────────────────────────────

def _get_pages(api_key: str, path: str, extra_params: dict | None = None) -> list[dict]:
    """GET с пагинацией, возвращает все items."""
    items: list[dict] = []
    params: dict = {"first": "100", **(extra_params or {})}
    after: str | None = None
    while True:
        if after:
            params["after"] = after
        try:
            r = requests.get(
                f"{BASE_URL}{path}",
                headers={"x-api-key": api_key},
                params=params,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            raise RuntimeError(f"Ошибка API: {e}") from e
        if not data.get("success"):
            raise RuntimeError("API вернул success=false")
        batch = data.get("data") or []
        items.extend(batch)
        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        after = cursor
        params = {"first": "100", **(extra_params or {})}
    return items


def fetch_all_markets(api_key: str) -> list[dict]:
    return _get_pages(api_key, "/v1/markets")


def fetch_market_ids_by_tag(api_key: str, tag_id: str) -> set[int]:
    items = _get_pages(api_key, "/v1/markets", {"tagIds": tag_id})
    return {int(m["id"]) for m in items if m.get("id") is not None}


def fetch_markets_parallel(api_key: str, market_ids: list[int], max_workers: int = 10) -> dict[int, dict]:
    """Загружает данные по нескольким маркетам параллельно."""
    result: dict[int, dict] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(fetch_market, api_key, mid): mid for mid in market_ids}
        for future in as_completed(futures):
            mid = futures[future]
            try:
                data = future.result()
                if data:
                    result[mid] = data
            except Exception:
                pass
    return result


def fetch_market(api_key: str, market_id: int) -> dict | None:
    try:
        r = requests.get(
            f"{BASE_URL}/v1/markets/{market_id}",
            headers={"x-api-key": api_key},
            timeout=30,
        )
        if not r.ok:
            return None
        data = r.json()
        return data.get("data") if data.get("success") else None
    except Exception:
        return None


def parse_end_date(market: dict) -> date | None:
    for field in DATE_FIELDS:
        raw = market.get(field)
        if not raw:
            continue
        if isinstance(raw, date):
            return raw
        if isinstance(raw, datetime):
            return raw.date()
        s = str(raw).strip()
        if not s:
            continue
        try:
            return datetime.fromisoformat(re.sub(r"Z$", "+00:00", s)).date()
        except (ValueError, TypeError):
            continue
    return None


# ── Фильтры ───────────────────────────────────────────────────────────────────

def _market_ok(
    market: dict | None,
    require_status: str | None,
    min_days: int | None,
    today: date,
) -> bool:
    if market is None:
        return require_status is None
    if require_status:
        if (market.get("status") or "").upper() != require_status.upper():
            return False
    if min_days and min_days > 0:
        end = parse_end_date(market)
        if end is None or (end - today).days < min_days:
            return False
    return True


def _polymarket_end_date(condition_id: str) -> date | None:
    cached = _poly_date_cache.get(condition_id, _CACHE_MISS)
    if cached is not _CACHE_MISS:
        return cached  # type: ignore[return-value]
    try:
        r = requests.get(f"{POLYMARKET_CLOB_API}/markets/{condition_id}", timeout=15)
        if r.status_code != 200:
            return None
        raw = r.json().get("end_date_iso")
        if not raw:
            return None
        end = date.fromisoformat(str(raw).strip()[:10])
        _poly_date_cache[condition_id] = end
        return end
    except Exception:
        return None


def _kalshi_has_market(title: str, min_matches: int = 2) -> bool:
    words = re.findall(r"\b[a-zA-Z]{3,}\b", title)
    keywords = [w.lower() for w in words if w.lower() not in _STOP_WORDS]
    if len(keywords) < 2:
        return True
    query = " ".join(keywords[:4])
    try:
        r = requests.get(
            f"{KALSHI_BASE}/events",
            params={"title": query, "status": "open", "limit": 10},
            timeout=15,
        )
        if r.status_code != 200:
            return True
        events = r.json().get("events") or []
        if not events:
            return False
        kw_set = set(keywords)
        for ev in events:
            ev_title = (ev.get("title") or ev.get("sub_title") or "").lower()
            ev_words = set(re.findall(r"\b[a-zA-Z]{3,}\b", ev_title))
            if len(kw_set & ev_words) >= min_matches:
                return True
        return False
    except Exception:
        return True


def _apply_kalshi_filter(
    market_ids: list[int],
    market_data: dict[int, dict],
    min_days: int | None,
    max_workers: int = 8,
) -> tuple[list[int], int]:
    today = date.today()
    keep: set[int] = set()

    # Маркеты с прямой ссылкой на Polymarket — проверяем дату параллельно
    poly_ids = []
    kalshi_ids = []
    for mid in market_ids:
        mdata = market_data.get(mid) or {}
        condition_ids = mdata.get("polymarketConditionIds") or []
        if condition_ids:
            poly_ids.append(mid)
        else:
            title = mdata.get("question") or mdata.get("title") or ""
            if not title:
                keep.add(mid)  # нет заголовка — не фильтруем
            else:
                kalshi_ids.append(mid)

    # Polymarket: параллельная проверка дат
    def _check_poly(mid: int) -> bool:
        mdata = market_data.get(mid) or {}
        condition_ids = mdata.get("polymarketConditionIds") or []
        if min_days and min_days > 0 and condition_ids:
            end = _polymarket_end_date(condition_ids[0])
            if end is None or (end - today).days < min_days:
                return False
        return True

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for mid, ok in zip(poly_ids, pool.map(_check_poly, poly_ids)):
            if ok:
                keep.add(mid)

    # Kalshi: параллельная проверка через текстовый поиск
    def _check_kalshi(mid: int) -> bool:
        mdata = market_data.get(mid) or {}
        if min_days and min_days > 0:
            end = parse_end_date(mdata)
            if end is None or (end - today).days < min_days:
                return False
        title = mdata.get("question") or mdata.get("title") or ""
        return _kalshi_has_market(title)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        for mid, ok in zip(kalshi_ids, pool.map(_check_kalshi, kalshi_ids)):
            if ok:
                keep.add(mid)

    result = [mid for mid in market_ids if mid in keep]
    return result, len(market_ids) - len(result)


# ── Пайплайн ──────────────────────────────────────────────────────────────────

def run_pipeline(
    api_key: str,
    use_all_markets: bool,
    market_ids_input: list[int] | None,
    exclude_tag_ids: list[str],
    exclude_tag_names: list[str],
    require_status: str | None,
    min_days: int | None,
    use_kalshi: bool,
    step_callback: StepCallback,
) -> tuple[list[int], str | None]:
    """
    Запускает пайплайн фильтрации.
    Шаги:
      1        — загрузка маркетов
      2..K+1   — исключение по тегам (по одному шагу на тег)
      K+2      — вычитание
      K+3      — фильтр статус/дата
      K+4      — фильтр Kalshi (если включён)
    """
    try:
        today = date.today()
        markets_by_id: dict[int, dict] = {}
        K = len(exclude_tag_ids)

        # Шаг 1 — загружаем маркеты
        step_callback(1, "running", "")
        if use_all_markets:
            all_markets = fetch_all_markets(api_key)
            markets_by_id = {int(m["id"]): m for m in all_markets if m.get("id") is not None}
            market_ids = list(markets_by_id.keys())
            step_callback(1, "done", f"Загружено {len(market_ids)} маркетов")
        else:
            if not market_ids_input:
                return [], "Список маркетов пуст"
            market_ids = market_ids_input
            step_callback(1, "done", f"Загружено {len(market_ids)} id")

        if not market_ids:
            return [], "Список маркетов пуст"

        # Шаги 2..K+1 — исключаем по тегам (параллельно)
        ids_to_remove: set[int] = set()
        for i in range(K):
            step_callback(2 + i, "running", "")

        def _fetch_tag(args: tuple) -> tuple[int, str, str, set[int]]:
            i, tag_id, name = args
            tag_ids = fetch_market_ids_by_tag(api_key, tag_id)
            return i, tag_id, name, tag_ids

        tag_args = [
            (i, exclude_tag_ids[i], exclude_tag_names[i] if i < len(exclude_tag_names) else exclude_tag_ids[i])
            for i in range(K)
        ]
        if tag_args:
            with ThreadPoolExecutor(max_workers=min(K, 8)) as pool:
                for i, tag_id, name, tag_ids in pool.map(_fetch_tag, tag_args):
                    ids_to_remove |= tag_ids
                    step_callback(2 + i, "done", f"Найдено {len(tag_ids)} маркетов с тегом «{name}»")

        # Шаг K+2 — вычитаем
        step_callback(2 + K, "running", "")
        result = [mid for mid in market_ids if mid not in ids_to_remove]
        removed = len(market_ids) - len(result)
        step_callback(2 + K, "done", f"Исключено {removed}, осталось {len(result)}")

        # Шаг K+3 — фильтр по статусу/дате
        need_filter = bool(require_status) or (min_days and min_days > 0 and not use_kalshi)
        if need_filter:
            step_callback(3 + K, "running", "")
            # Догружаем маркеты, которых нет в кэше, параллельно
            missing = [mid for mid in result if mid not in markets_by_id]
            if missing:
                fetched = fetch_markets_parallel(api_key, missing)
                markets_by_id.update(fetched)
            filtered = [
                mid for mid in result
                if _market_ok(markets_by_id.get(mid), require_status, None if use_kalshi else min_days, today)
            ]
            removed2 = len(result) - len(filtered)
            result = filtered
            parts = []
            if require_status:
                parts.append(f"статус {require_status}")
            if min_days and not use_kalshi:
                parts.append(f">= {min_days} дн.")
            step_callback(3 + K, "done", f"Исключено {removed2} ({', '.join(parts)}), осталось {len(result)}")
        else:
            step_callback(3 + K, "skip", "Фильтры не заданы")

        # Шаг K+4 — фильтр Kalshi/Polymarket
        if use_kalshi:
            step_callback(4 + K, "running", "")
            missing2 = [mid for mid in result if mid not in markets_by_id]
            if missing2:
                fetched2 = fetch_markets_parallel(api_key, missing2)
                markets_by_id.update(fetched2)
            result, removed3 = _apply_kalshi_filter(result, markets_by_id, min_days)
            step_callback(4 + K, "done", f"Исключено {removed3}, осталось {len(result)}")

        return result, None

    except Exception as e:
        return [], str(e)
