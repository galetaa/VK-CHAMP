"""Reproduce read-only analysis of the four aggregate VK CaseChamp sheets.

Input workbook is never modified. Outputs are JSON and a human-readable Markdown
report in the same directory as this script. Python runtime: pandas, numpy,
openpyxl; no workbook authoring is performed.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, date
import math
import numpy as np
import pandas as pd
import openpyxl

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "source" / "case_dataset.xlsx"
WB = openpyxl.load_workbook(SOURCE, data_only=True, read_only=True)
NAMES = ["Маркет Вконтакте — итоги", "Маркет Вконтакте — по дням", "Маркет Вконтакте — источники", "Маркет Вконтакте — категории"]


def native(v):
    if isinstance(v, dict):
        return {str(k): native(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [native(x) for x in v]
    if isinstance(v, (pd.Timestamp, datetime, date)):
        return v.isoformat()
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        return None if not np.isfinite(v) else float(v)
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def ratio(n, d):
    return n / d if d and pd.notna(d) else None


def pct_change(a, b):
    return b / a - 1 if a else None


def frame(name):
    vals = list(WB[name].values)
    return pd.DataFrame(vals[1:], columns=vals[0])


daily = frame(NAMES[1])
daily.columns = ["date", "dau_market", "users_saw_products", "users_opened_cards", "search_queries", "search_queries_with_card", "impressions_vk", "impressions_partner", "opens_vk", "opens_partner", "buttons_vk", "buttons_partner", "orders_vk", "orders_partner", "paid_orders_vk", "gmv_vk", "gmv_partner", "buyers_vk", "buyers_partner"]
daily["date"] = pd.to_datetime(daily.date)
daily["weekday_num"] = daily.date.dt.weekday
daily["weekend"] = daily.weekday_num >= 5
daily["excel_row"] = daily.index + 2
raw_daily_columns = list(daily.columns[1:19])
summary = WB[NAMES[0]]
src = frame(NAMES[2])
src.columns = ["source", "opens_vk", "share_vk_input_pct", "opens_partner", "share_partner_input_pct"]
src["excel_row"] = src.index + 2
cat = frame(NAMES[3])
cat.columns = ["category", "opens_vk", "orders_vk", "opens_partner", "orders_partner"]
cat["excel_row"] = cat.index + 2

S = lambda a: summary[a].value
monthly = {
    "vk": {"impressions": S("B2"), "opens": S("B3"), "buttons": S("B4"), "direct_external_visits": None, "orders": S("B6"), "paid_orders_recorded": S("B7"), "gmv": S("B8"), "aov_input": S("B9"), "buyers": S("B10"), "orders_per_buyer_input": S("B11"), "repeat_buyer_share": S("B12")/100, "imp_card_input": S("B13")/100, "card_order_input": S("B14")/100, "imp_order_input": S("B15")/100, "buyers_disaggregated": S("B16"), "orders_disaggregated": S("B17")},
    "partner": {"impressions": S("C2"), "opens": S("C3"), "buttons": S("C4"), "direct_external_visits": S("C5"), "orders": S("C6"), "paid_orders_recorded": None, "gmv": S("C8"), "aov_input": S("C9"), "buyers": S("C10"), "orders_per_buyer_input": S("C11"), "repeat_buyer_share": S("C12")/100, "imp_card_input": S("C13")/100, "card_order_input": S("C14")/100, "imp_order_input": S("C15")/100, "buyers_disaggregated": S("C16"), "orders_disaggregated": S("C17")},
    "general": {"period": S("D19"), "dau_input": S("D20"), "mau": S("D21"), "users_saw_products": S("D22"), "users_opened_cards": S("D23"), "opens_from_posts_clips": S("D24"), "opens_referral_posts_clips": S("D25"), "search_queries": S("D26"), "search_open_input": S("D27")/100},
}

out = {
    "source_path": "source/case_dataset.xlsx",
    "scope": "Four aggregate worksheets only. Descriptive analysis; no causal identification.",
    "raw_cells": {},
    "monthly": monthly,
}
for name in NAMES:
    sh = WB[name]
    out["raw_cells"][name] = [{"address": c.coordinate, "value": c.value, "number_format": c.number_format} for row in sh for c in row if c.value is not None]

quality = {"daily_dates_count": len(daily), "daily_dates_unique": int(daily.date.nunique()), "date_min": daily.date.min(), "date_max": daily.date.max(), "daily_missing_by_column": daily[raw_daily_columns].isna().sum().to_dict(), "daily_negative_by_column": (daily[raw_daily_columns] < 0).sum().to_dict()}
check_map = [("impressions_vk", "B2"), ("impressions_partner", "C2"), ("opens_vk", "B3"), ("opens_partner", "C3"), ("buttons_vk", "B4"), ("buttons_partner", "C4"), ("orders_vk", "B6"), ("orders_partner", "C6"), ("paid_orders_vk", "B7"), ("gmv_vk", "B8"), ("gmv_partner", "C8"), ("search_queries", "D26")]
quality["daily_monthly_reconciliations"] = [{"metric": c, "daily_range": f"{openpyxl.utils.get_column_letter(daily.columns.get_loc(c)+1)}2:{openpyxl.utils.get_column_letter(daily.columns.get_loc(c)+1)}31", "daily_sum": daily[c].sum(), "summary_cell": a, "summary_value": S(a), "difference": daily[c].sum()-S(a)} for c,a in check_map]
quality["dau_mean"] = {"calculated": daily.dau_market.mean(), "summary": S("D20"), "difference": daily.dau_market.mean()-S("D20")}
quality["search_conversion"] = {"numerator": daily.search_queries_with_card.sum(), "denominator": daily.search_queries.sum(), "ratio": daily.search_queries_with_card.sum()/daily.search_queries.sum(), "input_ratio": S("D27")/100}
quality["unique_counts_nonadditive"] = {c: {"daily_sum": daily[c].sum(), "daily_mean": daily[c].mean(), "monthly_unique": m, "daily_sum_per_monthly_unique": daily[c].sum()/m} for c,m in [("buyers_vk",S("B10")),("buyers_partner",S("C10")),("users_saw_products",S("D22")),("users_opened_cards",S("D23"))]}
out["quality"] = quality

for side in ["vk", "partner"]:
    m = monthly[side]
    m["derived"] = {
        "impressions_to_opens_event_ratio": m["opens"]/m["impressions"],
        "opens_to_buttons_event_ratio": m["buttons"]/m["opens"],
        "orders_per_open_event_ratio": m["orders"]/m["opens"],
        "orders_per_button_event_ratio": m["orders"]/m["buttons"],
        "orders_per_impression_event_ratio": m["orders"]/m["impressions"],
        "aov_gmv_per_formed_order": m["gmv"]/m["orders"],
        "orders_per_buyer": m["orders"]/m["buyers"],
        "gmv_per_buyer": m["gmv"]/m["buyers"],
        "gmv_per_1000_impressions": m["gmv"]/m["impressions"]*1000,
        "gmv_per_open": m["gmv"]/m["opens"],
        "recorded_paid_share": ratio(m["paid_orders_recorded"],m["orders"]) if m["paid_orders_recorded"] is not None else None,
        "buyers_disaggregation_coverage": m["buyers_disaggregated"]/m["buyers"],
        "orders_disaggregation_coverage": m["orders_disaggregated"]/m["orders"],
    }
    m["rounding_checks"] = {
        "aov_difference_rub": m["aov_input"]-m["derived"]["aov_gmv_per_formed_order"],
        "orders_per_buyer_difference": m["orders_per_buyer_input"]-m["derived"]["orders_per_buyer"],
        "impression_card_difference_pp": 100*(m["imp_card_input"]-m["derived"]["impressions_to_opens_event_ratio"]),
        "card_order_difference_pp": 100*(m["card_order_input"]-m["derived"]["orders_per_open_event_ratio"]),
        "impression_order_difference_pp": 100*(m["imp_order_input"]-m["derived"]["orders_per_impression_event_ratio"]),
    }

out["monthly_cross_channel_descriptive"] = {
    "partner_to_vk": {k: monthly["partner"][k]/monthly["vk"][k] for k in ["impressions", "opens", "buttons", "orders", "gmv", "buyers"]},
    "partner_to_vk_derived": {k: monthly["partner"]["derived"][k]/monthly["vk"]["derived"][k] for k in ["impressions_to_opens_event_ratio","opens_to_buttons_event_ratio","orders_per_open_event_ratio","orders_per_impression_event_ratio","aov_gmv_per_formed_order","orders_per_buyer","gmv_per_buyer","gmv_per_1000_impressions"]},
    "repeat_share_difference_pp": 100*(monthly["partner"]["repeat_buyer_share"]-monthly["vk"]["repeat_buyer_share"]),
    "warning": "Differences describe channel totals, not causal tool effects or matched conversion funnels.",
}
out["general_derived"] = {
    "dau_mau_ratio": S("D20")/S("D21"),
    "monthly_card_openers_per_monthly_product_viewers": S("D23")/S("D22"),
    "all_card_events_per_monthly_card_opener": (S("B3")+S("C3"))/S("D23"),
    "all_product_impressions_per_monthly_product_viewer": (S("B2")+S("C2"))/S("D22"),
    "referral_share_of_reported_posts_clips_opens": S("D25")/S("D24"),
    "nonreferral_posts_clips_difference_events": S("D24")-S("D25"),
    "search_queries_per_market_mau_if_same_scope": S("D26")/S("D21"),
    "scope_warning": "Only DAU/MAU labels clearly reference the same service; other mixed-surface quotients require scope confirmation. Event ratios are not unique-person conversion probabilities.",
}
out["partner_bypass"] = {
    "direct_events": S("C5"), "card_open_events": S("C3"), "direct_to_card_event_ratio": S("C5")/S("C3"),
    "direct_share_of_sum_entry_events": S("C5")/(S("C5")+S("C3")),
    "orders_divided_by_cards_plus_direct_events_descriptive_only": S("C6")/(S("C3")+S("C5")),
    "orders_divided_by_buttons_plus_direct_events_descriptive_only": S("C6")/(S("C4")+S("C5")),
    "warning": "Alternative denominators are sensitivity illustrations, not corrected conversions: attribution and overlap are unknown.",
}

ratio_specs = {
    "imp_open_vk": ("opens_vk","impressions_vk"),
    "imp_open_partner": ("opens_partner","impressions_partner"),
    "open_button_vk": ("buttons_vk","opens_vk"),
    "open_button_partner": ("buttons_partner","opens_partner"),
    "open_order_vk": ("orders_vk","opens_vk"),
    "open_order_partner": ("orders_partner","opens_partner"),
    "button_order_vk": ("orders_vk","buttons_vk"),
    "button_order_partner": ("orders_partner","buttons_partner"),
    "imp_order_vk": ("orders_vk","impressions_vk"),
    "imp_order_partner": ("orders_partner","impressions_partner"),
    "paid_order_share_vk": ("paid_orders_vk","orders_vk"),
    "aov_vk": ("gmv_vk","orders_vk"),
    "aov_partner": ("gmv_partner","orders_partner"),
    "orders_per_daily_buyer_vk": ("orders_vk","buyers_vk"),
    "orders_per_daily_buyer_partner": ("orders_partner","buyers_partner"),
    "search_open_ratio": ("search_queries_with_card","search_queries"),
    "daily_unique_openers_per_viewers": ("users_opened_cards","users_saw_products"),
}
for k,(n,d) in ratio_specs.items():
    daily[k] = daily[n]/daily[d]
out["daily_ratio_definitions"] = {k:{"numerator":n,"denominator":d,"warning":"Unlinked aggregate quotient; partner card/order ratios include a bypass route."} for k,(n,d) in ratio_specs.items()}


def summarize_group(g):
    return {"days":len(g), "start":g.date.min(), "end":g.date.max(), "totals":g[raw_daily_columns].sum().to_dict(), "per_day_means":g[raw_daily_columns].mean().to_dict(), "medians":g[raw_daily_columns].median().to_dict(), "ratios_of_sums":{k:g[n].sum()/g[d].sum() for k,(n,d) in ratio_specs.items()}}


groups = {
    "all_30_days":daily,
    "first_7_days":daily.iloc[:7],
    "last_7_days":daily.iloc[-7:],
    "first_15_days":daily.iloc[:15],
    "last_15_days":daily.iloc[15:],
    "weekday":daily[~daily.weekend],
    "weekend":daily[daily.weekend],
    "all_except_september_1":daily.iloc[1:],
    "september_2_to_8":daily.iloc[1:8],
    "september_8_to_14":daily.iloc[7:14],
    "september_15_to_21":daily.iloc[14:21],
    "september_22_to_28":daily.iloc[21:28],
}
out["daily_groups"] = {k:summarize_group(g) for k,g in groups.items()}
out["weekday_groups"] = {str(k):summarize_group(g) for k,g in daily.groupby("weekday_num")}
comparison_defs = [("first7_to_last7","first_7_days","last_7_days"),("first15_to_last15","first_15_days","last_15_days"),("weekday_to_weekend","weekday","weekend"),("sep2_8_to_last7","september_2_to_8","last_7_days")]
out["daily_comparisons"] = {}
for key, a, b in comparison_defs:
    ga=out["daily_groups"][a]; gb=out["daily_groups"][b]
    out["daily_comparisons"][key] = {
        "base":a,"comparison":b,"days_base":ga["days"],"days_comparison":gb["days"],
        "per_day_means":{k:{"base":ga["per_day_means"][k],"comparison":gb["per_day_means"][k],"relative_change":pct_change(ga["per_day_means"][k],gb["per_day_means"][k])} for k in raw_daily_columns},
        "ratios_of_sums":{k:{"base":ga["ratios_of_sums"][k],"comparison":gb["ratios_of_sums"][k],"absolute_change":gb["ratios_of_sums"][k]-ga["ratios_of_sums"][k],"relative_change":pct_change(ga["ratios_of_sums"][k],gb["ratios_of_sums"][k])} for k in ratio_specs},
    }

daily_stats = {}
for col in raw_daily_columns + list(ratio_specs):
    a=daily[col]
    q1,q3=a.quantile(.25),a.quantile(.75); iqr=q3-q1
    slope,intercept=np.polyfit(np.arange(len(daily)),a.to_numpy(),1)
    fitted=intercept+slope*np.arange(len(daily))
    denom=((a-a.mean())**2).sum()
    r2=1-((a-fitted)**2).sum()/denom if denom else None
    mad=(a-a.median()).abs().median()
    flags=daily[(a<q1-1.5*iqr)|(a>q3+1.5*iqr)]
    daily_stats[col]={
        "sum_only_valid_for_additive_metrics":a.sum(),"mean":a.mean(),"median":a.median(),"std_sample":a.std(),"coefficient_of_variation":ratio(a.std(),a.mean()),"q1":q1,"q3":q3,"min":a.min(),"min_dates":daily.loc[a==a.min(),"date"].tolist(),"max":a.max(),"max_dates":daily.loc[a==a.max(),"date"].tolist(),
        "linear_slope_per_day":slope,"linear_slope_as_share_of_mean":ratio(slope,a.mean()),"descriptive_linear_r_squared":r2,
        "iqr_low_bound":q1-1.5*iqr,"iqr_high_bound":q3+1.5*iqr,"iqr_outlier_rows":flags[["date","excel_row",col]].to_dict("records"),
        "median_absolute_deviation":mad,
    }
out["daily_metric_statistics"] = daily_stats
out["daily_all_rows_with_derived_metrics"] = daily.to_dict("records")

out["first_last_7_gmv_arithmetic_decomposition"]={}
for side in ["vk","partner"]:
    g0=out["daily_groups"]["first_7_days"]["totals"];g1=out["daily_groups"]["last_7_days"]["totals"]
    o0,o1=g0[f"orders_{side}"],g1[f"orders_{side}"];v0,v1=g0[f"gmv_{side}"],g1[f"gmv_{side}"];a0,a1=v0/o0,v1/o1
    out["first_last_7_gmv_arithmetic_decomposition"][side]={"orders_first7":o0,"orders_last7":o1,"gmv_first7":v0,"gmv_last7":v1,"aov_first7":a0,"aov_last7":a1,"gmv_change":v1-v0,"orders_component_symmetric":(o1-o0)*(a0+a1)/2,"aov_component_symmetric":(a1-a0)*(o0+o1)/2,"method":"Exact two-factor arithmetic decomposition using mean counterpart factor. It does not identify causes."}

source_primary=src[src.excel_row<=7].copy()
for side in ["vk","partner"]:
    col=f"opens_{side}"
    source_primary[f"calculated_share_{side}"]=source_primary[col]/monthly[side]["opens"]
    source_primary[f"reported_minus_calculated_pp_{side}"]=source_primary[f"share_{side}_input_pct"]-100*source_primary[f"calculated_share_{side}"]
out["sources"]={
    "all_rows":src.to_dict("records"),
    "primary_rows_calculated":source_primary.to_dict("records"),
    "primary_totals":{side:int(source_primary[f"opens_{side}"].sum()) for side in ["vk","partner"]},
    "primary_reported_share_sums_pct":{side:source_primary[f"share_{side}_input_pct"].sum() for side in ["vk","partner"]},
    "feed_clips":{side:{"opens":src.loc[src.source.isin(["Лента","Клипы"]),f"opens_{side}"].sum(),"share_total_opens":src.loc[src.source.isin(["Лента","Клипы"]),f"opens_{side}"].sum()/monthly[side]["opens"]} for side in ["vk","partner"]},
    "standalone_home":{side:{"opens":int(src.loc[src.excel_row==7,f"opens_{side}"].iloc[0]),"share_total_opens":src.loc[src.excel_row==7,f"opens_{side}"].iloc[0]/monthly[side]["opens"]} for side in ["vk","partner"]},
    "reported_content_opens":S("D24"),
    "feed_clips_total_both_sides":int(src.loc[src.source.isin(["Лента","Клипы"]),["opens_vk","opens_partner"]].sum().sum()),
    "referral_partner_row8":{"opens":int(src.iloc[6].opens_partner),"share_partner_opens":src.iloc[6].opens_partner/monthly["partner"]["opens"],"excess_over_partner_feed_plus_clips":int(src.iloc[6].opens_partner-src.loc[src.source.isin(["Лента","Клипы"]),"opens_partner"].sum())},
    "warning":"Row 8 is nested and excluded from primary sums. Feed vs posts and surface/attribution definitions remain unresolved.",
}
out["sources"]["feed_clips_minus_reported_content"] = out["sources"]["feed_clips_total_both_sides"]-S("D24")

for side in ["vk","partner"]:
    cat[f"orders_per_open_{side}"]=cat[f"orders_{side}"]/cat[f"opens_{side}"].replace(0,np.nan)
    for metric in ["opens","orders"]:
        col=f"{metric}_{side}"
        cat[f"{metric}_share_category_sum_{side}"]=cat[col]/cat[col].sum()
        cat[f"{metric}_share_official_monthly_{side}"]=cat[col]/monthly[side][metric]
    cat[f"order_share_minus_open_share_pp_{side}"]=100*(cat[f"orders_share_category_sum_{side}"]-cat[f"opens_share_category_sum_{side}"])
    cat[f"orders_per_open_relative_to_category_average_{side}"]=cat[f"orders_per_open_{side}"]/(cat[f"orders_{side}"].sum()/cat[f"opens_{side}"].sum())
cat["partner_to_vk_orders_per_open_ratio"]=cat.orders_per_open_partner/cat.orders_per_open_vk.replace(0,np.nan)

out["categories"]={"all_rows":cat.to_dict("records"),"totals_and_reconciliation":{},"concentration":{},"sensitivity":{},"mix_standardization_descriptive":{}}
for side in ["vk","partner"]:
    out["categories"]["totals_and_reconciliation"][side]={}
    for metric in ["opens","orders"]:
        val=cat[f"{metric}_{side}"].sum();control=monthly[side][metric]
        out["categories"]["totals_and_reconciliation"][side][metric]={"category_sum":val,"monthly_total":control,"difference":val-control,"relative_difference":val/control-1}
        col=f"{metric}_{side}";ranked=cat.sort_values(col,ascending=False)
        shares=ranked[col]/ranked[col].sum()
        out["categories"]["concentration"][col]={"ranking":ranked[["category","excel_row",col,f"{metric}_share_category_sum_{side}",f"{metric}_share_official_monthly_{side}"]].to_dict("records"),"top1_share_category_sum":shares.iloc[:1].sum(),"top2_share_category_sum":shares.iloc[:2].sum(),"top3_share_category_sum":shares.iloc[:3].sum(),"top5_share_category_sum":shares.iloc[:5].sum(),"hhi_unit_scale":(shares**2).sum()}

scenario_masks={
    "all_18_rows":pd.Series(True,index=cat.index),
    "exclude_undefined":cat.category!="Не определена",
    "exclude_generic_other_all_undefined":~cat.category.isin(["Другое","Все товары","Не определена"]),
    "common_positive_card_opens":(cat.opens_vk>0)&(cat.opens_partner>0),
    "exclude_vk_top2_order_categories_food_hobby":~cat.category.isin(["Продукты питания","Хобби и развлечения"]),
}
for name,mask in scenario_masks.items():
    g=cat[mask]
    item={"n_categories":len(g),"included_categories":g.category.tolist()}
    for side in ["vk","partner"]:
        item[side]={"opens":g[f"opens_{side}"].sum(),"orders":g[f"orders_{side}"].sum(),"orders_per_open_aggregate":g[f"orders_{side}"].sum()/g[f"opens_{side}"].sum(),"open_share_of_full_category_sum":g[f"opens_{side}"].sum()/cat[f"opens_{side}"].sum(),"order_share_of_full_category_sum":g[f"orders_{side}"].sum()/cat[f"orders_{side}"].sum()}
    item["partner_vk_orders_per_open_ratio"]=item["partner"]["orders_per_open_aggregate"]/item["vk"]["orders_per_open_aggregate"]
    out["categories"]["sensitivity"][name]=item

for sample_name,mask in [("common_positive_card_opens",scenario_masks["common_positive_card_opens"]),("exclude_generic_other_all_undefined",scenario_masks["exclude_generic_other_all_undefined"])]:
    g=cat[mask].copy()
    cases={"vk_open_mix":g.opens_vk/g.opens_vk.sum(),"partner_open_mix":g.opens_partner/g.opens_partner.sum(),"pooled_open_mix":(g.opens_vk+g.opens_partner)/(g.opens_vk+g.opens_partner).sum(),"equal_category_mix":pd.Series(1/len(g),index=g.index)}
    z={}
    for mix,w in cases.items():
        rv=(w*g.orders_per_open_vk).sum();rp=(w*g.orders_per_open_partner).sum()
        z[mix]={"vk_weighted_orders_per_open":rv,"partner_weighted_orders_per_open":rp,"partner_vk_ratio":rp/rv,"weights_by_category":dict(zip(g.category,w))}
    out["categories"]["mix_standardization_descriptive"][sample_name]=z
out["categories"]["mix_standardization_warning"]="Sensitivity arithmetic only: categories may overlap, orders are not linked to card cohorts, partner bypass exists, zero-open categories are excluded. Standardization does not estimate treatment effect or a transferable conversion benchmark."

out["limitations"]=[
    "Synthetic anonymized case data, not actual VK performance; one month only.",
    "Metric rows are aggregates with no event/user/order identifiers or attribution links.",
    "Partner has a direct bypass route absent from daily data; all orders/card opens is not a linked conversion.",
    "Paid-order scope, payment coverage and cohort timing are unknown; no abandonment inference.",
    "Categories do not reconcile to official totals; counts may have different scopes or overlap.",
    "Sources use nonidentical surface labels; referral row is nested and must not be added.",
    "Unique daily users cannot be summed into monthly unique counts. Segment buyers may overlap.",
    "No costs, margin, commission, paid GMV, cancellations/returns or retention cohorts available in these sheets.",
    "Daily trend and weekend differences are descriptive for September only; no causal or long-term seasonal inference.",
]

def n(v,d=0):
    if v is None or pd.isna(v): return "н/д"
    return f"{v:,.{d}f}".replace(","," ").replace(".",",")

def pc(v,d=2):
    if v is not None and 0 < abs(100*v) < 10**(-d):
        d = 6
    return n(100*v,d)+"%" if v is not None else "н/д"

def mdtable(headers,rows):
    return "| "+" | ".join(headers)+" |\n| "+" | ".join(["---"]*len(headers))+" |\n"+"\n".join("| "+" | ".join(map(str,r))+" |" for r in rows)+"\n"

labels_ru = {
    "dau_market":"DAU Маркета", "users_saw_products":"Видели товары, уникальные за день", "users_opened_cards":"Открывали карточки, уникальные за день", "search_queries":"Поисковые запросы", "search_queries_with_card":"Поисковые запросы с открытием", "impressions_vk":"Показы VK", "impressions_partner":"Показы Партнёра", "opens_vk":"Открытия карточек VK", "opens_partner":"Открытия карточек Партнёра", "buttons_vk":"Нажатия кнопок VK", "buttons_partner":"Нажатия кнопок Партнёра", "orders_vk":"Заказы VK", "orders_partner":"Заказы Партнёра", "paid_orders_vk":"Учтённые оплаченные заказы VK", "gmv_vk":"GMV VK, ₽", "gmv_partner":"GMV Партнёра, ₽", "buyers_vk":"Покупатели VK, уникальные за день", "buyers_partner":"Покупатели Партнёра, уникальные за день",
    "imp_open_vk":"Открытия / показы VK", "imp_open_partner":"Открытия / показы Партнёра", "open_button_vk":"Кнопки / открытия VK", "open_button_partner":"Кнопки / открытия Партнёра", "open_order_vk":"Заказы / открытия VK", "open_order_partner":"Все заказы / открытия Партнёра", "button_order_vk":"Заказы / кнопки VK", "button_order_partner":"Все заказы / кнопки Партнёра", "imp_order_vk":"Заказы / показы VK", "imp_order_partner":"Заказы / показы Партнёра", "paid_order_share_vk":"Учтённые оплаченные / оформленные VK", "aov_vk":"GMV / заказы VK, ₽", "aov_partner":"GMV / заказы Партнёра, ₽", "orders_per_daily_buyer_vk":"Заказы / дневные покупатели VK", "orders_per_daily_buyer_partner":"Заказы / дневные покупатели Партнёра", "search_open_ratio":"Поиск с открытием / весь поиск", "daily_unique_openers_per_viewers":"Дневные открывавшие / видевшие",
    "all_18_rows":"Все 18 строк", "exclude_undefined":"Без «Не определена»", "exclude_generic_other_all_undefined":"Без «Другое», «Все товары», «Не определена»", "common_positive_card_opens":"Положительные открытия в обоих каналах", "exclude_vk_top2_order_categories_food_hobby":"Без продуктов питания и хобби",
}

lines=["# Полный анализ агрегатных листов VK CaseChamp 2026", "", "Расчёты воспроизводятся скриптом `agg_analysis.py`; все исходные ячейки, дневные значения и промежуточные результаты находятся в `agg_results.json`. Исходный XLSX не изменён. Период — 1–30 сентября 2026 года. Данные синтетические. Ниже описательные отношения агрегатов не интерпретируются как причинные эффекты.", "", "## 1. Полнота и согласованность", "", "Все 30 дат уникальны и идут без пропусков. Во всех 18 числовых дневных полях отсутствуют пустые и отрицательные значения. Суммы дневных показов, открытий, кнопок, заказов, учтённых оплаченных заказов, GMV VK и поисковых запросов совпадают с месячными итогами. GMV Партнёра по дням меньше итоговой ячейки C8 на 1 ₽.", "", f"DAU: среднее B2:B31 = {n(daily.dau_market.mean(),2)}, округлённый итог D20 = {n(S('D20'))}. Поиск: {n(daily.search_queries_with_card.sum())} запросов с открытием / {n(daily.search_queries.sum())} всех запросов = {pc(quality['search_conversion']['ratio'],4)}; итог D27 = 18,4%.", "", "## 2. Все месячные показатели и их смысл", ""]
monthly_labels=[("Показы товаров","impressions"),("Открытия карточек","opens"),("Нажатия кнопки","buttons"),("Прямые переходы без карточки","direct_external_visits"),("Оформленные заказы","orders"),("Учтённые оплаченные заказы","paid_orders_recorded"),("GMV оформленных заказов, ₽","gmv"),("Средний чек в исходнике, ₽","aov_input"),("Уникальные покупатели","buyers"),("Заказов на покупателя в исходнике","orders_per_buyer_input")]
lines.append(mdtable(["Показатель","VK","Партнёр"],[[label,n(monthly['vk'][k],1 if 'per_buyer' in k else 0),n(monthly['partner'][k],1 if 'per_buyer' in k else 0)] for label,k in monthly_labels]))
lines += [f"Повторные покупатели (2+ заказа): {pc(monthly['vk']['repeat_buyer_share'],1)} VK и {pc(monthly['partner']['repeat_buyer_share'],1)} Партнёра; разница 7,6 п.п. Это повторение в пределах месяца, не когортное удержание.", "", "### Отношения месячных событий", ""]
derived_labels=[("Открытия / показы","impressions_to_opens_event_ratio",True),("Кнопки / открытия","opens_to_buttons_event_ratio",True),("Все заказы / открытия","orders_per_open_event_ratio",True),("Все заказы / кнопки","orders_per_button_event_ratio",True),("Заказы / показы","orders_per_impression_event_ratio",True),("GMV / оформленные заказы, ₽","aov_gmv_per_formed_order",False),("Заказы / уникальные покупатели","orders_per_buyer",False),("GMV / покупатели, ₽","gmv_per_buyer",False),("GMV на 1000 показов, ₽","gmv_per_1000_impressions",False),("GMV / открытие карточки, ₽","gmv_per_open",False)]
lines.append(mdtable(["Отношение","VK","Партнёр"],[[label,(pc if isp else n)(monthly['vk']['derived'][k],4 if isp else 2),(pc if isp else n)(monthly['partner']['derived'][k],4 if isp else 2)] for label,k,isp in derived_labels]))
lines += ["Все опубликованные коэффициенты и средние месячного листа согласуются с расчётами с учётом округления. Однако арифметическая правильность отношения не делает его конверсией одной связанной воронки.", "", f"Партнёр имеет {n(S('C5'))} прямых переходов без открытия карточки (C5), что в {n(S('C5')/S('C3'),3)} раза превышает открытия карточек. Все заказы / открытия дают {pc(monthly['partner']['derived']['orders_per_open_event_ratio'],4)}. Если механически добавить прямые переходы к открытиям, коэффициент станет {pc(out['partner_bypass']['orders_divided_by_cards_plus_direct_events_descriptive_only'],4)}, а при делении на кнопки + прямые переходы — {pc(out['partner_bypass']['orders_divided_by_buttons_plus_direct_events_descriptive_only'],4)}. Эти два альтернативных значения служат только демонстрацией чувствительности к знаменателю: считать их исправленной конверсией нельзя без правил атрибуции и пересечений.", "", f"B7/B6 = {n(S('B7'))}/{n(S('B6'))} = {pc(monthly['vk']['derived']['recorded_paid_share'],4)}. Это доля учтённых оплаченных заказов. Она не доказывает, что остальные заказы брошены: неизвестны способы оплаты, покрытие учёта, оплаты при получении и лаги. У Партнёра показатель отсутствует. GMV — валовая стоимость оформленных заказов, не выручка VK и не оплаченный оборот.", "", "### Общие показатели и аудитории", ""]
generalrows=[["DAU Маркета",n(S('D20')),"D20"],["MAU Маркета",n(S('D21')),"D21"],["Видели товары за месяц, уникальные",n(S('D22')),"D22"],["Открывали карточку за месяц, уникальные",n(S('D23')),"D23"],["Открытия из постов/клипов",n(S('D24')),"D24"],["Из них реферальные",n(S('D25')),"D25"],["Поисковые запросы",n(S('D26')),"D26"],["Доля поиска с открытием",pc(S('D27')/100,1),"D27"]]
lines.append(mdtable(["Показатель","Значение","Ячейка Итогов"],generalrows))
lines += [f"DAU/MAU = {pc(out['general_derived']['dau_mau_ratio'],3)}. Месячные открывавшие карточку / видевшие товары = {pc(out['general_derived']['monthly_card_openers_per_monthly_product_viewers'],3)}, но это отношение требует подтверждения одинаковых границ аудиторий. MAU отдельного Маркета нельзя подставлять вместо охвата всех товарных поверхностей. Доля реферальных открытий в отдельно заявленных открытиях из постов и клипов D25/D24 = {pc(out['general_derived']['referral_share_of_reported_posts_clips_opens'],3)}; остаток = {n(S('D24')-S('D25'))} событий. С источниками трафика это определение не сведено (см. раздел 5).", "", "### Уникальные показатели неаддитивны", ""]
lines.append(mdtable(["Метрика","Сумма дневных уникальных","Уникальные за месяц","Отношение"],[[labels_ru[k],n(v['daily_sum']),n(v['monthly_unique']),n(v['daily_sum_per_monthly_unique'],3)] for k,v in quality['unique_counts_nonadditive'].items()]))
lines += ["Повторное появление человека в разные дни объясняет превышение суммы дневных уникальных над месячными. Суммирование покупателей VK и Партнёра также не даёт уникальную общую аудиторию, поскольку пересечения не раскрыты.", "", "## 3. Все дневные показатели: уровень, разброс и динамика", "", "Сравниваются 1–7 сентября (строки 2:8) и 24–30 сентября (строки 25:31). Каждый семидневный интервал содержит по одному дню каждой недели, поэтому состав дней недели совпадает. Специфические даты, кампании и изменение аудитории не контролируются. Все выводы относятся только к сентябрю 2026 года. Линейные наклоны и R² в JSON — описания ряда, не статистическое доказательство устойчивого тренда.", ""]
day_rows=[]
for k in raw_daily_columns:
    a=daily_stats[k];cp=out['daily_comparisons']['first7_to_last7']['per_day_means'][k]
    day_rows.append([labels_ru[k],n(a['mean'],1),n(a['median'],1),f"{n(a['min'])} ({pd.Timestamp(a['min_dates'][0]).strftime('%d.%m')})",f"{n(a['max'])} ({pd.Timestamp(a['max_dates'][0]).strftime('%d.%m')})",pc(cp['relative_change'],2)])
lines.append(mdtable(["Метрика","Среднее в день","Медиана","Минимум","Максимум","Последние 7 / первые 7 − 1"],day_rows))
lines += ["### Изменение отношений событий и среднего чека", ""]
ratio_rows=[]
for k in ratio_specs:
    cp=out['daily_comparisons']['first7_to_last7']['ratios_of_sums'][k]
    ismoney=k.startswith('aov') or 'per_daily_buyer' in k
    ratio_rows.append([labels_ru[k],n(cp['base'],3) if ismoney else pc(cp['base'],3),n(cp['comparison'],3) if ismoney else pc(cp['comparison'],3),pc(cp['relative_change'],2)])
lines.append(mdtable(["Отношение сумм за период","Первые 7 дней","Последние 7 дней","Относительное изменение"],ratio_rows))
lines += ["### Разложение изменения GMV", "", "GMV = число оформленных заказов × вычисленный средний чек. Симметричное разложение использует среднее значение второго множителя; сумма двух вкладов точно равна изменению GMV. Это арифметика, а не объяснение причин.", ""]
lines.append(mdtable(["Канал","GMV первых 7, ₽","GMV последних 7, ₽","Изменение, ₽","Компонент числа заказов, ₽","Компонент среднего чека, ₽"],[[s,n(z['gmv_first7']),n(z['gmv_last7']),n(z['gmv_change']),n(z['orders_component_symmetric']),n(z['aov_component_symmetric'])] for s,z in out['first_last_7_gmv_arithmetic_decomposition'].items()]))
lines += ["### Будни и выходные", "", f"В месяце {int((~daily.weekend).sum())} будних и {int(daily.weekend.sum())} выходных дней. Сравниваются средние за день, а не суммы разных по длине групп. Метрики отношений рассчитаны как отношение сумм. Различия могут отражать календарный состав, тренд, кампании и изменение ассортимента.", ""]
wkrows=[]
for k in raw_daily_columns:
    cp=out['daily_comparisons']['weekday_to_weekend']['per_day_means'][k]
    wkrows.append([labels_ru[k],n(cp['base'],1),n(cp['comparison'],1),pc(cp['relative_change'],2)])
lines.append(mdtable(["Метрика","Будний день","Выходной день","Разница"],wkrows))
lines += ["### Возможные выбросы и чувствительность к первому дню", "", "Ниже — точки вне 1,5 межквартильных размахов. Флаг не означает ошибку: он лишь выделяет день для объяснения. Для рядов с трендом этот критерий особенно осторожный.", ""]
flags=[]
for k in raw_daily_columns:
    for a in daily_stats[k]['iqr_outlier_rows']:
        flags.append([labels_ru[k],pd.Timestamp(a['date']).strftime('%d.%m'),a['excel_row'],n(a[k])])
lines.append(mdtable(["Метрика","Дата","Строка дневного листа","Значение"],flags) if flags else "По абсолютным дневным показателям флагов IQR нет.\n")
sens=[]
for k in ['orders_vk','gmv_vk','orders_partner','gmv_partner']:
    cp=out['daily_comparisons']['first7_to_last7']['per_day_means'][k]; cp2=out['daily_comparisons']['sep2_8_to_last7']['per_day_means'][k]
    sens.append([labels_ru[k],pc(cp['relative_change']),pc(cp2['relative_change'])])
lines.append(mdtable(["Метрика","24–30 сентября против 1–7","24–30 против 2–8"],sens))
lines += ["Этот пересчёт проверяет, не определяется ли результат одним 1 сентября. Интервал 2–8 сентября также содержит каждый день недели ровно один раз, однако меняется включённая дата и само положение окна. Это проверка чувствительности, а не оценка причинного эффекта даты.", "", "## 4. Детализации покупателей: контрольные ограничения", ""]
demorows=[]
for side in ['vk','partner']:
    m=monthly[side]
    demorows.append([side,n(m['buyers']),n(m['buyers_disaggregated']),n(m['buyers']-m['buyers_disaggregated']),n(m['orders']),n(m['orders_disaggregated']),n(m['orders']-m['orders_disaggregated'])])
lines.append(mdtable(["Канал","Все покупатели","В детализациях","Разница","Все заказы","В детализациях","Разница"],demorows))
lines += ["Строки 16–17 Итогов дают специальные контрольные суммы без групп меньше 100. У Партнёра разница 172 покупателя и 370 272 заказа означала бы 2 152,7 заказа на исключённого покупателя, если причина только в этих покупателях. Это требует объяснения периметра. Доли по демографии/регионам нужно считать в рамках той же детализации и раскрывать её охват.", "", "## 5. Все источники трафика", ""]
lines.append(mdtable(["Источник","Открытия VK","Доля VK, пересчёт","Открытия Партнёра","Доля Партнёра, пересчёт"],[[r.source,n(r.opens_vk),pc(r.calculated_share_vk,3),n(r.opens_partner),pc(r.calculated_share_partner,3)] for r in source_primary.itertuples()]))
lines += [f"Суммы строк 2:7 точно совпадают с общими открытиями карточек. Сумма опубликованных округлённых долей = 100% у каждого канала. VK получает из ленты и клипов {n(out['sources']['feed_clips']['vk']['opens'])} открытий ({pc(out['sources']['feed_clips']['vk']['share_total_opens'],3)}), Партнёр — {n(out['sources']['feed_clips']['partner']['opens'])} ({pc(out['sources']['feed_clips']['partner']['share_total_opens'],3)}).", "", f"Главная Маркета даёт {n(out['sources']['standalone_home']['vk']['opens'])} открытий VK ({pc(out['sources']['standalone_home']['vk']['share_total_opens'],3)}) и {n(out['sources']['standalone_home']['partner']['opens'])} Партнёра ({pc(out['sources']['standalone_home']['partner']['share_total_opens'],3)}). Это доли открытий, отнесённых к указанному источнику, не оценка всех функций Маркета и не доказательство ненужности витрины. Источник «Другое» у VK занимает {pc(source_primary.iloc[0].calculated_share_vk,3)}, поэтому значительная часть механики привлечения остаётся неразобранной.", "", f"Реферальная строка 8 вложенная: {n(src.iloc[6].opens_partner)} открытий, {pc(out['sources']['referral_partner_row8']['share_partner_opens'],3)} от всех открытий Партнёра. Её нельзя суммировать с основными источниками. Она превышает ленту+клипы Партнёра на {n(out['sources']['referral_partner_row8']['excess_over_partner_feed_plus_clips'])} событий. Лента+клипы обоих каналов = {n(out['sources']['feed_clips_total_both_sides'])}, а Итоги D24 («из постов и клипов») = {n(S('D24'))}, разница {n(out['sources']['feed_clips_minus_reported_content'])}. Разные названия не гарантируют одинаковое множество событий: требуется единая карта источников.", "", "## 6. Все товарные категории", "", "Ниже доли заказов рассчитаны от суммы заказов соответствующей таблицы категорий, не от общего месячного итога. Отношение заказов к открытиям — описательный коэффициент событий. У Партнёра есть путь без карточки, а категории не сведены к месячному итогу.", ""]
lines.append(mdtable(["Категория","Открытия VK","Заказы VK","Заказы/открытия VK","Доля заказов VK","Открытия Партнёра","Заказы Партнёра","Заказы/открытия Партнёра","Доля заказов Партнёра"],[[r.category,n(r.opens_vk),n(r.orders_vk),pc(r.orders_per_open_vk,3),pc(r.orders_share_category_sum_vk,2),n(r.opens_partner),n(r.orders_partner),pc(r.orders_per_open_partner,3) if pd.notna(r.orders_per_open_partner) else 'не определено',pc(r.orders_share_category_sum_partner,2)] for r in cat.itertuples()]))
lines += ["### Несведение категорий", ""]
recrows=[]
for side,z in out['categories']['totals_and_reconciliation'].items():
    for metric,v in z.items():recrows.append([side,metric,n(v['category_sum']),n(v['monthly_total']),n(v['difference']),pc(v['relative_difference'],4)])
lines.append(mdtable(["Канал","Метрика","Сумма категорий","Месячный итог","Разница","Относительная разница"],recrows))
lines += ["Причина неизвестна: возможны неполная классификация, многокатегорийные заказы, разные периметры или особенности синтетики. Категория «Не определена» у Партнёра: D19 = 0 открытий, E19 = 117 818 заказов. Коэффициент не определён; его нельзя заменять нулём. «Все товары» в строке 16 имеет собственные небольшие значения и не является очевидным итогом; определение надо уточнить.", "", "### Концентрация", ""]
conc=[]
for k,z in out['categories']['concentration'].items():
    conc.append([labels_ru[k],', '.join(x['category'] for x in z['ranking'][:3]),pc(z['top1_share_category_sum']),pc(z['top2_share_category_sum']),pc(z['top3_share_category_sum']),pc(z['top5_share_category_sum'])])
lines.append(mdtable(["Метрика","Три лидера","Топ-1","Топ-2","Топ-3","Топ-5"],conc))
lines += ["Выбор целевого сегмента требует одновременно учитывать спрос, долю продаж, коэффициенты на сопоставимых маршрутах, доступность продавцов и их экономику. Высокая текущая доля заказов показывает концентрацию наблюдаемых результатов; она сама по себе не означает максимальный доступный рост.", "", "### Чувствительность к составу категорий", ""]
lines.append(mdtable(["Набор","Категорий","Коэффициент VK","Коэффициент Партнёра","Отношение Партнёр/VK"],[[labels_ru[name],z['n_categories'],pc(z['vk']['orders_per_open_aggregate'],4),pc(z['partner']['orders_per_open_aggregate'],4),n(z['partner_vk_orders_per_open_ratio'],2)] for name,z in out['categories']['sensitivity'].items()]))
lines += ["В JSON дополнительно рассчитана стандартизация на структуру открытий VK, Партнёра, объединённую и равномерную структуру категорий. Она показывает, насколько сравнение меняется от весов. Даже одинаковые веса не исправляют обход карточки, несведение заказов, неодинаковые цены и атрибуцию; стандартизованные коэффициенты нельзя использовать как доказанный потенциальный эффект для VK.", "", "## 7. Что следует проверять в следующем исследовании", "", "1. Периметр платёжной метрики и связь оформления с оплатой одной когорты.", "2. Отдельные заказы Партнёра после карточки и после прямого перехода; окно и модель атрибуции.", "3. Границы отдельной витрины Маркета, товаров сообществ, общего каталога и контентных поверхностей.", "4. Причины несведения категорий, демографии и регионов; правила многокатегорийного заказа.", "5. Значение «Другое», «Все товары» и соответствие постов/ленты/клипов/реферального контента.", "6. Причины дневных изменений: кампании, предложения, трафик и ассортимент. В текущих данных этих факторов нет.", "7. Экономику: оплаченный и выкупленный оборот, отмены/возвраты, маржу продавцов, рекламную атрибуцию, комиссии и операционные затраты.", "", "## 8. Ограничения вывода", "", "Датасет позволяет описать масштаб, состав и изменения одного месяца, проверить взаимную согласованность цифр и сформулировать гипотезы. Он не позволяет доказать причинное влияние чекаута, контента или рекламы; оценить когортное удержание; определить реализованную выручку VK; экстраполировать сентябрь на год; измерить чистый эффект переноса покупок внутрь VK. Для прогноза нужны явно названные допущения и сценарии, не скрытая замена отсутствующих данных."]

(HERE / "agg_results.json").write_text(json.dumps(native(out),ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
(HERE / "agg_findings.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
print(json.dumps({"outputs":[str(HERE/"agg_results.json"),str(HERE/"agg_findings.md")],"summary":{"monthly":monthly,"first_last7":out['daily_comparisons']['first7_to_last7'],"categories_sensitivity":out['categories']['sensitivity']}},ensure_ascii=False,default=str))
