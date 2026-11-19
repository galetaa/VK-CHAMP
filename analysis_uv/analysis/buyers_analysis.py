#!/usr/bin/env python3
"""Read-only analysis of buyer demographics and regions in the supplied VK case.

Run with the primary runtime Python. The source XLSX is never saved or modified.
Produces buyers_results.json and buyers_findings.md beside this script by default.
Shares are fractions (0..1) in JSON. Differences named *_pp are percentage points.
Missing/suppressed cells remain null; only published numeric observations are summed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from openpyxl import load_workbook


METRICS = ["market_buyers", "market_orders", "partner_buyers", "partner_orders"]
COLUMNS = {
    "Пол": "gender", "Возраст": "age", "Регион": "region",
    "Покупатели: Маркет Вконтакте": "market_buyers",
    "Заказы: Маркет Вконтакте": "market_orders",
    "Покупатели: Партнёр 1": "partner_buyers",
    "Заказы: Партнёр 1": "partner_orders",
}
AGE_ORDER = ["до 18", "18–24", "25–34", "35–44", "45–54", "55+", "н/д"]
GENDER_ORDER = ["Ж", "М", "н/д"]


def clean(obj):
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if obj is pd.NA or obj is None:
        return None
    if isinstance(obj, (float, np.floating)):
        return None if not math.isfinite(obj) else float(obj)
    return obj


def recs(frame):
    return clean(frame.to_dict(orient="records"))


def divide(a, b):
    return a / b if pd.notna(a) and pd.notna(b) and b else np.nan


def load_table(source, name):
    frame = pd.read_excel(source, sheet_name=name).rename(columns=COLUMNS)
    frame.insert(0, "excel_row", np.arange(2, len(frame) + 2))
    for col in METRICS:
        frame[col] = pd.to_numeric(frame[col], errors="raise")
    # Source strings are retained, including its region nomenclature.
    return frame


def aggregate(frame, key, order=None):
    result = frame.groupby(key, sort=False)[METRICS].sum(min_count=1)
    for col in METRICS:
        result[col + "_missing_cells"] = frame.groupby(key, sort=False)[col].apply(lambda x: int(x.isna().sum()))
    if order:
        result = result.reindex(order)
    return result.reset_index()


def enrich(frame, totals):
    frame = frame.copy()
    for prefix in ["market", "partner"]:
        frame[prefix + "_orders_per_buyer"] = frame[prefix + "_orders"] / frame[prefix + "_buyers"]
        for metric in ["buyers", "orders"]:
            frame[prefix + "_" + metric + "_share"] = frame[prefix + "_" + metric] / totals[prefix + "_" + metric]
            frame[prefix + "_" + metric + "_rank"] = frame[prefix + "_" + metric].rank(ascending=False, method="min")
    for metric in ["buyers", "orders"]:
        frame[metric + "_composition_index"] = frame["market_" + metric + "_share"] / frame["partner_" + metric + "_share"]
        frame[metric + "_share_gap_pp"] = 100 * (frame["market_" + metric + "_share"] - frame["partner_" + metric + "_share"])
        frame["market_to_partner_" + metric + "_count_ratio"] = frame["market_" + metric] / frame["partner_" + metric]
    frame["orders_per_buyer_gap_market_minus_partner"] = frame.market_orders_per_buyer - frame.partner_orders_per_buyer
    return frame


def quality(frame, keys):
    return {
        "rows": len(frame),
        "duplicate_keys": recs(frame.loc[frame.duplicated(keys, keep=False), ["excel_row"] + keys]),
        "missing_key_cells": int(frame[keys].isna().sum().sum()),
        "missing_numeric_cells": {col: frame.loc[frame[col].isna(), "excel_row"].tolist() for col in METRICS},
        "negative_counts": {col: frame.loc[frame[col] < 0, "excel_row"].tolist() for col in METRICS},
        "zero_counts": {col: frame.loc[frame[col] == 0, "excel_row"].tolist() for col in METRICS},
        "noninteger_counts": {col: frame.loc[frame[col].notna() & (frame[col] % 1 != 0), "excel_row"].tolist() for col in METRICS},
        "orders_less_than_buyers": {pre: frame.loc[frame[pre + "_orders"] < frame[pre + "_buyers"], "excel_row"].tolist() for pre in ["market", "partner"]},
        "unpaired_buyer_order_missing": {pre: frame.loc[frame[pre + "_orders"].isna() != frame[pre + "_buyers"].isna(), "excel_row"].tolist() for pre in ["market", "partner"]},
        "min_published_count": {col: frame[col].min() for col in METRICS},
        "max_published_count": {col: frame[col].max() for col in METRICS},
    }


def concentration(frame, column, label_column):
    valid = frame.loc[frame[column].notna()].sort_values(column, ascending=False)
    total = valid[column].sum()
    shares = valid[column] / total
    result = {"denominator": total, "published_groups": len(valid), "hhi_fraction": float((shares ** 2).sum())}
    result["effective_equal_sized_groups"] = 1 / result["hhi_fraction"]
    result["top_n"] = {}
    for n in [1, 3, 5, 10, 20]:
        top = valid.head(n)
        result["top_n"][str(n)] = {"count": top[column].sum(), "share": top[column].sum() / total, "groups": top[label_column].tolist()}
    return clean(result)


def fnum(value, decimals=0):
    if value is None or pd.isna(value):
        return "нет данных"
    return f"{value:,.{decimals}f}".replace(",", " ").replace(".", ",")


def fpct(value, decimals=2):
    if value is None or pd.isna(value):
        return "нет данных"
    return fnum(100 * value, decimals) + "%"


def fmt_pp(value):
    return fnum(value, 2) + " п.п."


def mdtable(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines += ["| " + " | ".join(str(v).replace("|", "\\|").replace("\n", " ") for v in row) + " |" for row in rows]
    return "\n".join(lines)


def summary_rows(frame, label):
    return [[row[label], fnum(row.market_buyers), fpct(row.market_buyers_share), fnum(row.market_orders), fnum(row.market_orders_per_buyer, 3), fnum(row.partner_buyers), fpct(row.partner_buyers_share), fnum(row.partner_orders), fnum(row.partner_orders_per_buyer, 3), fnum(row.buyers_composition_index, 2)] for _, row in frame.iterrows()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    source_dir = Path(__file__).resolve().parent.parent / "source"
    candidates = list(source_dir.glob("*.xlsx"))
    source = args.source or (candidates[0] if len(candidates) == 1 else None)
    if source is None:
        raise RuntimeError("Specify one unambiguous XLSX source")
    source = source.resolve()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    demo_raw = load_table(source, "Покупатели")
    regions_raw = load_table(source, "Покупатели по регионам")
    summary = pd.read_excel(source, sheet_name="Маркет Вконтакте — итоги").set_index("Метрика")
    published = demo_raw[METRICS].sum(min_count=1)
    regional_totals = regions_raw[METRICS].sum(min_count=1)
    control = {}
    overall = {}
    for pre, segment in [("market", "Маркет Вконтакте"), ("partner", "Партнёр 1")]:
        for unit, text in [("buyers", "Покупатели"), ("orders", "Заказы")]:
            label = next(s for s in summary.index if s.startswith(text + " на листах"))
            control[pre + "_" + unit] = int(summary.loc[label, segment])
        overall[pre + "_buyers"] = int(summary.loc["Уникальные покупатели за 30 дней", segment])
        overall[pre + "_orders"] = int(summary.loc["Оформленные заказы", segment])
    reconciliation = []
    for col in METRICS:
        omitted = overall[col] - published[col]
        reconciliation.append({"metric": col, "demographic_sum": published[col], "regional_sum": regional_totals[col], "control_total": control[col], "detail_minus_control": published[col] - control[col], "demographics_minus_regions": published[col] - regional_totals[col], "overall_total": overall[col], "overall_minus_published": omitted, "published_coverage_of_overall": published[col] / overall[col]})
    demo = enrich(demo_raw, published)
    gender = enrich(aggregate(demo_raw, "gender", GENDER_ORDER), published)
    age = enrich(aggregate(demo_raw, "age", AGE_ORDER), published)
    for pre in ["market", "partner"]:
        gender_den = demo.groupby("gender")[pre + "_buyers"].transform(lambda x: x.sum(min_count=1))
        age_den = demo.groupby("age")[pre + "_buyers"].transform(lambda x: x.sum(min_count=1))
        demo[pre + "_buyers_share_within_gender"] = demo[pre + "_buyers"] / gender_den
        demo[pre + "_buyers_share_within_age"] = demo[pre + "_buyers"] / age_den
    age_bands = {}
    for label, groups in {
        "under_35": ["до 18", "18–24", "25–34"],
        "18_34": ["18–24", "25–34"],
        "25_44": ["25–34", "35–44"],
        "35_54": ["35–44", "45–54"],
        "45_plus": ["45–54", "55+"],
        "55_plus": ["55+"],
    }.items():
        sums = demo_raw.loc[demo_raw.age.isin(groups), METRICS].sum(min_count=1)
        age_bands[label] = {"groups": groups, "counts": sums.to_dict(), "shares_of_published": (sums / published).to_dict(), "market_orders_per_buyer": sums.market_orders / sums.market_buyers, "partner_orders_per_buyer": sums.partner_orders / sums.partner_buyers}
    selected_cells = {}
    for label, mask in {
        "women_25_44": (demo.gender == "Ж") & demo.age.isin(["25–34", "35–44"]),
        "women_18_44": (demo.gender == "Ж") & demo.age.isin(["18–24", "25–34", "35–44"]),
        "men_18_34": (demo.gender == "М") & demo.age.isin(["18–24", "25–34"]),
    }.items():
        sums = demo.loc[mask, METRICS].sum(min_count=1)
        selected_cells[label] = {"counts": sums.to_dict(), "shares_of_published": (sums / published).to_dict()}
    # Arithmetic standardisation, not a causal model. Unknown ages excluded equally.
    ages_known = age.loc[age.age != "н/д"]
    wm = ages_known.market_buyers / ages_known.market_buyers.sum()
    wp = ages_known.partner_buyers / ages_known.partner_buyers.sum()
    fm = ages_known.market_orders_per_buyer
    fp = ages_known.partner_orders_per_buyer
    within = float(((fp - fm) * (wp + wm) / 2).sum())
    mix = float(((wp - wm) * (fp + fm) / 2).sum())
    standardisation = {
        "scope": "Six known age bands; synthetic descriptive decomposition, not causal",
        "market_orders_per_buyer_known_age": float((wm * fm).sum()),
        "partner_orders_per_buyer_known_age": float((wp * fp).sum()),
        "partner_rate_with_market_age_weights": float((wm * fp).sum()),
        "market_rate_with_partner_age_weights": float((wp * fm).sum()),
        "gap_partner_minus_market": within + mix,
        "symmetric_within_age_component": within,
        "symmetric_age_mix_component": mix,
        "age_mix_fraction_of_gap": mix / (within + mix),
        "formula": "within=sum((fp-fm)*(wp+wm)/2); mix=sum((wp-wm)*(fp+fm)/2)",
    }
    age_quantile_bands = {}
    for pre in ["market", "partner"]:
        cumulative = ages_known[pre + "_buyers"].cumsum() / ages_known[pre + "_buyers"].sum()
        age_quantile_bands[pre] = {str(q): ages_known.loc[cumulative >= q, "age"].iloc[0] for q in [0.25, 0.5, 0.75]}
    regions = enrich(regions_raw, published)
    known_mask = regions.region != "Не указан"
    known_totals = regions.loc[known_mask, METRICS].sum(min_count=1)
    unknown = regions.loc[~known_mask].iloc[0]
    common_mask = known_mask & regions.market_buyers.notna() & regions.partner_buyers.notna()
    common_totals = regions.loc[common_mask, METRICS].sum(min_count=1)
    for pre in ["market", "partner"]:
        for unit in ["buyers", "orders"]:
            col = pre + "_" + unit
            regions[col + "_share_known_regions"] = (regions[col] / known_totals[col]).where(known_mask)
            regions[col + "_rank_known_regions"] = regions[col].where(known_mask).rank(ascending=False, method="min")
            regions[col + "_share_common_regions"] = (regions[col] / common_totals[col]).where(common_mask)
        # Identification intervals: each unknown buyer may or may not belong to a given region.
        regions[pre + "_buyer_regional_share_lower_bound_published"] = (regions[pre + "_buyers"] / published[pre + "_buyers"]).where(known_mask)
        regions[pre + "_buyer_regional_share_upper_bound_published"] = ((regions[pre + "_buyers"] + unknown[pre + "_buyers"]) / published[pre + "_buyers"]).where(known_mask)
    for unit in ["buyers", "orders"]:
        regions[unit + "_composition_index_known_regions"] = regions["market_" + unit + "_share_known_regions"] / regions["partner_" + unit + "_share_known_regions"]
        regions[unit + "_share_gap_pp_known_regions"] = 100 * (regions["market_" + unit + "_share_known_regions"] - regions["partner_" + unit + "_share_known_regions"])
        regions[unit + "_composition_index_common_regions"] = regions["market_" + unit + "_share_common_regions"] / regions["partner_" + unit + "_share_common_regions"]
        regions[unit + "_rank_gap_partner_minus_market_known"] = regions["partner_" + unit + "_rank_known_regions"] - regions["market_" + unit + "_rank_known_regions"]
    known = regions.loc[known_mask]
    regional_concentration = {scope: {metric: concentration(frame, metric, "region") for metric in METRICS} for scope, frame in [("published_including_unknown", regions), ("published_known_regions", known), ("published_common_regions", regions.loc[common_mask])]} 
    regional_frequency_exceptions = regions.loc[regions.market_orders_per_buyer > regions.partner_orders_per_buyer, ["excel_row", "region"] + METRICS + ["market_orders_per_buyer", "partner_orders_per_buyer"]]
    frequency = {}
    for pre in ["market", "partner"]:
        missing_buyers = overall[pre + "_buyers"] - published[pre + "_buyers"]
        missing_orders = overall[pre + "_orders"] - published[pre + "_orders"]
        frequency[pre] = {
            "published_orders_per_buyer": published[pre + "_orders"] / published[pre + "_buyers"],
            "overall_orders_per_buyer": overall[pre + "_orders"] / overall[pre + "_buyers"],
            "summary_rounded_orders_per_buyer": float(summary.loc["Заказов на покупателя", "Маркет Вконтакте" if pre == "market" else "Партнёр 1"]),
            "omitted_buyers": missing_buyers, "omitted_orders": missing_orders,
            "implied_orders_per_omitted_buyer_if_same_partition": divide(missing_orders, missing_buyers),
            "extra_orders_beyond_first_in_published": published[pre + "_orders"] - published[pre + "_buyers"],
            "known_region_orders_per_buyer": known_totals[pre + "_orders"] / known_totals[pre + "_buyers"],
            "unknown_region_orders_per_buyer": unknown[pre + "_orders"] / unknown[pre + "_buyers"],
            "summary_repeat_buyer_share": float(summary.loc["Доля повторных покупателей (2+ заказа), %", "Маркет Вконтакте" if pre == "market" else "Партнёр 1"]) / 100,
        }
    workbook = load_workbook(source, read_only=False, data_only=False)
    source_structure = {name: {"max_rows": workbook[name].max_row, "max_columns": workbook[name].max_column, "hidden_rows": [row for row, dim in workbook[name].row_dimensions.items() if dim.hidden], "hidden_columns": [col for col, dim in workbook[name].column_dimensions.items() if dim.hidden], "formula_cells": [cell.coordinate for row in workbook[name] for cell in row if cell.data_type == "f"], "comments": [{"cell": cell.coordinate, "text": cell.comment.text} for row in workbook[name] for cell in row if cell.comment]} for name in ["Покупатели", "Покупатели по регионам", "Маркет Вконтакте — итоги"]}
    definitions = [
        {"source": "PDF, с. 32", "type": "direct", "text": "Датасет создан специально для кейс-чемпионата; синтетические обезличенные данные не отражают реальные показатели компании; использовать при разработке решения обязательно."},
        {"source": "PDF, с. 6", "type": "direct", "text": "Часть данных могла быть намеренно искажена или упрощена; единственного верного решения нет."},
        {"source": "PDF, с. 33", "type": "direct", "text": "Партнёр — условная внешняя торговая площадка. Данные описывают взаимодействие с её товарами во ВКонтакте и покупки на внешней площадке в данном сценарии. Название реального партнёра не определено."},
        {"source": "PDF, с. 33", "type": "direct", "text": "Лист Покупатели: число покупателей и заказов по полу и возрасту. Лист Покупатели по регионам: число покупателей и заказов по регионам. Таблица итогов — месячные метрики."},
        {"source": "XLSX, итоги, A19:D19", "type": "direct", "text": "Период 2026-09-01 — 2026-09-30. В строке покупателей явно указано за 30 дней."},
        {"source": "XLSX, итоги, A16:C17", "type": "direct", "text": "Контрольные суммы листов покупателей обозначены как без групп меньше 100. Правило, по какой метрике и по каким точным группам применяется порог, отдельно не дано."},
        {"source": "PDF, с. 34", "type": "direct", "text": "Уникальный пользователь — человек, учтённый один раз за период независимо от числа действий. Чекаут — оформление заказа внутри платформы без стороннего сайта."},
        {"source": "PDF, с. 34", "type": "direct", "text": "GMV — стоимость заказов без вычета отмен, возвратов и комиссий; средний чек — GMV/заказы. Конверсия — доля пользователей, дошедших до следующего шага воронки."},
        {"source": "XLSX, итоги, A6:C7", "type": "direct", "text": "Оформленные и оплаченные заказы выделены отдельно. Маркет: 413740 оформленных и 4903 оплаченных; Партнёр 1: 4927525 оформленных, по оплаченных стоит прочерк, не ноль."},
        {"source": "PDF и XLSX: отсутствие определения", "type": "unknown", "text": "Не определены технический статус заказа, момент признания покупателя, полнота учёта оплат, внешние/офлайн-оплаты, обработка отмен/возвратов, дедупликация между каналами, геопривязка, проверка возраста/пола, генеральная совокупность и механизм выборки."},
        {"source": "Сверка XLSX", "type": "interpretation", "text": "Заказы в двух покупательских разрезах согласуются с опубликованными контрольными итогами, но их нельзя автоматически считать оплаченными или выкупленными. Частоты описывают опубликованный срез."},
    ]
    requirements = [
        {"page": 20, "direct": "Увеличить число бизнесов, продающих через ВКонтакте. Ориентиры — подключившие инструменты бизнесы и оформленные внутри ВКонтакте заказы.", "implication": "Портрет покупателя поддерживает выбор бизнесов; не заменяет сегментацию продавцов и не переводит внешний заказ партнёра в целевой внутренний заказ."},
        {"page": 13, "direct": "Фокус на предпринимателе, который сам приходит, создаёт сообщество, размещает товары и общается с клиентами.", "implication": "Нельзя свести решение к привлечению блогеров или продавцов условного Партнёра 1."},
        {"page": 15, "direct": "Приоритет — продажа товаров; инструменты пока не складываются в целостный опыт.", "implication": "Демография сама по себе не доказывает, какая точка опыта сломана; нужны воронки и наблюдения поведения."},
        {"page": 16, "direct": "Искать корреляции и сегментировать бизнесы; сначала проверить сопоставимость групп и доступность внешних продаж.", "implication": "Использовать разные знаменатели общего и опубликованного срезов; отдельно показать неизвестную географию; не объявлять корреляции причинностью."},
        {"page": 20, "direct": "Для каждого инструмента оценить эффект бизнесу и ВКонтакте; определить роль Маркета; спроектировать путь бизнеса с развилками.", "implication": "Число покупателей без GMV/маржи/стоимости привлечения не даёт экономику инструментов. Демографические индексы не обосновывают самостоятельный экран Маркета."},
        {"page": "17–18", "direct": "Допустимо заключить, что отдельная витрина не нужна, если это обосновано данными.", "implication": "Листы покупателей не описывают причинную ценность самостоятельной витрины и её инкрементальные продажи."},
        {"page": "8–9", "direct": "Подтверждать факты расчётами и источниками, рассмотреть альтернативы, связать решение с датасетом; раскрыть и проверить применение ИИ.", "implication": "Хранить воспроизводимый расчёт, признавать несогласованные итоги и неизвестные параметры, не маскировать их уверенным прогнозом."},
    ]
    result = {
        "metadata": {"source_xlsx": "source/case_dataset.xlsx", "source_sha256": source_hash, "period": "2026-09-01 — 2026-09-30", "scope": "All 15 demographic and all 89 regional source rows; source XLSX unchanged", "shares_unit": "fraction_0_to_1", "percentage_point_unit": "0_to_100", "rank_method": "descending competition rank, ties share minimum rank", "missing_values": "null means source missing/suppressed or undefined, never a measured zero", "source_structure": source_structure},
        "quality": {"demographics": quality(demo_raw, ["gender", "age"]), "regions": quality(regions_raw, ["region"])},
        "published_totals": published.to_dict(), "overall_totals": overall, "reconciliation": reconciliation,
        "frequency_and_scope": frequency,
        "demographics": {"all_rows": recs(demo), "gender": recs(gender), "age": recs(age), "selected_age_bands": age_bands, "selected_joint_groups": selected_cells, "age_quantile_bands": age_quantile_bands, "age_standardisation": standardisation, "market_frequency_above_partner": recs(demo.loc[demo.market_orders_per_buyer > demo.partner_orders_per_buyer]), "concentration": {metric: concentration(demo.assign(cell=demo.gender + " " + demo.age), metric, "cell") for metric in METRICS}},
        "regions": {"all_rows": recs(regions), "known_totals": known_totals.to_dict(), "common_region_totals": common_totals.to_dict(), "known_named_regions": len(known), "market_named_regions_with_published_counts": int(known.market_buyers.notna().sum()), "partner_named_regions_with_published_counts": int(known.partner_buyers.notna().sum()), "unknown_region": clean(unknown.to_dict()), "concentration": regional_concentration, "market_frequency_above_partner": recs(regional_frequency_exceptions), "top_composition_index_known_regions": recs(known.sort_values("buyers_composition_index_known_regions", ascending=False).head(15)), "bottom_composition_index_known_regions": recs(known.sort_values("buyers_composition_index_known_regions").head(15)), "largest_positive_share_gaps_known": recs(known.sort_values("buyers_share_gap_pp_known_regions", ascending=False).head(15)), "largest_negative_share_gaps_known": recs(known.sort_values("buyers_share_gap_pp_known_regions").head(15))},
        "pdf_and_workbook_definitions": definitions, "case_requirements_affecting_analysis": requirements,
    }
    # Exact, independent cross-table controls, rather than tests that mirror each row formula.
    assert all(published[col] == regional_totals[col] == control[col] for col in METRICS)
    assert math.isclose(within + mix, float((wp * fp).sum() - (wm * fm).sum()), abs_tol=1e-12)
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    result = clean(result)
    (args.output_dir / "buyers_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    write_findings(args.output_dir, result, demo, gender, age, regions, known, reconciliation)
    print(json.dumps({"outputs": [str(args.output_dir / "buyers_results.json"), str(args.output_dir / "buyers_findings.md")], "published_totals": result["published_totals"], "overall_totals": overall, "frequency_and_scope": result["frequency_and_scope"], "source_unchanged": True}, ensure_ascii=False, indent=2))


def write_findings(output_dir, result, demo, gender, age, regions, known, reconciliation):
    p = result["published_totals"]
    overall = result["overall_totals"]
    bands = result["demographics"]["selected_age_bands"]
    groups = result["demographics"]["selected_joint_groups"]
    st = result["demographics"]["age_standardisation"]
    unknown = result["regions"]["unknown_region"]
    fq = result["frequency_and_scope"]
    concentration_known = result["regions"]["concentration"]["published_known_regions"]
    mrate = fq["market"]["published_orders_per_buyer"]
    prate = fq["partner"]["published_orders_per_buyer"]
    sr = lambda name: regions.loc[regions.region == name].iloc[0]
    md = []
    add = md.append
    add("# Покупатели VK Case Champ: полный анализ двух разрезов\n")
    add("Источник: предоставленная книга «VK_CaseChamp_2026_Отборочныи__этап_Датасет (1).xlsx», листы «Покупатели», «Покупатели по регионам» и контрольные строки «Маркет Вконтакте — итоги». Условия и определения: PDF «VK CASE СHAMP 2026», указанные ниже страницы. Период в книге: 1–30 сентября 2026 года. Все 15 демографических строк и все 89 региональных строк включены в анализ. Это синтетический учебный датасет, а не фактическая статистика VK. Исходная книга не изменялась.\n")
    add("## 1. Главные выводы\n")
    add(f"1. Два покупательских разреза согласуются между собой точно: Маркет — {fnum(p['market_buyers'])} покупателей и {fnum(p['market_orders'])} заказа; Партнёр 1 — {fnum(p['partner_buyers'])} покупателя и {fnum(p['partner_orders'])} заказа. Это опубликованный срез после исключения малых групп. Его нельзя незаметно смешивать с общими итогами.\n")
    add(f"2. Крупнейшая проблема качества: у Партнёра 1 вне разрезов остаются {fnum(fq['partner']['omitted_buyers'])} покупателя и {fnum(fq['partner']['omitted_orders'])} заказа. При одинаковых определениях это {fnum(fq['partner']['implied_orders_per_omitted_buyer_if_same_partition'], 1)} заказа на исключённого покупателя. Возможны разные охваты/определения, ошибка подготовки или экстремальные записи. Сам файл не позволяет выбрать объяснение. Поэтому частота партнёра в разрезах — {fnum(prate, 3)}, а по общему итогу — {fnum(fq['partner']['overall_orders_per_buyer'], 3)} заказа на человека.\n")
    add(f"3. У Маркета моложе возрастная структура: 18–34 года — {fpct(bands['18_34']['shares_of_published']['market_buyers'])}, у партнёра — {fpct(bands['18_34']['shares_of_published']['partner_buyers'])}. Покупатели 45+ — {fpct(bands['45_plus']['shares_of_published']['market_buyers'])} и {fpct(bands['45_plus']['shares_of_published']['partner_buyers'])}. Это различие состава уже купивших людей, не конверсия и не доказательство предпочтения всех молодых пользователей.\n")
    add(f"4. Женщины составляют {fpct(gender.loc[gender.gender == 'Ж', 'market_buyers_share'].iloc[0])} покупателей Маркета и {fpct(gender.loc[gender.gender == 'Ж', 'partner_buyers_share'].iloc[0])} партнёра. Женщины 25–44 — {fpct(groups['women_25_44']['shares_of_published']['market_buyers'])} покупателей Маркета. При этом наиболее выраженное относительное отличие от партнёра — у молодых мужчин, а не у женщин в целом. Масштаб и относительное отличие отвечают на разные вопросы.\n")
    add(f"5. География партнёра неполна: «Не указан» — {fpct(unknown['partner_buyers_share'])} его покупателей против {fpct(unknown['market_buyers_share'])} у Маркета. Любое сравнение региональных долей должно показывать эту пропущенную часть. Среди известных регионов Маркет более сконцентрирован: топ-10 — {fpct(concentration_known['market_buyers']['top_n']['10']['share'])} против {fpct(concentration_known['partner_buyers']['top_n']['10']['share'])}. Это условная характеристика географически определённых покупателей.\n")
    add(f"6. На одного опубликованного покупателя приходится {fnum(mrate, 3)} заказа Маркета и {fnum(prate, 3)} заказа партнёра. Разрыв — {fnum(prate - mrate, 3)} заказа, или {fpct(prate / mrate - 1)} относительно Маркета. Внутривозрастное сравнение сохраняет большую часть разницы; одного возраста недостаточно для её объяснения. Установить причины без категорий, каналов привлечения и истории клиентов нельзя.\n")
    add("## 2. Что именно дано, а чего в условиях нет\n")
    for definition in result["pdf_and_workbook_definitions"]:
        kind = {"direct": "Прямо в источнике", "unknown": "Не определено", "interpretation": "Вывод из сверки"}[definition["type"]]
        add(f"- **{kind}, {definition['source']}:** {definition['text']}\n")
    add("Уникальность покупателя разумно читать в пределах каждого канала и 30-дневного периода по названию строки, но точный алгоритм не дан. Один человек может быть покупателем и Маркета, и партнёра. Их сумму нельзя называть числом уникальных людей во всей социальной коммерции. Без пересечения ID неизвестны дедуплицированная аудитория, миграция и каннибализация между маршрутами.\n")
    add("В покупательских таблицах нет дневной оси и нет повторения периодов. Они не показывают рост, падение, сезонность, срок до первой покупки или удержание. Состав покупателей не описывает состав всех посетителей, увидевших товары, и тем более населения региона.\n")
    add("### Оформление, оплата и фактическая покупка\n")
    add(f"В итогах отдельно стоят {fnum(overall['market_orders'])} оформленных и 4 903 оплаченных заказа Маркета. Их арифметическое отношение 1,185% не доказывает, что остальные заказы сорвались: неизвестно, охватывает ли поле оплат все способы, какие статусы и временное окно использованы. По оплаченных заказам партнёра стоит прочерк. В двух анализируемых листах показатели названы просто «Заказы». Без уточнения их нельзя называть выкупами, доставленными или оплаченными заказами. PDF определяет GMV до возвратов и отмен, поэтому даже наличие GMV не устраняет этот пробел.\n")
    add("## 3. Проверка всех строк и контрольных сумм\n")
    metric_labels = {"market_buyers": "Маркет: покупатели", "market_orders": "Маркет: заказы", "partner_buyers": "Партнёр: покупатели", "partner_orders": "Партнёр: заказы"}
    add(mdtable(["Показатель", "Сумма демографии", "Сумма регионов", "Контроль разрезов", "Общий итог", "Вне разрезов", "Охват общего итога"], [[metric_labels[r['metric']], fnum(r['demographic_sum']), fnum(r['regional_sum']), fnum(r['control_total']), fnum(r['overall_total']), fnum(r['overall_minus_published']), fpct(r['published_coverage_of_overall'], 4)] for r in reconciliation]) + "\n")
    add("Все четыре разницы между демографией, регионами и специальными контрольными строками равны нулю. Это сильная проверка арифметики, но не доказательство одинаковой методологии общего итога и разрезов.\n")
    add(f"У Маркета вне разрезов {fnum(fq['market']['omitted_buyers'])} покупателей и {fnum(fq['market']['omitted_orders'])} заказов, или {fnum(fq['market']['implied_orders_per_omitted_buyer_if_same_partition'], 3)} заказа на такого покупателя. У партнёра исключено около 0,0059% покупателей, но 7,514% заказов. Масштаб этой асимметрии нельзя списать на округление: расхождение составляет 370 272 целых заказа. Если исключённые покупатели действительно совершали необычно много заказов, требуется отдельная проверка типа покупателей и очистки аномалий. Без исходных ID это лишь одна из гипотез.\n")
    add("Проверены все числовые ячейки этих двух листов: отрицательных и дробных счётчиков, наблюдаемых нулей, дубликатов ключа «пол × возраст»/«регион» нет. Во всех опубликованных парах заказы не меньше покупателей. Пропуски покупателей и заказов парные. Скрытых строк/столбцов, формул и комментариев в двух разрезах и контрольном листе нет. Значения — готовые агрегаты, восстановить алгоритм сборки по формулам невозможно.\n")
    add("В демографии Маркета пусты две пары: «М, н/д» и «н/д, н/д». В регионах Маркета пусты четыре пары: Еврейская автономная область, Карачаево-Черкесская Республика, Херсонская область, Чукотский автономный округ. У партнёра все 15 демографических и 89 региональных строк численно заполнены. Пустые ячейки сохранены как отсутствие опубликованных данных, не превращены в нулевые продажи. Название контроля «без групп меньше 100» согласуется с подавлением малых групп, но полного перечня исключённых групп и основания порога нет. Нельзя восстановить все исключённые группы из числа видимых пустых ячеек.\n")
    add("## 4. Знаменатели и формулы\n")
    add("- Доля группы покупателей внутри канала = опубликованные покупатели группы / все опубликованные покупатели того же канала. Базы: 295 850 для Маркета и 2 901 162 для партнёра. Доли заказов считаются отдельно по 413 334 и 4 557 253 заказам.\n- Заказов на покупателя = сумма заказов / сумма покупателей в одной и той же группе. Среднее от частот строк не используется. Это частота оформлений в месяце, не конверсия и не доля повторных покупателей.\n- Индекс состава = доля группы среди покупателей Маркета / доля этой же группы среди покупателей партнёра. Индекс 2 означает вдвое большую представленность группы в составе Маркета. Он не означает вдвое большую вероятность покупки, проникновение или абсолютный размер аудитории.\n- Региональная доля среди известных регионов: региональные покупатели / покупатели всех опубликованных именованных регионов того же канала. Базы: 290 661 и 1 836 068. «Не указан» исключён из числителя и знаменателя этой версии сравнения.\n- Разница долей измеряется в процентных пунктах; отношение частот и индексы — в разах. Все исходные расчёты сохраняют полную точность; округление применяется только в тексте.\n- Ранг 1 — крупнейшая опубликованная группа, одинаковым значениям присваивается минимальный из их рангов. Региональные ранги ниже исключают строку «Не указан».\n")
    add("Суммировать уникальных покупателей по непересекающимся половозрастным/региональным группам можно при условии единственной принадлежности человека группе в периоде. Точная сверка с контролями подтверждает, что так устроены опубликованные таблицы, но не доказывает отсутствие пересечения между Маркетом и партнёром. Объединять эти два разреза по позиции строки или строить «женщины 25–44 из Челябинска» нельзя: совместной таблицы возраст × пол × регион нет.\n")
    add("## 5. Пол: масштаб и частота\n")
    headers = ["Группа", "Покупатели М", "Доля М", "Заказы М", "Заказов/покуп. М", "Покупатели П", "Доля П", "Заказы П", "Заказов/покуп. П", "Индекс доли М/П"]
    add(mdtable(headers, summary_rows(gender, "gender")) + "\n")
    add("М — Маркет, П — Партнёр 1. «н/д» у Маркета — скрытое/отсутствующее значение, поэтому в итогах по полу указана сумма известных опубликованных наблюдений. 0% неизвестного пола было бы неверным утверждением. У мужчин Маркета также не опубликована группа с неизвестным возрастом, поэтому 80 454 — наблюдаемая сумма показанных мужских групп.\n")
    add("Женщины доминируют в обоих каналах. Их доля у Маркета ниже на 1,664 п.п., доля мужчин выше примерно на 1,670 п.п. Женщины Маркета делают 1,403 заказа на покупательницу, мужчины — 1,382. Разрыв небольшой, около 1,47%. У партнёра частота женщин 1,604 против 1,473 у мужчин, около 8,93% выше. Это могут объяснять категории, состав по возрастам, повторяемость товаров и привлечение. По агрегатам нельзя приписать разницу одному полу.\n")
    add("## 6. Возраст: все группы\n")
    add(mdtable(headers, summary_rows(age, "age")) + "\n")
    add(f"Главное возрастное ядро Маркета — 25–44: {fnum(bands['25_44']['counts']['market_buyers'])} покупателей, {fpct(bands['25_44']['shares_of_published']['market_buyers'])}. У партнёра тот же диапазон — {fpct(bands['25_44']['shares_of_published']['partner_buyers'])}. Самая крупная отдельная возрастная группа обоих каналов — 35–44. Но Маркет имеет значительно больше 18–34, а партнёр — 45+.\n")
    add(mdtable(["Возрастной диапазон", "Покупатели М", "Доля М", "Покупатели П", "Доля П", "Разница долей М − П"], [[label, fnum(bands[key]['counts']['market_buyers']), fpct(bands[key]['shares_of_published']['market_buyers']), fnum(bands[key]['counts']['partner_buyers']), fpct(bands[key]['shares_of_published']['partner_buyers']), fmt_pp(100 * (bands[key]['shares_of_published']['market_buyers'] - bands[key]['shares_of_published']['partner_buyers']))] for key, label in [('under_35', 'Моложе 35'), ('18_34', '18–34'), ('25_44', '25–44'), ('35_54', '35–54'), ('45_plus', '45+'), ('55_plus', '55+')]]) + "\n")
    add("Индекс доли 18–24 составляет 3,24; 25–34 — 1,66; 35–44 — 0,89; 45–54 — 0,63; 55+ — 0,45. То есть молодой состав Маркета виден не только в абсолютном числе крупных групп. Но у партнёра абсолютных покупателей больше в каждой опубликованной сопоставимой половозрастной ячейке. Большой индекс Маркета не означает большего количества людей.\n")
    add("Медианная возрастная группа в обоих каналах — 35–44. Нижний квартиль Маркета находится в 25–34, партнёра — в 35–44; верхний квартиль Маркета — в 35–44, партнёра — в 45–54. Это интервалы квартилей, не точный медианный возраст. Средний возраст корректно не вычисляется: нет возраста отдельных людей, а группы «до 18» и «55+» открытые.\n")
    add("Неизвестный возраст опубликован у 134 покупателей Маркета (0,0453%) и 3 687 партнёра (0,1271%). Эти доли малы среди показанных строк, но полное неизвестное значение Маркета не восстановить из подавленных групп. До 18 лет — 6 213 и 21 507 покупателей; источник не сообщает, как проверялись возраст и самостоятельность покупателя. Эти наблюдения не следует превращать в рекомендацию таргетировать несовершеннолетних или в портрет собственника бизнеса.\n")
    add("### Проверка: объясняет ли возраст разницу частоты?\n")
    add(f"Только среди известных возрастов средняя частота Маркета {fnum(st['market_orders_per_buyer_known_age'], 3)}, партнёра {fnum(st['partner_orders_per_buyer_known_age'], 3)}. Если приложить возрастные частоты партнёра к возрастному составу Маркета, получим {fnum(st['partner_rate_with_market_age_weights'], 3)}. При симметричном разложении разрыва {fnum(st['gap_partner_minus_market'], 4)} заказа на покупателя компонент возрастного состава равен {fnum(st['symmetric_age_mix_component'], 4)} ({fpct(st['age_mix_fraction_of_gap'])}), а различия частоты внутри возрастных групп — {fnum(st['symmetric_within_age_component'], 4)}. Это арифметическая стандартизация, не оценка причинного влияния возраста. Категории, привлечение, вид заказа и ошибка охвата остаются возможными объяснениями.\n")
    add("## 7. Пол × возраст: все исходные строки\n")
    add(mdtable(["Строка XLSX", "Пол", "Возраст", "Покупатели М", "Заказы М", "Заказов/покуп. М", "Покупатели П", "Заказы П", "Заказов/покуп. П"], [[int(row.excel_row), row.gender, row.age, fnum(row.market_buyers), fnum(row.market_orders), fnum(row.market_orders_per_buyer, 3), fnum(row.partner_buyers), fnum(row.partner_orders), fnum(row.partner_orders_per_buyer, 3)] for _, row in demo.iterrows()]) + "\n")
    add(mdtable(["Пол", "Возраст", "Доля покупателей М", "Доля покупателей П", "Разница М − П", "Индекс доли М/П", "Ранг М", "Ранг П"], [[row.gender, row.age, fpct(row.market_buyers_share), fpct(row.partner_buyers_share), fmt_pp(row.buyers_share_gap_pp), fnum(row.buyers_composition_index, 2), fnum(row.market_buyers_rank), fnum(row.partner_buyers_rank)] for _, row in demo.iterrows()]) + "\n")
    add(f"Самые крупные ячейки Маркета: женщины 35–44 (66 329), женщины 25–34 (54 635), женщины 45–54 (37 784), женщины 18–24 (31 697), мужчины 25–34 (27 331). Первые две дают {fpct(groups['women_25_44']['shares_of_published']['market_buyers'])} покупателей и {fpct(groups['women_25_44']['shares_of_published']['market_orders'])} заказов. Женщины 18–44 дают {fpct(groups['women_18_44']['shares_of_published']['market_buyers'])} всех покупателей Маркета. Это описание достигнутого спроса, не готовый выбор товарной категории.\n")
    add(f"Мужчины 18–34 — {fnum(groups['men_18_34']['counts']['market_buyers'])} покупателей Маркета ({fpct(groups['men_18_34']['shares_of_published']['market_buyers'])}) против доли {fpct(groups['men_18_34']['shares_of_published']['partner_buyers'])} у партнёра. Мужчины 18–24 имеют индекс 4,69, мужчины 25–34 — 2,21. Для женщин 18–24 индекс 2,78, 25–34 — 1,47. Следовательно, одна усреднённая формулировка «основная аудитория — женщины» теряет важное относительное отличие молодой мужской группы.\n")
    add("Внутри возрастов картина пола неодинакова. Доля женщин Маркета возрастает примерно от 54,18% в «до 18» до 65,35% в 18–24, 66,65% в 25–34, 76,47% в 35–44 и 82,61% в 45–54; в 55+ она 80,78%. У партнёра в возрастах 18–55+ доля женщин находится примерно между 73,30% и 76,63%. Поэтому эффекты пола и возраста нельзя изучать только отдельными маргинальными таблицами.\n")
    add("У партнёра частота выше во всех шести агрегированных известных возрастах, но не в каждой ячейке пола × возраста. Исключения — мужчины до 18 (1,649 против 1,474), мужчины 18–24 (1,377 против 1,367) и мужчины 25–34 (1,408 против 1,396). Это ещё один аргумент проверять состав, а не переносить общий средний показатель на каждого покупателя. Для небольших отличий без распределений и экспериментальной схемы нельзя делать вывод о стабильном превосходстве.\n")
    add("## 8. География: сначала пропуски, затем сравнение\n")
    add(mdtable(["Охват", "Покупатели М", "Заказы М", "Покупатели П", "Заказы П"], [["Все опубликованные", fnum(p['market_buyers']), fnum(p['market_orders']), fnum(p['partner_buyers']), fnum(p['partner_orders'])], ["Регион указан", *[fnum(result['regions']['known_totals'][c]) for c in METRICS]], ["Не указан", *[fnum(unknown[c]) for c in METRICS]], ["Доля неизвестного региона", *[fpct(unknown[c + '_share']) for c in METRICS]]]) + "\n")
    add("В источнике 88 именованных региональных строк и одна строка «Не указан». Из именованных регионов у Маркета опубликованы значения по 84, у партнёра по 88. Названия сохранены ровно как в книге; административная полнота и актуальность классификатора не проверялись, переименований и внешних объединений не делалось. Москва и Московская область, Петербург и Ленинградская область — разные строки. Тюменская область и автономные округа также не объединяются без определения иерархии и проверки непересечения.\n")
    add(f"У неизвестного региона {fpct(unknown['partner_orders_share'])} заказов партнёра и {fpct(unknown['market_orders_share'])} Маркета. Частота внутри этой группы — {fnum(fq['partner']['unknown_region_orders_per_buyer'], 3)} и {fnum(fq['market']['unknown_region_orders_per_buyer'], 3)}; среди известных регионов — {fnum(fq['partner']['known_region_orders_per_buyer'], 3)} и {fnum(fq['market']['known_region_orders_per_buyer'], 3)}. Близкая частота у известной/неизвестной географии партнёра не доказывает случайность географических пропусков.\n")
    spb = sr("г. Санкт-Петербург")
    moscow_obl = sr("Московская область")
    add(f"Показательный пример знаменателя: Петербург занимает {fpct(spb.market_buyers_share)} всех опубликованных покупателей Маркета и {fpct(spb.partner_buyers_share)} партнёра; сырой индекс {fnum(spb.buyers_composition_index, 2)}. Среди известных регионов это {fpct(spb.market_buyers_share_known_regions)} и {fpct(spb.partner_buyers_share_known_regions)}, индекс {fnum(spb.buyers_composition_index_known_regions, 3)} — практически одинаковая представленность. Московская область по сырым долям почти равна ({fpct(moscow_obl.market_buyers_share)} и {fpct(moscow_obl.partner_buyers_share)}), но среди известных регионов её индекс {fnum(moscow_obl.buyers_composition_index_known_regions, 3)}.\n")
    add("Исключение «Не указан» не восстанавливает истинную географию: оно меняет вопрос на состав покупателей с известным регионом. При неслучайных пропусках этот состав может отличаться от всех покупателей. Для иллюстрации, доля Петербурга у партнёра среди всех опубликованных людей могла бы лежать от наблюдаемых 5,36% до 42,07%, если все неизвестные оказались бы петербуржцами. Это крайняя граница идентификации, не прогноз. Поэтому сильные решения о геотаргетинге требуют восполнения региона или проверки механизма его отсутствия.\n")
    add("В JSON дополнительно рассчитана чувствительность на одинаковом наборе 84 именованных регионов с опубликованными данными в обоих каналах. Четыре дополнительных региональных строки партнёра дают всего 1 552 покупателя, около 0,0845% его известных регионов. Переход к общей поддержке меняет индексы примерно на 0,0845%, намного меньше эффекта 36,71% неизвестной географии.\n")
    add("### Масштаб и концентрация\n")
    add(mdtable(["Показатель среди известных регионов", "Маркет: покупатели", "Партнёр: покупатели", "Маркет: заказы", "Партнёр: заказы"], [[f"Доля топ-{n}", *[fpct(concentration_known[c]['top_n'][str(n)]['share']) for c in ['market_buyers', 'partner_buyers', 'market_orders', 'partner_orders']]] for n in [1, 3, 5, 10, 20]] + [["HHI как сумма квадратов долей", *[fnum(concentration_known[c]['hhi_fraction'], 5) for c in ['market_buyers', 'partner_buyers', 'market_orders', 'partner_orders']]]]) + "\n")
    add("Топ-N у каждого канала определяется отдельно по его собственным значениям. HHI здесь описывает концентрацию распределения покупателей по регионам, а не конкуренцию компаний и не долю VK на рынке. Если включить строку «Не указан» как регион, концентрация партнёра искусственно возрастёт; для географической интерпретации такая версия не подходит, но сохранена в JSON как проверка.\n")
    topm = known.sort_values("market_buyers", ascending=False).head(10)
    topp = known.sort_values("partner_buyers", ascending=False).head(10)
    add("Топ-10 Маркета по опубликованным покупателям: " + "; ".join(f"{row.region} — {fnum(row.market_buyers)}" for _, row in topm.iterrows()) + ".\n")
    add("Топ-10 партнёра: " + "; ".join(f"{row.region} — {fnum(row.partner_buyers)}" for _, row in topp.iterrows()) + ".\n")
    add("### Где особенно отличается состав\n")
    notable = ["г. Москва", "г. Санкт-Петербург", "Челябинская область", "Пермский край", "Новгородская область", "Курская область", "Удмуртская Республика", "Московская область", "Краснодарский край", "Волгоградская область", "Омская область", "Республика Марий Эл", "Чеченская Республика"]
    add(mdtable(["Регион", "Покупатели М", "Покупатели П", "Доля М среди известных", "Доля П среди известных", "Индекс М/П"], [[name, fnum(sr(name).market_buyers), fnum(sr(name).partner_buyers), fpct(sr(name).market_buyers_share_known_regions), fpct(sr(name).partner_buyers_share_known_regions), fnum(sr(name).buyers_composition_index_known_regions, 3)] for name in notable]) + "\n")
    add("Для достаточно крупных наблюдаемых групп заметны Челябинская область (индекс 1,904), Новгородская (2,710), Пермский край (1,549), Курская область (1,757), Удмуртия (1,510). Это кандидаты для изучения того, какие бизнесы, категории или каналы привели спрос. Сама география не устанавливает причину и не показывает свободный потенциал роста. Сопоставление со средним партнёра также не говорит, что партнёр представляет население или универсальную структуру электронной торговли.\n")
    add("Самый высокий индекс имеет Чеченская Республика (3,488), но это лишь 180 покупателей Маркета и 326 партнёра. Запорожская область тоже даёт высокий индекс (2,140) при 123 и 363 покупателях. Выбирать основной регион запуска по максимуму индекса без масштаба, экономической ценности и проверки данных было бы слабым обоснованием. Низкий индекс также не равен привлекательному «недоохваченному рынку»: там могут отсутствовать товары, логистика, нужные продавцы или доступный спрос.\n")
    add("### Частота регионов\n")
    add("У Маркета опубликованная частота варьирует от 1,261 в Орловской области до 1,617 в Магаданской; у партнёра — от 1,440 в Кабардино-Балкарии до 1,763 в Ненецком автономном округе. Многие крайние значения опираются на сотни покупателей. Региональный состав по категориям, чекам, возрасту и источникам трафика не дан.\n")
    ex = result['regions']['market_frequency_above_partner']
    add(mdtable(["Регион, где частота М выше П", "Покупатели М", "Покупатели П", "Заказов/покуп. М", "Заказов/покуп. П"], [[r['region'], fnum(r['market_buyers']), fnum(r['partner_buyers']), fnum(r['market_orders_per_buyer'], 3), fnum(r['partner_orders_per_buyer'], 3)] for r in ex]) + "\n")
    add("Таких региональных строк четыре. Ивановская область опирается на 2 464 покупателей Маркета; у остальных в этой таблице 115–351 покупатель. Это наблюдаемые исключения, не установленный эффект региона.\n")
    add("## 9. Полный региональный реестр: все 89 строк\n")
    add("Ниже сохранён порядок исходного листа. Все отсутствующие значения обозначены словами; реальные нули не подставлялись.\n")
    add(mdtable(["Строка XLSX", "Регион", "Покупатели М", "Заказы М", "Покупатели П", "Заказы П", "Заказов/покуп. М", "Заказов/покуп. П"], [[int(r.excel_row), r.region, fnum(r.market_buyers), fnum(r.market_orders), fnum(r.partner_buyers), fnum(r.partner_orders), fnum(r.market_orders_per_buyer, 3), fnum(r.partner_orders_per_buyer, 3)] for _, r in regions.iterrows()]) + "\n")
    add("### Доли, индексы и ранги всех именованных регионов\n")
    add("Знаменатели: все опубликованные покупатели с известным регионом, 290 661 у Маркета и 1 836 068 у партнёра. Ранг не учитывает «Не указан». Индекс неизвестен, если данные Маркета скрыты.\n")
    add(mdtable(["Регион", "Ранг М", "Ранг П", "Доля М", "Доля П", "Разница М − П", "Индекс доли М/П"], [[r.region, fnum(r.market_buyers_rank_known_regions), fnum(r.partner_buyers_rank_known_regions), fpct(r.market_buyers_share_known_regions, 3), fpct(r.partner_buyers_share_known_regions, 3), fmt_pp(r.buyers_share_gap_pp_known_regions), fnum(r.buyers_composition_index_known_regions, 3)] for _, r in known.iterrows()]) + "\n")
    add("## 10. Что из этих данных нельзя заключить\n")
    add("1. **Проникновение и конверсия.** Нет численности населения по группам, активной аудитории VK, уникальных увидевших товар и открывших карточку в том же разрезе. Доля региона в покупателях не равна доле покупающих жителей, а заказы на покупателя не равны конверсии посетителя.\n2. **Кто выгоднее.** Нет GMV, валовой маржи, возвратов, стоимости доставки, рекламных затрат и инкрементального CAC по полу/возрасту/региону. Большая группа и высокая частота не гарантируют большую прибыль.\n3. **Какая категория нужна группе.** Нет совместного разреза демография × категория и география × категория. Нельзя приписывать женщинам одежду, мужчинам электронику, а региону локальные товары по стереотипу.\n4. **Кто повторный и кто удержан.** Средняя частота не даёт распределения заказов по людям. Дополнительные заказы сверх первого — не число повторных покупателей. Retention требует когорт и повторных периодов, которых в этих листах нет.\n5. **Причина отличий и эффект продукта.** Нет экспериментального назначения, до/после и контрольных групп. Возраст, ассортимент, реклама и маршрут покупки могут различаться одновременно. Разница частоты не доказывает, что покупка на партнёре удобнее или конкретная функция повысит продажи на определённый процент.\n6. **Рынок в целом.** Данные синтетические; нет описания репрезентативной выборки и весов. Нельзя экстраполировать доли на всех жителей, всех покупателей электронной торговли, более миллиона бизнес-сообществ или реальную выручку VK.\n7. **Число разных людей между каналами.** Нет покупательских ID и перекрытия. Сумма аудиторий не дедуплицирована, а доля Маркета в их арифметической сумме не является рыночной долей.\n8. **Точные пересечения групп.** Пол × возраст и регион — два отдельных среза. Нельзя перемножать их доли без предположения независимости, которое не проверено.\n9. **Наблюдаемость оплаты.** Поле «Оплаченные» не описано по полноте; у партнёра данных об оплате нет. Оформление не следует выдавать за завершённую покупку.\n10. **Географические причины.** Не определено, откуда взят регион: профиль, адрес доставки, IP, место жительства или другое. Особенности этих источников могут объяснять часть различий.\n")
    add(f"Для примера: {fnum(p['market_orders'] - p['market_buyers'])} заказов Маркета сверх одного на каждого показанного покупателя дают 39,71 дополнительного заказа на 100 покупателей. Это не 39,71% повторных покупателей. Итоговый лист отдельно сообщает 19% повторных покупателей Маркета и 26,6% партнёра для общего охвата; переносить эти доли на каждый демографический сегмент или регион нельзя.\n")
    add("## 11. Какие данные нужны для решений о таргетинге и росте\n")
    add(mdtable(["Недостающие данные", "Что они позволят проверить"], [
        ["Уникальные показы, открытия карточек, начатые и оформленные заказы по тем же полу/возрасту/региону и одному периоду", "Посчитать этапы конверсии и отделить низкую долю из-за охвата от низкой готовности покупать"],
        ["Пол × возраст × регион × категория × источник × маршрут; связь с ID бизнеса", "Найти реальные сочетания спроса и предложения, не выдумывая их из отдельных маргинальных таблиц"],
        ["Полный словарь статусов заказа/оплаты и аудит охвата разрезов, особенно 370 272 заказа партнёра", "Убедиться, что сравниваются одинаковые события, устранить противоречие общего итога"],
        ["Причины неизвестного региона и метод геопривязки; безопасное дополнение пропусков", "Проверить, насколько условная география партнёра отражает весь спрос"],
        ["Покупательские ID, число заказов каждого, дата первой и последующих покупок, пересечение каналов", "Рассчитать удержание, повторность, новые заказы, переключение маршрута и инкрементальный эффект"],
        ["GMV, чек, маржа, отмены, возвраты, доставка и способы оплаты по сегменту", "Определить денежную ценность и завершённость покупок, не выбирать аудиторию только по количеству"],
        ["Расходы и показы рекламных/органических кампаний, назначение экспериментальной группы", "Оценить CAC, дополнительный заказ, инкрементальную прибыль и причинный эффект продукта"],
        ["Региональные пользователи VK, аудитория электронной торговли/население и сопоставимые периоды", "Оценить проникновение с подходящим знаменателем и размеры доступного рынка"],
        ["Доступные товары, активные продавцы, наличие и качество исполнения заказов по регионам", "Выбрать место пилота с нужным предложением, логистикой и возможностью бизнеса обслужить спрос"],
        ["Метод формирования синтетической выборки и возможные ограничения вывода", "Понимать, какие закономерности организаторы разрешают использовать как учебные предпосылки"]
    ]) + "\n")
    add("## 12. Осторожные, проверяемые гипотезы для решения кейса\n")
    add("Это наши гипотезы, а не прямые требования организаторов и не доказанные причинные выводы. Их следует состыковать с листом «Бизнесы», инструментами и товарными категориями.\n")
    add(mdtable(["Гипотеза", "Основание в двух листах", "Как проверить и что может опровергнуть"], [
        ["Для первого пилота целесообразно искать бизнесы, уже обслуживающие крупные возрастные группы спроса Маркета", "25–44 дают 57,02% покупателей; женщины 25–44 — 40,89%", "Сопоставить реальные категории/бизнесы и их экономику с этой аудиторией. Гипотезу опровергнут низкая маржа, отсутствие активных подходящих продавцов или слабая конверсия дополнительного охвата"],
        ["Маркет имеет отличающийся профиль молодых покупателей, который может быть полезен бизнесам как дополнение к партнёрскому маркетплейсу", "18–34: 44,10% против 21,77%; мужчины 18–24 имеют индекс состава 4,69", "Проверить одинаковые категории, источники, чекауты и пересечение людей. Если различие вызвано исключительно ассортиментом/трафиком и не даёт дополнительных заказов, отдельное позиционирование по возрасту не подтверждается"],
        ["Некоторые регионы полезны как площадка для изучения уже работающих сценариев", "Челябинская, Новгородская, Пермская, Курская области/край и Удмуртия относительно заметны среди известных регионов", "Сначала установить, какие конкретные бизнесы и категории дают результат; проверить известность региона и логистику. Не объявлять эти регионы недоохваченными или прибыльными без дополнительных знаменателей"],
        ["Потенциал повышения частоты не объясняется одной молодостью аудитории", "Возрастная стандартизация оставляет около 90,9% разрыва частоты в компоненте различий внутри возрастов", "Проверить повторяемые категории, сроки доставки, статусы и каналы. Затем A/B-тестировать конкретный механизм повторной покупки. Размер среднего разрыва нельзя брать как ожидаемый uplift функции"],
        ["Прежде чем перераспределять географический бюджет, полезно улучшить диагностику географии", "36,71% неизвестных регионов партнёра против 1,75% Маркета; СПб меняется от сырого индекса 1,56 к условному 1,003", "Установить механизм пропусков и пересчитать на одинаковом измерении. Если восстановленная география меняет приоритеты, прежний таргетинг опирался на измерение, а не спрос"],
        ["Единые определения заказа и покупателя должны предшествовать прогнозам эффекта", "У 172 исключённых покупателей формально остаются 370 272 заказа партнёра", "Запросить правила агрегации и провести сверку по исходным событиям. Возможны ошибка, другой охват или экстремальная активность; без проверки нельзя использовать разрыв частот в бизнес-кейсе"]
    ]) + "\n")
    add("## 13. Как требования кейса ограничивают выводы\n")
    add(mdtable(["Страница PDF", "Прямое требование/условие", "Следствие для этого анализа"], [[r['page'], r['direct'], r['implication']] for r in result['case_requirements_affecting_analysis']]) + "\n")
    add("Особенно важно не подменить выбор целевых бизнесов выбором пола покупателей. Правильная цепочка аргументации: наблюдаемый спрос → проверенные товарные/торговые сценарии → конкретные бизнесы и их барьеры → инструмент → дополнительный оформленный внутренний заказ и эффект для бизнеса/VK. В этих двух листах уверенно наблюдается только первая часть цепочки.\n")
    add("## 14. Воспроизводимость\n")
    add("`buyers_analysis.py` читает исходную книгу и записывает только JSON и этот Markdown. В `buyers_results.json` сохранены все исходные числовые строки, доли покупателей и заказов, ранги, индексы, условные доли по известным/общим регионам, границы географических долей при неопределённом регионе, концентрация, проверки и определения. `null` означает отсутствующее/подавленное значение или неопределимый результат. Все доли в JSON хранятся в шкале 0–1; поля `*_pp` — в процентных пунктах.\n")
    add("Формулы: `частота = сумма заказов / сумма покупателей`; `доля = группа / опубликованный итог своего канала`; `индекс = доля Маркета / доля партнёра`; `HHI = сумма квадратов долей`. Проверено точное совпадение четырёх сумм между обоими разрезами и контрольными строками, а также неизменность SHA-256 исходной книги до/после анализа.\n")
    add("Внешние источники и реальные показатели VK для этих расчётов не использовались. Текст отделяет прямые данные и условия от интерпретаций и проверяемых гипотез.\n")
    (output_dir / "buyers_findings.md").write_text("\n".join(md), encoding="utf-8")


if __name__ == "__main__":
    main()
