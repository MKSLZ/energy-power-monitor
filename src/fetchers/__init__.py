"""fetchers 包：按主题聚合事件与行情。

对外唯一入口 fetch_all() -> {"events":[...], "quotes":[...], "source_status":[...]}
每个子模块独立 try/except，失败降级，绝不让整轮崩。
"""
from __future__ import annotations

from . import (china_coal_power, china_flash_news, europe_gas_power,
               intl_oil_macro, weather)
from .base import clean_events


def fetch_all() -> dict:
    events: list[dict] = []
    quotes: list[dict] = []
    status: list[dict] = []

    # 国际油气/宏观
    try:
        intl_quotes = intl_oil_macro.fetch_quotes()
        intl_news = clean_events(intl_oil_macro.fetch_news())
        quotes.extend(intl_quotes)
        events.extend(intl_news)
        status.extend(intl_oil_macro.fetch_status(intl_news, intl_quotes))
    except Exception as e:  # noqa: BLE001
        status.append({"chip": "未抓到", "chip_cls": "amb",
                       "content": "国际油气/宏观抓取异常", "data": str(e)[:120]})

    # 欧洲气与电
    try:
        eu_quotes = europe_gas_power.fetch_quotes()
        agsi = europe_gas_power.fetch_agsi()
        eu_news = clean_events(europe_gas_power.fetch_news())
        quotes.extend(eu_quotes)
        events.extend(eu_news)
        status.extend(europe_gas_power.fetch_status(agsi, eu_quotes, len(eu_news)))
    except Exception as e:  # noqa: BLE001
        status.append({"chip": "未抓到", "chip_cls": "amb",
                       "content": "欧洲气/电抓取异常", "data": str(e)[:120]})

    # 中国煤与电
    try:
        cn_quotes = china_coal_power.fetch_quotes()
        cn_news = clean_events(china_coal_power.fetch_news())
        quotes.extend(cn_quotes)
        events.extend(cn_news)
        status.extend(china_coal_power.fetch_status(cn_quotes, len(cn_news)))
    except Exception as e:  # noqa: BLE001
        status.append({"chip": "未抓到", "chip_cls": "amb",
                       "content": "中国煤/电抓取异常", "data": str(e)[:120]})

    # 国内快讯政策
    try:
        fl_news = clean_events(china_flash_news.fetch_news())
        events.extend(fl_news)
        status.extend(china_flash_news.fetch_status(len(fl_news)))
    except Exception as e:  # noqa: BLE001
        status.append({"chip": "未抓到", "chip_cls": "amb",
                       "content": "国内快讯抓取异常", "data": str(e)[:120]})

    # 天气水文
    try:
        wx_news = clean_events(weather.fetch_news())
        events.extend(wx_news)
        status.extend(weather.fetch_status(len(wx_news)))
    except Exception as e:  # noqa: BLE001
        status.append({"chip": "未抓到", "chip_cls": "amb",
                       "content": "天气/水文抓取异常", "data": str(e)[:120]})

    # 兜底：跨源再统一清洗去重一次（幂等）
    events = clean_events(events)
    return {"events": events, "quotes": quotes, "source_status": status}
