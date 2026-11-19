"""Additional descriptive checks for the synthetic VK CaseChamp dataset.

Reproduces three additions to the previous core/extended analysis:
1. Common-population direct standardization over subscribers x active catalogue.
2. Candidate-pool stability, eligibility boundaries, and concentration diagnostics.
3. Scenario-only exact Fisher-test power for a hypothetical randomized pilot.

No causal effects, future baselines or expected commercial outcomes are estimated.
Only pandas/numpy/scipy/openpyxl are required; the source workbook is read only.

Usage:
    python robustness_analysis.py --source dataset.xlsx --output-dir results
Notebook:
    from robustness_analysis import run_analysis
    results = run_analysis(source_path, output_dir)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.optimize import brentq
from scipy.stats import binom, hypergeom, fisher_exact


NAMES = [
    'id', 'theme', 'business_type', 'subscribers', 'posts', 'clips',
    'product_posts', 'product_clips', 'views', 'likes', 'reposts',
    'product_views', 'product_likes', 'product_reposts', 'content_opens',
    'uploaded_products', 'active_products', 'checkout', 'delivery', 'payment',
    'original_author', 'referral', 'orders', 'gmv', 'reported_aov',
    'ad_spend', 'active_services', 'median_price',
]
FEATURES = ['has_product_pubs', 'delivery', 'payment', 'ad_active', 'has_opens']


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, np.ndarray)):
        return [clean(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return float(o) if math.isfinite(o) else None
    if isinstance(o, np.bool_):
        return bool(o)
    if pd.isna(o):
        return None
    return o


def load_businesses(source: Path) -> pd.DataFrame:
    d = pd.read_excel(source, sheet_name='Бизнесы')
    if d.shape[1] != 28:
        raise ValueError('Expected the original 28-field business sheet.')
    d.columns = NAMES
    d['excel_row'] = np.arange(2, len(d) + 2)
    d['has_goods'] = d.active_products.gt(0)
    d['has_services'] = d.active_services.gt(0)
    d['seller'] = d.orders.gt(0)
    d['pubs'] = d.posts + d.clips
    d['product_pubs'] = d.product_posts + d.product_clips
    d['has_product_pubs'] = d.product_pubs.gt(0)
    d['has_pubs'] = d.pubs.gt(0)
    d['ad_active'] = d.ad_spend.gt(0)
    d['has_opens'] = d.content_opens.gt(0)
    d['no_referral'] = d.referral.eq(0)
    return d


def weighted_rates(strata: pd.DataFrame):
    """Same stratum weights for feature=0 and feature=1, never impute empty arms."""
    if strata.empty:
        return dict(n=0, rate0=None, rate1=None, ratio=None, gap_pp=None)
    w = (strata.n0 + strata.n1).to_numpy(dtype=float)
    w /= w.sum()
    r0 = float(np.dot(w, strata.rate0))
    r1 = float(np.dot(w, strata.rate1))
    return dict(n=int((strata.n0 + strata.n1).sum()), rate0=r0, rate1=r1,
                ratio=r1 / r0 if r0 else None, gap_pp=100 * (r1 - r0))


def joint_standardization(d: pd.DataFrame):
    """3x3 and 5x5 quantile groups; 5x5 is the primary descriptive check.

    Quantile edges are set once from each full scope; they are not re-cut after
    restricting to checkout=1. Common support requires both feature arms.
    The min-arm=5 version is a sensitivity check on a different covered subset.
    No confidence intervals/p-values are attached to this synthetic sample.
    """
    summaries, all_strata, edge_rows = [], [], []
    for scope, mask in [('goods', d.has_goods),
                        ('goods_no_referral', d.has_goods & d.no_referral)]:
        full = d[mask].copy()
        for q in [3, 5]:
            for column in ['subscribers', 'active_products']:
                full[column + '_stratum'], edges = pd.qcut(
                    full[column], q, labels=False, retbins=True, duplicates='drop')
                edge_rows.append(dict(scope=scope, quantiles=q, variable=column,
                                      edges=edges.tolist()))
            for variant, x in [('all', full), ('checkout_on', full[full.checkout.eq(1)])]:
                for feature in FEATURES:
                    rows = []
                    for (sb, cb), g in x.groupby(['subscribers_stratum', 'active_products_stratum']):
                        g0, g1 = g[g[feature].eq(0)], g[g[feature].eq(1)]
                        n0, n1, y0, y1 = len(g0), len(g1), int(g0.seller.sum()), int(g1.seller.sum())
                        rows.append(dict(scope=scope, quantiles=q, variant=variant, feature=feature,
                                         subscriber_bin=int(sb), catalogue_bin=int(cb),
                                         n0=n0, n1=n1, sellers0=y0, sellers1=y1,
                                         rate0=y0/n0 if n0 else None,
                                         rate1=y1/n1 if n1 else None))
                    table = pd.DataFrame(rows)
                    all_strata.extend(rows)
                    raw0, raw1 = x.loc[x[feature].eq(0), 'seller'].mean(), x.loc[x[feature].eq(1), 'seller'].mean()
                    for minimum in [1, 5]:
                        valid = table[(table.n0 >= minimum) & (table.n1 >= minimum)].copy()
                        z = weighted_rates(valid)
                        if len(valid):
                            keys = set(zip(valid.subscriber_bin, valid.catalogue_bin))
                            covered = x[[ 'subscribers_stratum', 'active_products_stratum' ]].apply(tuple, axis=1).isin(keys)
                            restricted = x[covered]
                            cr0 = restricted.loc[restricted[feature].eq(0), 'seller'].mean()
                            cr1 = restricted.loc[restricted[feature].eq(1), 'seller'].mean()
                            # Stratum influence: omit each cell and renormalize the
                            # SAME weights for both arms. A diagnostic, not an interval.
                            leave_ratios = [weighted_rates(valid.drop(i))['ratio'] for i in valid.index]
                            leave_ratios = [v for v in leave_ratios if v is not None]
                        else:
                            cr0 = cr1 = np.nan
                            leave_ratios = []
                        summaries.append(dict(
                            scope=scope, quantiles=q, variant=variant, feature=feature,
                            minimum_arm_n=minimum, eligible_scope_n=len(x),
                            raw_rate0=raw0, raw_rate1=raw1,
                            raw_ratio=raw1/raw0 if raw0 else None,
                            common_raw_rate0=cr0, common_raw_rate1=cr1,
                            common_raw_ratio=cr1/cr0 if cr0 else None,
                            covered_n=z['n'], coverage_share=z['n']/len(x),
                            covered_strata=len(valid), observed_strata=len(table),
                            excluded_strata=len(table)-len(valid),
                            standardized_rate0=z['rate0'], standardized_rate1=z['rate1'],
                            standardized_ratio=z['ratio'], standardized_gap_pp=z['gap_pp'],
                            min_arm0=int(valid.n0.min()) if len(valid) else None,
                            min_arm1=int(valid.n1.min()) if len(valid) else None,
                            zero_event_arm0_strata=int(valid.sellers0.eq(0).sum()),
                            zero_event_arm1_strata=int(valid.sellers1.eq(0).sum()),
                            leave_one_stratum_ratio_min=min(leave_ratios) if leave_ratios else None,
                            leave_one_stratum_ratio_max=max(leave_ratios) if leave_ratios else None,
                        ))
    return summaries, all_strata, edge_rows


def theme_tv(x: pd.DataFrame, reference: pd.DataFrame) -> float:
    a, b = x.theme.value_counts(normalize=True), reference.theme.value_counts(normalize=True)
    keys = a.index.union(b.index)
    return float((a.reindex(keys, fill_value=0)-b.reindex(keys, fill_value=0)).abs().sum()/2)


def profile(x: pd.DataFrame) -> dict:
    n = len(x)
    ads = x.ad_spend.sort_values(ascending=False)
    total = float(ads.sum())
    positive = ads[ads > 0]
    shares = ads / total if total else np.zeros(n)
    return dict(
        n=n, ids=[int(v) for v in x.id], source_excel_rows=[int(v) for v in x.excel_row],
        theme_counts=x.theme.value_counts().to_dict(), themes=int(x.theme.nunique()),
        checkout_n=int(x.checkout.eq(1).sum()), delivery_n=int(x.delivery.eq(1).sum()),
        payment_n=int(x.payment.eq(1).sum()), ad_users=int(x.ad_active.sum()),
        zero_content_opens_n=int(x.content_opens.eq(0).sum()),
        positive_product_views_n=int(x.product_views.gt(0).sum()),
        content_opens=int(x.content_opens.sum()), product_views=int(x.product_views.sum()),
        subscribers_median=x.subscribers.median(), views_median=x.views.median(),
        active_products_median=x.active_products.median(), median_price_median=x.median_price.median(),
        ad_spend=total, ad_median_all=ads.median(),
        ad_median_positive=positive.median(), ad_mean_positive=positive.mean(),
        ad_positive_q25=positive.quantile(.25), ad_positive_q75=positive.quantile(.75),
        ad_top1_share=float(ads.iloc[:1].sum()/total) if total else None,
        ad_top5_share=float(ads.iloc[:5].sum()/total) if total else None,
        ad_top10_share=float(ads.iloc[:10].sum()/total) if total else None,
        ad_effective_equal_contributors=1/float(np.square(shares).sum()) if total else None,
        ad_total_without_top1=float(ads.iloc[1:].sum()),
        ad_total_without_top5=float(ads.iloc[5:].sum()),
    )


def candidate_stability(d: pd.DataFrame):
    strict = d.has_goods & ~d.seller & ~d.has_product_pubs & d.has_pubs & ~d.has_services & d.no_referral
    s = d[strict].copy()
    at1000 = s[s.subscribers.ge(1000) & s.views.ge(1000)]
    at10 = at1000[at1000.active_products.ge(10)]
    base = at10[at10.checkout.eq(1)]
    base_ids = set(base.id)
    groups = {
        'strict_1817': s, 'both_1000_684': at1000,
        'both_1000_catalogue_10_430': at10, 'pilot_366': base,
        'failed_catalogue_10_gate': at1000[at1000.active_products.lt(10)],
        'failed_checkout_gate': at10[at10.checkout.ne(1)],
    }
    matrix = []
    for subs in [0, 500, 800, 1000, 1200, 2000, 5000, 10000]:
        for views in [0, 500, 800, 1000, 1200, 2000, 5000, 10000]:
            g = s[s.subscribers.ge(subs) & s.views.ge(views) & s.active_products.ge(10) & s.checkout.eq(1)]
            ids = set(g.id)
            matrix.append(dict(subscribers_min=subs, views_min=views, catalogue_min=10, checkout_required=1,
                               n=len(g), retained_from_366=len(ids & base_ids),
                               added_to_366=len(ids-base_ids), removed_from_366=len(base_ids-ids),
                               jaccard_with_366=len(ids & base_ids)/len(ids | base_ids),
                               theme_total_variation_vs_366=theme_tv(g, base),
                               themes=int(g.theme.nunique()), ad_users=int(g.ad_active.sum()),
                               ad_spend=float(g.ad_spend.sum())))
    catalogue_grid = []
    for q in [1, 5, 10, 20, 50, 100]:
        g = at1000[at1000.active_products.ge(q) & at1000.checkout.eq(1)]
        catalogue_grid.append(dict(catalogue_min=q, n=len(g),
                                   theme_total_variation_vs_366=theme_tv(g, base),
                                   ad_users=int(g.ad_active.sum()), ad_spend=float(g.ad_spend.sum())))
    # The comparison is inclusive vs strict thresholds, not removal of errors.
    strict_inequalities = at1000[at1000.subscribers.gt(1000) & at1000.views.gt(1000)
                                & at1000.active_products.gt(10) & at1000.checkout.eq(1)]
    groups['strict_greater_than_instead_ge'] = strict_inequalities
    boundary = dict(
        exactly_1000_subscribers_n=int(base.subscribers.eq(1000).sum()),
        exactly_1000_views_n=int(base.views.eq(1000).sum()),
        exactly_10_products_n=int(base.active_products.eq(10).sum()),
        any_boundary_n=int((base.subscribers.eq(1000) | base.views.eq(1000) | base.active_products.eq(10)).sum()),
        strict_inequality_n=len(strict_inequalities),
    )
    outliers = []
    for metric in ['ad_spend', 'subscribers', 'views']:
        order = base.sort_values([metric, 'id'], ascending=[False, True])
        for k in [1, 5, 10]:
            keep = order.iloc[k:]
            outliers.append(dict(metric=metric, removed_top_k=k, remaining_n=len(keep),
                                 excluded_ids=[int(v) for v in order.id.iloc[:k]],
                                 remaining_ad_spend=float(keep.ad_spend.sum()),
                                 remaining_ad_share=float(keep.ad_spend.sum()/base.ad_spend.sum()),
                                 theme_total_variation_vs_366=theme_tv(keep, base),
                                 remaining_subscribers_median=keep.subscribers.median(),
                                 remaining_views_median=keep.views.median()))
    records = base[['excel_row', *NAMES]].to_dict('records')
    return dict(profiles={k: profile(g) for k,g in groups.items()},
                threshold_matrix=matrix, catalogue_thresholds=catalogue_grid,
                boundary_check=boundary, outlier_diagnostics=outliers,
                pilot_candidate_rows=records)


def fisher_rejection_matrix(n: int, alpha: float = .05):
    """Equal-arm, two-sided Fisher exact rejection region.

    For equal group sizes, the conditional hypergeometric distribution is
    symmetric, so twice the smaller inclusive tail equals the usual two-sided
    Fisher p-value (capped at 1). Degenerate all-zero/all-one tables get p=1.
    """
    k0 = np.arange(n+1)[:, None]
    k1 = np.arange(n+1)[None, :]
    total = k0+k1
    p = np.minimum(1., 2*np.minimum(hypergeom.cdf(k1, 2*n, total, n),
                                    hypergeom.sf(k1-1, 2*n, total, n)))
    p[(total == 0) | (total == 2*n)] = 1.
    return (p <= alpha + 1e-12).astype(float)


def exact_power(reject: np.ndarray, p0: float, p1: float):
    n = len(reject)-1
    ks = np.arange(n+1)
    return float(binom.pmf(ks, n, p0) @ reject @ binom.pmf(ks, n, p1))


def pilot_power():
    scenarios, mde_rows = [], []
    cache = {}
    # Pools are counts, not estimates of willingness to enroll or future baseline.
    for pool_n in [366, 430, 684]:
        for participation in [.5, .8, 1.]:
            n = int(math.floor(pool_n*participation/2))
            if n not in cache:
                cache[n] = fisher_rejection_matrix(n)
            reject = cache[n]
            for p0 in [.01, .03, .05, .10]:
                for delta in [.03, .05, .10]:
                    scenarios.append(dict(
                        eligible_pool_n=pool_n, participation_assumption=participation,
                        analyzable_total=2*n, n_each_arm=n,
                        future_control_rate_assumption=p0, future_treatment_rate_assumption=p0+delta,
                        absolute_change_pp=100*delta, alpha=.05,
                        exact_fisher_power=exact_power(reject, p0, p0+delta),
                    ))
                p1 = brentq(lambda v: exact_power(reject, p0, v)-.8,
                            p0+1e-8, min(.999999, p0+.8), xtol=1e-9)
                mde_rows.append(dict(
                    eligible_pool_n=pool_n, participation_assumption=participation,
                    analyzable_total=2*n, n_each_arm=n,
                    future_control_rate_assumption=p0, alpha=.05,
                    target_power=.8, detectable_treatment_rate=p1,
                    detectable_absolute_change_pp=100*(p1-p0),
                    relative_change_over_control=(p1-p0)/p0,
                    null_rejection_probability=exact_power(reject, p0, p0),
                ))
    # A small deterministic independent check of the rejection matrix against
    # scipy's generic two-sided Fisher implementation, including sparse tables.
    checks = []
    for n in [91, 183, 342]:
        matrix = cache[n]
        for a,b in [(0,0),(0,1),(0,10),(5,15),(10,20),(20,20),(n,n)]:
            generic = float(fisher_exact([[a,n-a],[b,n-b]], alternative='two-sided').pvalue)
            agrees = bool(matrix[a,b] == float(generic <= .05+1e-12))
            checks.append(dict(n=n, successes0=a, successes1=b, scipy_pvalue=generic,
                               rejection_agrees=agrees))
    if not all(c['rejection_agrees'] for c in checks):
        raise AssertionError('Power rejection matrix differs from scipy Fisher exact.')
    return dict(assumptions=[
        'Scenario calculations, not fitted estimates from the synthetic dataset.',
        'A future period of equal length and one pre-specified binary endpoint per business.',
        'Independent businesses, common outcome probability within each arm, random 1:1 allocation, complete outcome ascertainment.',
        'No spillovers, no clustering, no sequential stopping and no multiplicity adjustment.',
        'Two-sided Fisher exact test at alpha=0.05; MDE targets 80% power.',
        'Participation fractions are scenario assumptions; remaining businesses split equally.',
        'Prior observed W=0 is an eligibility rule, not a future control probability of zero.',
        'The actual future control rate, enrollment, duration and meaningful effect are unknown.',
    ], scenarios=scenarios, mde=mde_rows, implementation_checks=checks)


def run_analysis(source: str | Path, output_dir: str | Path):
    source, output_dir = Path(source), Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    d = load_businesses(source)
    summaries, strata, edges = joint_standardization(d)
    candidate = candidate_stability(d)
    power = pilot_power()
    result = dict(
        metadata=dict(source_name=source.name, sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                      source_sheet='Бизнесы', source_range=f'A1:AB{len(d)+1}',
                      rows=len(d), analysis_date='2026-11-19',
                      additions_to_prior=['joint_subscriber_catalogue_standardization',
                                          'candidate_composition_boundary_and_outlier_stability',
                                          'explicit_scenario_randomized_pilot_power']),
        limitations=[
            'All observed facts describe the supplied synthetic month, not the VK population.',
            'Cross-sectional feature/order associations do not identify causal effects.',
            'Joint strata control only two coarse observed dimensions, not all confounding.',
            'Common-support and min-cell restrictions change the target population.',
            'Leave-one-cell ratios are sensitivity ranges, not confidence intervals.',
            'Scenario pilot power depends on unobserved future rates and participation.',
        ],
        joint_standardization=dict(summary=summaries, strata=strata, quantile_edges=edges),
        candidates=candidate, pilot_power=power,
    )
    result = clean(result)
    (output_dir/'robustness_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    exports = {
        'joint_standardization_summary.csv': summaries,
        'joint_standardization_strata.csv': strata,
        'pilot_threshold_matrix.csv': candidate['threshold_matrix'],
        'pilot_catalogue_thresholds.csv': candidate['catalogue_thresholds'],
        'pilot_outlier_diagnostics.csv': candidate['outlier_diagnostics'],
        'pilot_366_candidates.csv': candidate['pilot_candidate_rows'],
        'pilot_power_scenarios.csv': power['scenarios'],
        'pilot_power_mde.csv': power['mde'],
    }
    for name, rows in exports.items():
        pd.DataFrame(rows).to_csv(output_dir/name, index=False)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    result = run_analysis(args.source, args.output_dir)
    headline = dict(
        source=result['metadata'],
        joint_5x5_main=[v for v in result['joint_standardization']['summary']
                        if v['quantiles']==5 and v['variant']=='all' and v['minimum_arm_n']==1],
        pilot_boundary=result['candidates']['boundary_check'],
        candidate_count=result['candidates']['profiles']['pilot_366']['n'],
        power_366_full_3pct=[v for v in result['pilot_power']['scenarios']
                            if v['eligible_pool_n']==366 and v['participation_assumption']==1
                            and v['future_control_rate_assumption']==.03],
    )
    print(json.dumps(headline, ensure_ascii=False, indent=2))
