"""Three targeted extensions to the earlier VK case analysis.

Read-only source use. Run with the primary Python runtime, or in the notebook's
uv environment: python extra_audit.py --source PATH.xlsx --output-dir PATH.
Dependencies: pandas, numpy, openpyxl. No significance tests or causal estimates.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [clean(v) for v in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if math.isfinite(value) else None
    if value is None or pd.isna(value):
        return None
    return value


def divide(num, den):
    return float(num / den) if den else None


def md_table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |',
                      '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                     ['| ' + ' | '.join(str(x).replace('|', '/') for x in row) + ' |' for row in rows])


def main(source: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    d = pd.read_excel(source, sheet_name='Бизнесы')
    original_headers = dict(zip(list('ABCDEFGHIJKLMNOPQRSTUVWXYZ') + ['AA', 'AB'], d.columns))
    d.columns = list(original_headers)
    assert d.shape == (9754, 28), 'Expected the supplied case source, not a different sample.'
    assert d['A'].is_unique
    assert d['U'].dropna().isin([0, 1]).all()
    assert d[['E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'Q', 'W', 'X', 'AA']].ge(0).all().all()
    d['excel_row'] = np.arange(len(d)) + 2
    publisher = (d['G'] + d['H']).gt(0)
    seller = d['W'].gt(0)
    all_rows = pd.Series(True, index=d.index)
    scopes = {
        'all': all_rows,
        'goods': d['Q'].gt(0),
        'goods_no_referral': d['Q'].gt(0) & d['V'].eq(0),
        'goods_only_no_referral': d['Q'].gt(0) & d['AA'].eq(0) & d['V'].eq(0),
    }
    scope_labels = {
        'all': 'Все бизнесы', 'goods': 'Активные товары',
        'goods_no_referral': 'Активные товары, без рефералки',
        'goods_only_no_referral': 'Активные товары, без услуг и рефералки',
    }
    scope_rules = {
        'all': 'Все строки', 'goods': 'Q>0',
        'goods_no_referral': 'Q>0 and V=0',
        'goods_only_no_referral': 'Q>0 and AA=0 and V=0',
    }

    # These are definition-dependent diagnostics, not a data-error verdict.
    # No outcome W, GMV X, or reported average Y enters this diagnostic union.
    flags = {
        'publication_counter_subset': d['G'].gt(d['E']) | d['H'].gt(d['F']),
        'single_reaction_above_view': (d['J'].gt(d['I']) | d['K'].gt(d['I']) |
                                       d['M'].gt(d['L']) | d['N'].gt(d['L'])),
        'content_open_without_product_view': d['O'].gt(0) & d['L'].eq(0),
    }
    flag_rules = {
        'publication_counter_subset': 'G>E or H>F',
        'single_reaction_above_view': 'J>I or K>I or M>L or N>L',
        'content_open_without_product_view': 'O>0 and L=0',
    }
    flag_labels = {
        'publication_counter_subset': 'Товарный счётчик публикаций больше соответствующего E/F',
        'single_reaction_above_view': 'Один из счётчиков реакций больше соответствующих просмотров',
        'content_open_without_product_view': 'Открытия из контента при нулевых товарных просмотрах',
        'counter_union': 'Объединение трёх диагностик',
    }
    union = pd.concat(flags.values(), axis=1).any(axis=1)
    granular = {
        'product_posts_above_posts': d['G'].gt(d['E']),
        'product_clips_above_clips': d['H'].gt(d['F']),
        'ordinary_likes_above_views': d['J'].gt(d['I']),
        'ordinary_reposts_above_views': d['K'].gt(d['I']),
        'product_likes_above_views': d['M'].gt(d['L']),
        'product_reposts_above_views': d['N'].gt(d['L']),
        'content_open_without_product_view': flags['content_open_without_product_view'],
    }
    metrics = ['D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'W', 'X', 'Z', 'AA']

    def totals(mask):
        x = d[mask]
        return {'n': len(x), 'publishers': int(publisher[mask].sum()),
                'sellers': int(seller[mask].sum()),
                'active_catalogs': int(x['Q'].gt(0).sum()),
                'metric_totals': {c: int(x[c].sum()) for c in metrics}}

    volume_rows = []
    flag_results = {}
    for key, mask in {**flags, 'counter_union': union}.items():
        by_scope = {}
        for scope, population in scopes.items():
            selected = mask & population
            summary = totals(selected)
            base = totals(population)
            summary['denominator_n'] = base['n']
            summary['row_share'] = divide(summary['n'], base['n'])
            summary['metric_shares'] = {c: divide(summary['metric_totals'][c], base['metric_totals'][c]) for c in metrics}
            summary['rows_and_ids'] = d.loc[selected, ['excel_row', 'A']].rename(columns={'A': 'id'}).to_dict('records')
            by_scope[scope] = summary
            for c in metrics:
                volume_rows.append({'diagnostic': key, 'scope': scope, 'n': summary['n'],
                                    'denominator_n': base['n'], 'row_share': summary['row_share'],
                                    'metric': c, 'metric_name': original_headers[c],
                                    'metric_sum': summary['metric_totals'][c],
                                    'denominator_metric_sum': base['metric_totals'][c],
                                    'metric_share': summary['metric_shares'][c]})
        flag_results[key] = {'rule': flag_rules.get(key, 'Union of the three diagnostics'), 'by_scope': by_scope}

    overlap = [{'diagnostic_1': a, 'diagnostic_2': b, 'n_both': int((flags[a] & flags[b]).sum())}
               for a, b in itertools.combinations(flags, 2)]
    patterns = []
    for pattern in itertools.product([False, True], repeat=len(flags)):
        mask = all_rows.copy()
        for (key, flag), present in zip(flags.items(), pattern):
            mask &= flag if present else ~flag
        patterns.append({**dict(zip(flags, pattern)), **totals(mask)})
    granular_results = {key: totals(mask) for key, mask in granular.items()}
    members = d.loc[union, ['excel_row'] + list(original_headers)].copy()
    for key, flag in flags.items():
        members[key] = flag[union].to_numpy()
    members['diagnostic_count'] = members[list(flags)].sum(axis=1)
    members.to_csv(output / 'extra_diagnostic_rows.csv', index=False)
    pd.DataFrame(volume_rows).to_csv(output / 'extra_diagnostic_volume.csv', index=False)
    pd.DataFrame(overlap).to_csv(output / 'extra_diagnostic_overlap.csv', index=False)

    # Complement existing top-tail/checkout sensitivity with narrowly specified
    # counter-diagnostic exclusions. Selection does not use the outcome W/X/Y.
    variants = {'baseline': all_rows,
                **{f'exclude_{key}': ~mask for key, mask in flags.items()},
                'exclude_counter_union': ~union}
    sensitivity = []
    for scope, population in scopes.items():
        for variant, keep in variants.items():
            x = population & keep
            n1 = int((x & publisher).sum())
            s1 = int((x & publisher & seller).sum())
            n0 = int((x & ~publisher).sum())
            s0 = int((x & ~publisher & seller).sum())
            rate1, rate0 = divide(s1, n1), divide(s0, n0)
            sensitivity.append({'scope': scope, 'variant': variant,
                                'retained_n': int(x.sum()), 'excluded_n': int((population & ~keep).sum()),
                                'publisher_n': n1, 'publisher_sellers': s1, 'publisher_seller_rate': rate1,
                                'nonpublisher_n': n0, 'nonpublisher_sellers': s0, 'nonpublisher_seller_rate': rate0,
                                'seller_rate_difference_pp': (rate1-rate0)*100,
                                'seller_rate_ratio': divide(rate1, rate0),
                                'orders': int(d.loc[x, 'W'].sum()), 'gmv': int(d.loc[x, 'X'].sum())})
    pd.DataFrame(sensitivity).to_csv(output / 'extra_content_sensitivity.csv', index=False)
    strict = d['Q'].gt(0) & ~seller & ~publisher & (d['E'] + d['F']).gt(0) & d['AA'].eq(0) & d['V'].eq(0)
    pilot = strict & d['D'].ge(1000) & d['I'].ge(1000) & d['Q'].ge(10) & d['R'].eq(1)
    candidate_sensitivity = []
    for group, mask in [('strict_1817', strict), ('pilot_366', pilot)]:
        for variant, keep in variants.items():
            candidate_sensitivity.append({'group': group, 'variant': variant,
                                          'baseline_n': int(mask.sum()), 'retained_n': int((mask & keep).sum()),
                                          'excluded_n': int((mask & ~keep).sum()),
                                          'excluded_ids': d.loc[mask & ~keep, 'A'].astype(int).tolist()})

    # A contrast illustrating why broad automatic 'cleaning' is invalid here.
    # AOV identity is only observed for positive orders, so removing failures
    # preferentially deletes the outcome, not a neutral slice of businesses.
    aov_bad = seller & d['Y'].notna() & (d['Y'] - d['X'].div(d['W'].replace(0, np.nan))).abs().gt(1)
    outcome_dependent_exclusion = []
    for scope, population in scopes.items():
        excluded = totals(population & aov_bad)
        baseline = totals(population)
        retained = totals(population & ~aov_bad)
        outcome_dependent_exclusion.append({'scope': scope, 'rule': 'W>0 and Y present and abs(Y-X/W)>1 RUB',
                                           'baseline': baseline, 'excluded': excluded, 'retained': retained,
                                           'excluded_share_of_sellers': divide(excluded['sellers'], baseline['sellers']),
                                           'excluded_share_of_orders': divide(excluded['metric_totals']['W'], baseline['metric_totals']['W']),
                                           'excluded_share_of_gmv': divide(excluded['metric_totals']['X'], baseline['metric_totals']['X']),
                                           'warning': 'Outcome-dependent diagnostic: not a valid cleaned-sample business estimate.'})

    # Sharp finite-sample bounds for completion of the missing binary U labels.
    # Keep observed labels/outcomes fixed. Unknown seller U=1 / nonseller U=0
    # maximises both the difference and ratio; reverse assignment minimises them.
    # These are feasible descriptive bounds, not confidence intervals or effects.
    bounds = []
    bounds_long = []
    for scope, population in scopes.items():
        x = d[population]
        ns = {
            'known_0_n': int(x['U'].eq(0).sum()),
            'known_0_sellers': int((x['U'].eq(0) & x['W'].gt(0)).sum()),
            'known_1_n': int(x['U'].eq(1).sum()),
            'known_1_sellers': int((x['U'].eq(1) & x['W'].gt(0)).sum()),
            'unknown_n': int(x['U'].isna().sum()),
            'unknown_sellers': int((x['U'].isna() & x['W'].gt(0)).sum()),
        }
        ns['unknown_nonsellers'] = ns['unknown_n'] - ns['unknown_sellers']
        n0, s0, n1, s1 = ns['known_0_n'], ns['known_0_sellers'], ns['known_1_n'], ns['known_1_sellers']
        us, un = ns['unknown_sellers'], ns['unknown_nonsellers']

        def rates(positive_n, positive_s, negative_n, negative_s):
            r1, r0 = positive_s/positive_n, negative_s/negative_n
            return {'rating_1_n': positive_n, 'rating_1_sellers': positive_s,
                    'rating_0_n': negative_n, 'rating_0_sellers': negative_s,
                    'rating_1_seller_rate': r1, 'rating_0_seller_rate': r0,
                    'difference_pp': 100*(r1-r0), 'rate_ratio': divide(r1, r0)}

        observed = rates(n1, s1, n0, s0)
        lower = rates(n1+un, s1, n0+us, s0+us)
        upper = rates(n1+us, s1+us, n0+un, s0)
        assert lower['rating_1_n']+lower['rating_0_n'] == len(x)
        assert upper['rating_1_n']+upper['rating_0_n'] == len(x)
        assert lower['rating_1_sellers']+lower['rating_0_sellers'] == int(x['W'].gt(0).sum())
        assert lower['difference_pp'] <= observed['difference_pp'] <= upper['difference_pp']
        result = {'scope': scope, 'n': len(x), **ns,
                  'unknown_share': ns['unknown_n']/len(x),
                  'unknown_seller_rate': divide(us, us+un),
                  'observed_known_labels': observed,
                  'minimum_compatible_completion': lower,
                  'maximum_compatible_completion': upper,
                  'positive_sign_identified': lower['difference_pp'] > 0,
                  'completed_rating_1_population_share_bounds': [n1/len(x), (n1+us+un)/len(x)]}
        bounds.append(result)
        for state, value in [('known_only', observed), ('minimum_completion', lower), ('maximum_completion', upper)]:
            bounds_long.append({'scope': scope, 'state': state, 'total_scope_n': len(x), **ns, **value})
    pd.DataFrame(bounds_long).to_csv(output / 'extra_author_bounds.csv', index=False)

    # Verify the analytic bound construction with exhaustive allocations on all
    # four real scopes (<= 100k feasible allocations each). This is deterministic
    # verification of the optimisation, not a significance test.
    for b in bounds:
        n0, s0 = b['known_0_n'], b['known_0_sellers']
        n1, s1 = b['known_1_n'], b['known_1_sellers']
        us, un = b['unknown_sellers'], b['unknown_nonsellers']
        a, z = np.meshgrid(np.arange(us+1), np.arange(un+1), indexing='ij')
        r1 = (s1+a)/(n1+a+z)
        r0 = (s0+us-a)/(n0+us-a+un-z)
        differences = (r1-r0)*100
        ratios = r1/r0
        assert math.isclose(float(differences.min()), b['minimum_compatible_completion']['difference_pp'], abs_tol=1e-12)
        assert math.isclose(float(differences.max()), b['maximum_compatible_completion']['difference_pp'], abs_tol=1e-12)
        assert math.isclose(float(ratios.min()), b['minimum_compatible_completion']['rate_ratio'], abs_tol=1e-12)
        assert math.isclose(float(ratios.max()), b['maximum_compatible_completion']['rate_ratio'], abs_tol=1e-12)
        b['bound_verification'] = {'all_feasible_integer_allocations_checked': int(differences.size), 'passed': True}

    assert int(union.sum()) == 67
    assert int(strict.sum()) == 1817 and int(pilot.sum()) == 366
    assert int(seller.sum()) == 298 and int(d['W'].sum()) == 3478 and int(d['X'].sum()) == 9973027
    result = {
        'metadata': {'source_filename': source.name, 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                     'source_sheet': 'Бизнесы', 'source_range': 'A1:AB9755', 'rows': len(d),
                     'columns': original_headers, 'scope_rules': scope_rules,
                     'prior_analysis_extended': ['audit_analysis.py', 'core_analysis.py', 'extended_analysis.py'],
                     'new_questions': ['Diagnostic overlap and its share of different event volumes',
                                       'Sensitivity to counter-diagnostic rows and danger of outcome-conditioned deletion',
                                       'Sharp descriptive bounds under unknown original-author ratings']},
        'diagnostics': {'definitions': flag_rules, 'summary': flag_results, 'granular': granular_results,
                        'pairwise_overlap': overlap, 'joint_patterns': patterns},
        'content_sensitivity': sensitivity,
        'candidate_sensitivity': candidate_sensitivity,
        'outcome_dependent_exclusion_warning': outcome_dependent_exclusion,
        'author_partial_identification': bounds,
        'interpretation_limits': [
            'Данные синтетические. Здесь нет доказательства статистической закономерности реального VK.',
            'Диагностики счётчиков зависят от определений и окон наблюдения. Исходник не очищается и строки не объявляются ошибочными.',
            'Доля объёма у диагностических строк не равна доле неверных событий; флаг одного поля не дискредитирует все остальные поля строки.',
            'Чувствительность показывает условное изменение описательной связи после исключений, не исправленный истинный эффект.',
            'Диагностики без W/X/Y всё равно связаны с публикациями и объёмом активности. Их исключение меняет состав сравниваемых групп и не гарантирует случайный или нейтральный отбор.',
            'Результат W относится к чекауту. O относится только к открытиям из контента; отношения W/O не рассчитываются как сквозная конверсия.',
            'Границы U относятся к возможному заполнению отсутствующей бинарной оценки модели при сохранении известных меток. Для реальной оригинальности они не являются оценкой качества классификатора.',
            'Границы достигаются при предельных распределениях неизвестных меток по наличию заказа. Это множество совместимых значений, а не доверительный интервал или наиболее вероятное заполнение.',
            'Подключение инструментов, публикации и заказы наблюдаются одновременно. Причинность и будущий эффект не определены.',
        ],
    }
    (output/'extra_audit_results.json').write_text(json.dumps(clean(result), ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'extra_candidate_sensitivity.json').write_text(json.dumps(clean(candidate_sensitivity), ensure_ascii=False, indent=2), encoding='utf-8')

    u = flag_results['counter_union']['by_scope']['all']
    doc = ['# Дополнительный целевой аудит', '',
           'Источник: исходный Excel, лист «Бизнесы», A1:AB9755. Все расчёты воспроизводятся `extra_audit.py`. Исходник не изменяется.', '',
           '## 1. Несогласования счётчиков сосредоточены неравномерно', '',
           'Ранее были найдены отдельные диагностики. Здесь впервые проверены их пересечения и доли объёмов.', '',
           md_table(['Диагностика', 'Строк', 'Доля строк', 'Доля G', 'Доля K', 'Доля L', 'Доля O', 'Доля W', 'Доля X'],
                    [[flag_labels[k], v['by_scope']['all']['n'], f"{100*v['by_scope']['all']['row_share']:.3f}%",
                      *[f"{100*v['by_scope']['all']['metric_shares'][c]:.2f}%" for c in ['G','K','L','O','W','X']]]
                     for k,v in flag_results.items()]), '',
           f"Три набора дают {u['n']} уникальных строк. Между первыми двумя есть один общий ID 7062; набор O>0 при L=0 с ними не пересекается.", '',
           'Эти доли показывают концентрацию диагностики. Они не означают, что указанная доля реакций, просмотров или GMV неверна. Требуется уточнить окна и определения счётчиков.', '',
           '## 2. Устойчивость содержательной связи и правил отбора', '',
           md_table(['Выборка','Вариант','Публикуют: с заказом/N','Не публикуют: с заказом/N','Разница, п.п.','Отношение долей'],
                    [[scope_labels[v['scope']], v['variant'], f"{v['publisher_sellers']}/{v['publisher_n']}",
                      f"{v['nonpublisher_sellers']}/{v['nonpublisher_n']}", f"{v['seller_rate_difference_pp']:.2f}", f"{v['seller_rate_ratio']:.3f}"]
                     for v in sensitivity if v['variant'] in ['baseline','exclude_counter_union']]), '',
           md_table(['Группа','Исходно','После исключения объединения','Исключено'],
                    [[v['group'], v['baseline_n'], v['retained_n'], v['excluded_n']] for v in candidate_sensitivity if v['variant']=='exclude_counter_union']), '',
           'Знак наблюдаемой связи с наличием заказа сохраняется. Это проверка чувствительности на данном срезе, не установление причинного эффекта публикаций. Диагностики зависят от публикаций и активности: даже без прямого использования W/X/Y их исключение не гарантирует нейтральный отбор.', '',
           '### Почему нельзя удалить все «аномальные» строки одной командой', '',
           f"Удаление 287 строк с |Y−X/W|>1 ₽ убирает {100*outcome_dependent_exclusion[0]['excluded_share_of_sellers']:.2f}% бизнесов с заказами и оставляет только {outcome_dependent_exclusion[0]['retained']['sellers']} из 298. Проверка среднего чека существует преимущественно у положительных исходов. Такое удаление меняет состав результата по определению; оно не образует нейтральную очищенную выборку.", '',
           '## 3. Что можно утверждать об оригинальных авторах при пропусках U', '',
           md_table(['Выборка','Известно U=1: с заказом/N','Известно U=0: с заказом/N','U неизвестно: с заказом/N','Разница известных, п.п.','Нижняя граница, п.п.','Верхняя граница, п.п.'],
                    [[scope_labels[b['scope']], f"{b['known_1_sellers']}/{b['known_1_n']}", f"{b['known_0_sellers']}/{b['known_0_n']}",
                      f"{b['unknown_sellers']}/{b['unknown_n']}", f"{b['observed_known_labels']['difference_pp']:.2f}",
                      f"{b['minimum_compatible_completion']['difference_pp']:.2f}", f"{b['maximum_compatible_completion']['difference_pp']:.2f}"] for b in bounds]), '',
           'Нижняя граница: все неизвестные сообщества с заказом получают U=0, без заказа — U=1. Верхняя граница: обратное распределение. Известные метки и факт заказа сохраняются. Проверены все допустимые целочисленные распределения неизвестных меток; аналитические границы точны.', '',
           'Во всех четырёх выборках диапазон включает отрицательные и положительные значения. Поэтому знак связи во всей соответствующей группе не определяется известными метками без предположения о механизме пропусков. У известных меток положительная разница наблюдается; расширять её на неизвестные оценки без оговорки нельзя.', '',
           'Это границы возможного заполнения модельного флага, а не доверительный интервал, прогноз или причинный эффект. Они не измеряют реальную оригинальность автора и не предполагают безошибочность классификатора за пределами зафиксированных меток.', '',
           '## Формулы границ', '',
           'Пусть n₁,s₁ и n₀,s₀ — размеры и числа сообществ с заказами среди известных U=1 и U=0; u₁ — неизвестные U с заказом, u₀ — неизвестные U без заказа.', '',
           '- Нижняя разница: s₁/(n₁+u₀) − (s₀+u₁)/(n₀+u₁).',
           '- Верхняя разница: (s₁+u₁)/(n₁+u₁) − s₀/(n₀+u₀).', '',
           'Разница выше переведена в процентные пункты умножением на 100. Для отношения долей используются те же две крайние конфигурации.', '',
           '## Файлы', '',
           '- `extra_audit_results.json`: все определения, значения, знаменатели, перечни строк и ограничения.',
           '- `extra_diagnostic_rows.csv`: 67 исходных строк с полями и флагами.',
           '- `extra_diagnostic_volume.csv`: доли каждого из 18 объёмных показателей для диагностик и выборок.',
           '- `extra_diagnostic_overlap.csv`: пересечения диагностик.',
           '- `extra_content_sensitivity.csv`: все варианты чувствительности связи публикаций и наличия заказа.',
           '- `extra_candidate_sensitivity.json`: сохранность строгой группы и условного пилота.',
           '- `extra_author_bounds.csv`: известные группы и обе предельные конфигурации U.', '']
    (output/'extra_audit_findings.md').write_text('\n'.join(doc), encoding='utf-8')
    print(json.dumps(clean({'counter_union': u, 'author_bounds': bounds,
                            'candidate_union': [v for v in candidate_sensitivity if v['variant']=='exclude_counter_union']}), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    main(args.source, args.output_dir)
