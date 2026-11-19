"""Сборка структуры полного блокнота. Выполнение — execute_notebook.py."""
from pathlib import Path
import textwrap
import nbformat as nbf

ROOT=Path(__file__).resolve().parents[1]
cells=[]
def md(text):
    cells.append(nbf.v4.new_markdown_cell(textwrap.dedent(text).strip()))
def code(text):
    cells.append(nbf.v4.new_code_cell(textwrap.dedent(text).strip()))
def section(title,text):
    md('## '+title+'\n\n'+text)

md("""
# Полный анализ данных VK CaseChamp 2026

**Исходные данные:** семь листов Excel, сентябрь 2026 года. **Версия анализа:** 19 ноября 2026 года.

Этот блокнот воспроизводит весь предыдущий анализ и добавляет проверки устойчивости,
совместную стандартизацию, границы выводов при пропусках и сценарную оценку мощности пилота.
В сохранённом файле уже есть результаты и графики. Команда «Restart Kernel and Run All Cells»
пересчитывает их из исходного Excel; готовые JSON не используются как кэш.

Данные **синтетические** и по условию кейса не отражают реальные показатели VK.
Один месяц и несвязанные агрегаты не позволяют установить причинность, окупаемость,
удержание следующего месяца или будущую вероятность продажи.

Подробные вычисления вынесены в читаемые Python-модули папки analysis.
Каждый модуль запускается из соответствующего раздела. Внутри блокнота есть все аналитические
разрезы; полные таблицы также экспортируются в CSV, сложные результаты — в JSON.
Раскрываемые подробные разборы в конце сохраняют весь текст предыдущих исследований.

### Запуск через uv

Из корня распакованного проекта:

    uv sync --locked
    uv run --locked jupyter lab VK_full_analysis.ipynb

Полное выполнение в новом процессе IPython без сервера:

    uv run --locked python scripts/execute_notebook.py

Python 3.12.14 задан в .python-version, версии всех зависимостей и хеши пакетов — в uv.lock.
После установки анализ работает без сети. Исходные XLSX и PDF не изменяются.
""")

section('1. Окружение и неизменность исходников',
"""Проверяем версии, контрольные суммы и структуру проекта. Изменение источника вызывает
явную ошибку: фиксированные пояснения этого исследования относятся к предоставленному снимку.
Для нового датасета сначала нужно пересмотреть схему, определения и контрольные значения.""")
code("""
from pathlib import Path
import sys, json, math
ROOT = next((p for p in [Path.cwd(), *Path.cwd().parents]
             if (p/'pyproject.toml').exists() and (p/'source/case_dataset.xlsx').exists()), None)
if ROOT is None:
    raise RuntimeError("Откройте блокнот из папки проекта с pyproject.toml и source/")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from IPython.display import display, Markdown
from analysis.notebook_support import (
    ROOT, BIZ_NAMES, read_json, verify_sources, run_script, scalar_records,
    table, figure, report_details, save_environment, validate_results,
)
pd.set_option('display.max_columns', 40)
pd.set_option('display.max_rows', 120)
pd.set_option('display.max_colwidth', 120)
SEED = 42
np.random.seed(SEED)
table(verify_sources(), 'source_hashes')
table(save_environment(), 'environment')
""")

section('2. Все листы и словарь бизнес-показателей',
"""Единица наблюдения в листе «Бизнесы» — сообщество/бизнес. В остальных листах находятся
разные агрегаты платформы. Общего ключа для склейки бизнеса, пользователя, источника и заказа нет.
Поэтому объёмы выборки бизнесов не экстраполируются на всю платформу и не суммируются с агрегатами.""")
code("""
SOURCE = ROOT/'source/case_dataset.xlsx'
book = pd.ExcelFile(SOURCE)
sheets = {name: pd.read_excel(book, sheet_name=name) for name in book.sheet_names}
table([{'Лист':name, 'Строк':len(frame), 'Столбцов':len(frame.columns),
        'Пропущенных ячеек':int(frame.isna().sum().sum())}
       for name,frame in sheets.items()], 'sheet_inventory')
raw_business = sheets['Бизнесы']
assert raw_business.shape == (9754,28)
from openpyxl.utils import get_column_letter
dictionary = pd.DataFrame({
    'Excel': [get_column_letter(i+1) for i in range(28)],
    'Исходное поле':raw_business.columns,
    'Имя в расчётах':BIZ_NAMES,
})
table(dictionary, 'business_dictionary')
""")
md("""
Для чтения таблиц: **seller_rate** — доля бизнесов хотя бы с одним заказом в выбранной группе;
**orders** — число зарегистрированных заказов; **gmv** — их валовая стоимость до отмен,
возвратов и комиссий; **content_opens** — открытия карточки именно из контента.
Показатель original_author — оценка модели, а не установленное качество автора.

Флаги подключений описывают состояние настройки, события — период. Нулевые новые публикации
не исключают старый контент. Нулевые внутренние заказы не исключают сделки другим способом.
Доли в JSON/CSV обычно записаны от 0 до 1; колонки с суффиксом _pp измеряются в процентных пунктах.
В таблицах, где доли переведены в проценты, к названию колонки добавлено (%).
""")

section('3. Аудит качества и распределения всех 28 полей',
"""Проверяем типы, пропуски, диапазоны, дубликаты, флаги и внутренние соотношения.
Диагностический флаг не означает автоматически ошибочную строку: определения и временные
окна могут различаться. Исходные значения сохраняем; неоднозначные случаи не исправляем молча.""")
code("""
run_script('audit_analysis.py')
audit = read_json('analysis/audit_results.json')
table(scalar_records(audit['structure'], 'Показатель'), 'audit_structure')
dist = []
for field,row in audit['distributions'].items():
    item = {k:v for k,v in row.items() if not isinstance(v,(dict,list))}
    item.update({'q_'+k:v for k,v in row.get('quantiles',{}).items()})
    dist.append(item)
table(dist, 'all_field_distributions')
checks = []
for name,row in audit['checks'].items():
    checks.append({'check':name, **{k:v for k,v in row.items() if not isinstance(v,(dict,list))}})
table(checks, 'all_85_quality_checks', max_rows=200)
""")
code("""
# Пропуски рассматриваем по группам, а не подменяем нулями.
missing_profiles = []
for field,block in audit['missingness_analysis'].items():
    for state in ['missing','nonmissing']:
        if state in block:
            missing_profiles.append({'field':field,'state':state,**block[state]})
table(missing_profiles, 'missingness_profiles')
table(audit['identical_non_id_profiles'].get('groups',[]), 'duplicate_profile_groups')
table(audit['type_vs_actual_setup'], 'business_type_vs_actual_tools')
""")
md("""
Средний чек из исходного поля и GMV / заказы проверяем отдельно.
Отклонение не устраняем заменой исходного значения. Удаление таких строк может почти целиком
удалить бизнесы с заказами и исказить дальнейший анализ; эта проблема отдельно исследуется в разделе 24.
""")
code("""
aov_audit = audit['average_ticket_identity']
table(scalar_records({k:v for k,v in aov_audit.items() if not isinstance(v,(list,dict))}, 'Показатель'),
      'aov_identity_summary')
table(aov_audit['all_rows'], 'aov_identity_all_rows', max_rows=400)
table(scalar_records(audit['event_ratio_diagnostics'], 'Отношение'), 'event_ratio_diagnostics')
""")

section('4. Основные расчёты бизнесов',
"""Заново рассчитываем все сегменты, профили, интервальные срезы, ассоциации,
концентрацию, корреляции и четыре диагностические модели. Дополнительный модуль рассчитывает
форматы контента, правила отбора и одномерную стандартизацию.""")
code("""
run_script('core_analysis.py')
run_script('extended_analysis.py')
core = read_json('analysis/core_results.json')
ext = read_json('analysis/extended_results.json')
d = pd.read_csv(ROOT/'analysis/business_prepared.csv')
run_script('make_charts.py')
base = pd.DataFrame([
    {'Сегмент':key, **value} for key,value in core['segments'].items()
])
table(base, 'all_26_business_segments',
      columns=['Сегмент','n','sellers','seller_rate','orders','gmv','ad_spend',
               'median_subscribers','median_active_products','product_publishers','checkout_users'],
      percent=['seller_rate'])
figure('01_catalog_state.png')
""")
md("""
Активный каталог определяется по active_products > 0. Это отличается от исходной текстовой
метки «товарное». Подключение оформления и наличие заказов имеют общую базу активных каталогов,
но не образуют последовательную конверсию. Состояния ниже показывают несколько отдельных сочетаний.
""")
code("""
table(core['states'], 'catalog_content_order_states', percent=['seller_rate'])
figure('04_content_order_matrix.png')
""")

section('5. Размер бизнеса, тип и тематика',
"""Сначала сравниваем размеры групп и доли бизнесов с заказом. Небольшие тематические группы
не ранжируем как доказанные направления роста: несколько наблюдений могут резко менять долю.
Все три базы — общая выборка, активные каталоги, активные каталоги без рефералки — сохранены.""")
code("""
profile_rows=[]
for dimension,scopes in core['profiles'].items():
    for scope,rows in scopes.items():
        profile_rows.extend({'dimension':dimension,'scope':scope,**row} for row in rows)
table(profile_rows, 'all_business_profiles', max_rows=1000,
      columns=['dimension','scope','group','n','sellers','seller_rate','orders','gmv',
               'median_subscribers','median_active_products'], percent=['seller_rate'])
figure('02_subscribers_sales.png')
""")
code("""
# Полностью сохранены все 165 интервальных срезов.
table(core['binned'], 'all_165_binned_profiles', max_rows=1000,
      columns=['scope','feature','band','n','sellers','seller_rate','orders','gmv','ad_spend'],
      percent=['seller_rate'])
""")

section('6. Торговые инструменты и их сочетания',
"""Доставка, оплата, оформление, публикации и реклама могут быть связаны с размером,
готовностью и намерениями бизнеса. Разница между группами описывает совместное состояние.
Она не показывает, насколько изменятся продажи после переключения настройки.""")
code("""
table(core['binary'], 'all_76_feature_states', max_rows=200,
      columns=['scope','feature','value','n','sellers','seller_rate','orders','gmv','ad_spend'],
      percent=['seller_rate'])
table(core['tool_combos'], 'all_8_tool_combinations', percent=['seller_rate'])
figure('03_tools_associations.png')
""")

section('7. Контент, форматы и интерес без заказа',
"""Разделяем обычные и товарные публикации, посты и клипы, новые публикации и события.
Наличие открытий из контента не является обязательным условием зарегистрированного заказа.
Для бизнеса без заказов смотрим объём наблюдаемого интереса, не называя отношение агрегатов конверсией.""")
code("""
table(ext['content_formats'], 'content_formats', percent=['seller_rate'])
table(ext['no_order_interest'], 'interest_without_orders', percent=['seller_rate'])
table(core['stratified_content'], 'all_64_content_strata', max_rows=150, percent=['seller_rate'])
""")
code("""
content_comparison = pd.DataFrame(core['binary'])
table(content_comparison[content_comparison.feature.isin(['has_product_pubs','has_opens','referral'])],
      'content_referral_comparison', percent=['seller_rate'])
referral_profiles = base[base['Сегмент'].isin(['goods_referral','goods_no_referral','referral_no_goods'])]
table(referral_profiles, 'referral_roles', percent=['seller_rate'])
""")
md("""
Реферальное сообщество может рекомендовать чужой товар. Его собственные активные товары и
собственные заказы не измеряют полностью результат рекомендаций. Автора, продавца и партнёра
нужно различать в будущем событийном учёте. Реальная компания-партнёр не определяется из анонимного поля.
""")

section('8. Реклама, концентрация и выбросы',
"""Суммы рекламных трат и GMV могут определяться несколькими крупными строками.
Показываем медианы, доли крупнейших и коэффициент Джини. Траты на рекламу относятся ко всей
рекламе бизнеса; их нельзя целиком отнести к представленным checkout-заказам и получить достоверный ROAS.""")
code("""
ads = pd.DataFrame(core['binary'])
table(ads[ads.feature=='ad_active'], 'advertising_profiles', percent=['seller_rate'])
table(core['concentration'], 'all_126_concentration_cuts', max_rows=200, percent=['share'])
figure('05_concentration.png')
""")
code("""
top_rows=[]
for metric,rows in ext['top_records'].items():
    top_rows.extend({'ranked_by':metric,**row} for row in rows)
table(top_rows, 'top20_records_each_metric', max_rows=150)
""")

section('9. Цены и средний чек',
"""Медианная цена предложения и средний чек заказа измеряют разные вещи.
Отдельно сохраняем цены по тематикам, пропуски и взвешенный чек как сумма GMV / сумма заказов.
Среднее индивидуальных средних чеков не заменяет взвешенный показатель.""")
code("""
table(ext['theme_prices'], 'theme_prices')
table(d.loc[d.seller, ['id','theme','orders','gmv','reported_aov','actual_aov','median_price']],
      'sellers_prices_and_aov', max_rows=400)
""")

section('10. Корреляции',
"""Рассчитаны ранговые корреляции Спирмена в трёх базах. Они описывают совместное изменение
признаков. Переменные с общим числителем, знаменателем и производные показатели могут иметь
связь по построению. Сортировка по модулю коэффициента не превращает корреляцию в фактор влияния.""")
code("""
correlation_rows=[]
for scope,obj in core['correlations'].items():
    correlation_rows.extend({'scope':scope,**row} for row in obj['pairs'])
table(correlation_rows, 'all_1134_correlation_pairs', max_rows=2000)
figure('12_correlations.png')
""")

section('11. Проверки чувствительности и одномерная стандартизация',
"""Ранее сравнивались варианты без верхнего 1% по подписчикам или GMV, с включённым
оформлением и без услуг. Стандартизация выравнивает распределение одного признака.
Она не устраняет все различия между бизнесами. Совместная проверка двух признаков добавлена ниже.""")
code("""
table(core['sensitivity'], 'all_20_previous_sensitivities', max_rows=100, percent=['seller_rate'])
table(ext['standardized'], 'all_20_one_dimension_standardizations',
      percent=['standardized_rate0','standardized_rate1','raw_rate0','raw_rate1'])
table(ext['strata_rows'], 'all_100_one_dimension_strata', max_rows=150, percent=['rate0','rate1'])
figure('11_standardization.png')
""")

section('12. Диагностические модели',
"""Обучены регуляризованные логистические модели наличия хотя бы одного заказа у активного каталога.
Есть два набора признаков и две базы — с рефералкой и без неё. Использованы пять стратифицированных
разбиений; заполнение пропусков, масштабирование и кодирование обучаются внутри каждого разбиения.
В признаки не входят orders, GMV, reported_aov и ID.

Это диагностика **того же месяца**. Даже проверка на отложенных строках не делает её прогнозом
будущей продажи. ROC AUC не является процентом точных прогнозов. Числовые признаки, включая флаги,
стандартизированы, поэтому exp(coefficient) не следует читать как эффект переключения флага 0→1.""")
code("""
model_summary=scalar_records(core['models'], 'model')
table(model_summary, 'all_4_diagnostic_models')
coefficient_rows=[]; calibration_rows=[]
for model,obj in core['models'].items():
    coefficient_rows.extend({'model':model,**row} for row in obj['coefficients'])
    calibration_rows.extend({'model':model,**row} for row in obj['calibration'])
table(coefficient_rows, 'all_234_model_coefficients', max_rows=400)
table(calibration_rows, 'all_40_calibration_rows', percent=['actual','predicted'])
""")

section('13. Сегменты для исследования и пилота',
"""Строгая группа — активные товары, ноль внутренних заказов, ноль новых товарных публикаций,
есть обычные публикации, нет активных услуг и участия в рефералке. Это операционный фильтр
для исследования потребностей. Синтетические ID не являются контактами реальных предпринимателей.
Пороги подписчиков, просмотров и товаров — прозрачные рабочие допущения, а не найденный оптимум.""")
code("""
table(ext['readiness'], 'all_11_readiness_rules', percent=['seller_rate'])
table(core['strict_theme_opportunity'], 'all_strict_group_themes', percent=['seller_rate','share_subscribers_ge1000','share_views_ge1000'])
""")
code("""
# Эти три параметра можно менять и повторно запускать ячейку.
SUBSCRIBERS_MIN = 1000
VIEWS_MIN = 1000
ACTIVE_PRODUCTS_MIN = 10
strict = d.has_goods & ~d.seller & ~d.has_product_pubs & d.has_pubs & ~d.has_services & d.no_referral
custom_candidates = d.loc[
    strict & d.subscribers.ge(SUBSCRIBERS_MIN) & d.views.ge(VIEWS_MIN)
    & d.active_products.ge(ACTIVE_PRODUCTS_MIN) & d.checkout.eq(1)
].copy()
display(Markdown(f'При текущих правилах получено **{len(custom_candidates)} кандидатов**.'))
table(custom_candidates[['id','theme','subscribers','views','active_products','content_opens','ad_spend']],
      'custom_candidate_selection', max_rows=1500)
""")

section('14. Общие итоги Маркета VK и Партнёра',
"""Агрегатный анализ воспроизводится отдельно от бизнес-выборки.
Не связываем разные листы в пользовательскую воронку без соответствующих ключей, единиц
учёта и временного окна. Все отношения счётчиков подписаны как описательные.""")
code("""
run_script('agg_analysis.py')
agg = read_json('analysis/agg_results.json')
monthly_rows=[]
for channel,obj in agg['monthly'].items():
    monthly_rows.append({'channel':channel,**{k:v for k,v in obj.items() if not isinstance(v,(dict,list))}})
table(monthly_rows, 'monthly_all_published_metrics')
derived_rows=[]
for channel in ['vk','partner']:
    derived_rows.extend({'channel':channel,'metric':k,'value':v} for k,v in agg['monthly'][channel]['derived'].items())
table(derived_rows, 'monthly_derived_metrics')
table(scalar_records(agg['monthly_cross_channel_descriptive'].get('partner_to_vk',{}),'metric'),
      'cross_channel_descriptive_ratios')
""")
md("""
У Партнёра есть крупный маршрут в обход карточки. Неизвестно, сколько его заказов пришло
именно по нему. Поэтому все заказы / открытия карточки не являются сопоставимой конверсией
VK и Партнёра. Небольшая доля учтённых оплаченных заказов также не доказывает массовый отказ:
нужно уточнить охват способов оплаты, статусы и лаг регистрации.
""")
code("""
table(scalar_records(agg['partner_bypass'], 'metric'), 'partner_bypass_diagnostics')
table(scalar_records(agg['general_derived'], 'metric'), 'audience_and_search_ratios')
""")

section('15. Сверка дневных и месячных данных',
"""Как месячные итоги суммируем только аддитивные события и суммы. Суммы дневных уникальных
также сохранены для диагностики повторного учёта, но не считаются месячной уникальной аудиторией.
Дневные уникальные пользователи могут
повторяться между днями, поэтому их сумма не обязана совпадать с месячной уникальной аудиторией.
Общее число уникальных покупателей двух каналов нельзя получить сложением без знания пересечения.""")
code("""
def tree_rows(value, prefix=''):
    if isinstance(value,dict):
        out=[]
        for k,v in value.items():
            out.extend(tree_rows(v, prefix+'.'+str(k) if prefix else str(k)))
        return out
    if isinstance(value,list):
        out=[]
        for i,v in enumerate(value):
            out.extend(tree_rows(v, f'{prefix}[{i}]'))
        return out
    return [{'Показатель':prefix,'Значение':value}]
table(tree_rows(agg['quality']), 'aggregate_quality_reconciliations', max_rows=1000)
""")

section('16. Источники открытий и роль общей витрины',
"""В источниках шесть основных строк и вложенная реферальная строка.
Последнюю нельзя прибавлять к шести основным источникам. Открытия карточек не равны заказам.
Источник «Другое» остаётся неизвестным; его нельзя автоматически объявлять трафиком сообщений
или сообществ. Низкая доля главной Маркета сама по себе не обосновывает закрытие витрины.""")
code("""
table(agg['sources']['all_rows'], 'all_7_source_rows')
table(agg['sources']['primary_rows_calculated'], 'all_6_primary_sources')
table(tree_rows({k:v for k,v in agg['sources'].items()
                 if k not in ['all_rows','primary_rows_calculated']}), 'source_reconciliation_and_scope', max_rows=300)
figure('06_traffic_sources.png')
""")

section('17. Товарные категории',
"""Все 18 категорий рассматриваются с собственным знаменателем категориального листа.
Суммы заказов категорий не совпадают с общим итогом. Состав категорий влияет на средние отношения,
но выравнивание состава не устраняет обход карточки и разные правила регистрации заказов.
Из концентрации заказов не следует автоматически прибыльность или приоритет разработки.""")
code("""
table(agg['categories']['all_rows'], 'all_18_product_categories')
table(tree_rows(agg['categories']['totals_and_reconciliation']), 'category_total_reconciliation')
table(tree_rows(agg['categories']['concentration']), 'category_concentration', max_rows=500)
table(tree_rows(agg['categories']['sensitivity']), 'all_category_exclusion_scenarios', max_rows=600)
table(tree_rows(agg['categories']['mix_standardization_descriptive']), 'all_category_mix_standardizations', max_rows=600)
figure('07_categories.png')
""")

section('18. Динамика по дням и календарные сравнения',
"""Исследуем все 30 дней, семь групп дней недели, 12 периодов и четыре сравнения.
Семидневные окна содержат одинаковый набор дней недели. Отдельно проверяем чувствительность к
1 сентября. Один месяц не позволяет утверждать устойчивую сезонность или экстраполировать тренд.""")
code("""
table(agg['daily_all_rows_with_derived_metrics'], 'all_30_days_and_ratios')
table(scalar_records(agg['daily_metric_statistics'], 'metric'), 'all_35_daily_metric_statistics', max_rows=100)
table(tree_rows(agg['daily_groups']), 'all_12_daily_windows', max_rows=3000)
table(tree_rows(agg['weekday_groups']), 'all_7_weekdays', max_rows=2000)
table(tree_rows(agg['daily_comparisons']), 'all_4_period_comparisons', max_rows=3000)
table(tree_rows(agg['first_last_7_gmv_arithmetic_decomposition']), 'gmv_change_arithmetic_decomposition')
figure('08_daily_dynamics.png')
""")
md("""
Разложение GMV на число заказов и средний чек — арифметическое.
Оно не устанавливает, какое продуктовое изменение вызвало движение показателей.
Для причинного объяснения нужны данные об ассортименте, ценах, наличии, рекламе и технических событиях.
""")

section('19. Покупатели и контрольные суммы',
"""Половозрастные и региональные таблицы имеют собственные опубликованные контрольные суммы.
Сначала сверяем их между собой и с контрольными строками общего листа, затем отдельно сравниваем
с общими итогами платформы. Пустые/подавленные ячейки сохраняются как неизвестные значения.""")
code("""
run_script('buyers_analysis.py')
buyers = read_json('analysis/buyers_results.json')
table(buyers['reconciliation'], 'buyers_detail_reconciliation')
table(tree_rows(buyers['quality']), 'buyers_quality_all_fields', max_rows=500)
table(tree_rows(buyers['frequency_and_scope']), 'buyers_frequency_scope')
""")

section('20. Пол, возраст и частота заказов',
"""Показываем все 15 исходных сочетаний, половые и возрастные группы, доли и индексы состава.
Заказы на покупателя за месяц не измеряют удержание следующего месяца или пожизненную ценность.
Сравнение долей в двух каналах не является проникновением в население: численности населения нет.""")
code("""
demo = buyers['demographics']
table(demo['all_rows'], 'all_15_demographic_rows')
table(demo['gender'], 'gender_groups')
table(demo['age'], 'all_age_groups')
table(tree_rows(demo['selected_age_bands']), 'selected_age_bands', max_rows=500)
table(tree_rows(demo['selected_joint_groups']), 'selected_gender_age_groups', max_rows=500)
table(tree_rows(demo['age_quantile_bands']), 'age_quantile_bands', max_rows=300)
table(tree_rows(demo['age_standardisation']), 'age_mix_standardization')
table(tree_rows(demo['concentration']), 'demographic_concentration', max_rows=500)
table(tree_rows(demo['market_frequency_above_partner']), 'demographic_frequency_comparisons', max_rows=500)
figure('09_buyer_age.png')
""")

section('21. Все регионы и неизвестная география',
"""Сохраняем все 89 строк. Используем три разные базы там, где это уместно:
все опубликованные данные, только известные регионы, общие регионы с опубликованными значениями
в обоих каналах. Неизвестную географию не распределяем пропорционально известной без допущения.
Индексы состава не доказывают потенциал региона или низкую конкуренцию.""")
code("""
regions=buyers['regions']
table(regions['all_rows'], 'all_89_regions_all_denominators', max_rows=150)
table(tree_rows({k:regions[k] for k in ['known_totals','common_region_totals','unknown_region']}),
      'region_denominators_and_unknown', max_rows=500)
table(tree_rows(regions['concentration']), 'regional_concentration_and_bounds', max_rows=700)
for key in ['top_composition_index_known_regions','bottom_composition_index_known_regions',
            'largest_positive_share_gaps_known','largest_negative_share_gaps_known',
            'market_frequency_above_partner']:
    table(tree_rows(regions[key]), 'regions_'+key, max_rows=1200)
figure('10_regions_known.png')
""")

section('22. Новая проверка совместной сопоставимости',
"""Ранее подписчики и размер каталога учитывались отдельно. Здесь одновременно формируем
3 × 3 и 5 × 5 квантильных групп. В каждой группе сравниваем наличие признака и заказа,
затем применяем общие веса. Сохраняем все 80 сравнений и 680 ячеек.

Основной пример — активные каталоги без реферальной программы. Исходное различие
уменьшается после выравнивания состава по двум наблюдаемым размерам. Малые ячейки могут делать результат чувствительным.
Ограничение минимального числа наблюдений меняет охваченную совокупность, поэтому обязательно
показываем covered_n и coverage_share. Стандартизация не устанавливает причинный эффект.""")
code("""
run_script('robustness_analysis.py','--source','source/case_dataset.xlsx','--output-dir','analysis')
robust = read_json('analysis/robustness_results.json')
joint=pd.DataFrame(robust['joint_standardization']['summary'])
main_joint=joint[(joint.scope=='goods_no_referral') & (joint.quantiles==5)
                 & (joint.variant=='all') & (joint.minimum_arm_n==1)]
table(main_joint, 'main_joint_standardization',
      columns=['feature','eligible_scope_n','covered_n','coverage_share','raw_rate0','raw_rate1',
               'raw_ratio','standardized_rate0','standardized_rate1','standardized_ratio',
               'min_arm0','min_arm1'], percent=['coverage_share','raw_rate0','raw_rate1','standardized_rate0','standardized_rate1'])
support_comparison=joint[(joint.scope=='goods_no_referral') & (joint.quantiles==5)
                        & (joint.variant=='all') & (joint.feature=='has_product_pubs')]
table(support_comparison, 'common_population_comparison',
      columns=['minimum_arm_n','eligible_scope_n','covered_n','coverage_share',
               'raw_ratio','common_raw_ratio','standardized_ratio'],
      percent=['coverage_share'])
display(Markdown('При ограничении минимум пять наблюдений в каждой ветви меняется совокупность. '
                 'Сравнивать стандартизированное отношение нужно с common_raw_ratio этой же совокупности; '
                 'сопоставление с raw_ratio всей базы смешивает исключение строк и выравнивание состава.'))
table(joint, 'all_80_joint_standardizations', max_rows=150)
table(robust['joint_standardization']['strata'], 'all_680_joint_strata', max_rows=1000)
table(tree_rows(robust['joint_standardization']['quantile_edges']), 'joint_quantile_boundaries', max_rows=500)
""")

section('23. Новая проверка состава пилотного сегмента',
"""Меняем пороги подписчиков и просмотров независимо, отдельно меняем число товаров.
Проверяем ≥ против >, близкие пороги ±20%, тематический состав и исключение крупнейших рекламодателей.
Состав кандидатов может быть относительно устойчивым, даже если сумма их рекламных трат резко меняется.
Эта сумма не является доступным бюджетом пилота или доходом проекта.""")
code("""
candidates=robust['candidates']
table(scalar_records(candidates['profiles'],'profile'), 'candidate_profile_comparisons')
table(candidates['threshold_matrix'], 'all_64_threshold_combinations', max_rows=150)
table(candidates['catalogue_thresholds'], 'catalogue_threshold_sensitivity')
table(tree_rows(candidates['boundary_check']), 'inclusive_exclusive_thresholds', max_rows=500)
table(candidates['outlier_diagnostics'], 'pilot_outlier_sensitivity')
table(candidates['pilot_candidate_rows'], 'all_366_candidates_with_source_rows', max_rows=500)
""")
md("""
Обычные просмотры в правиле отбора не доказывают товарный интерес. У большинства кандидатов
нет открытий товаров из контента. Для реального пилота сначала нужны намерение продавца,
актуальность предложения и подтверждение конкретного препятствия.
""")

section('24. Новый аудит пересечений и риска автоматической очистки',
"""Выбираем три диагностики контентных счётчиков, не использующие заказы, GMV и средний чек.
Считаем пересечения и объёмный вес строк с флагами. Затем проверяем устойчивость описательной
связи товарных публикаций и заказов. Флаг не доказывает ошибочность всех полей затронутой строки.

Отдельно показываем, почему удаление строк с несогласованным средним чеком является отбором
по исходу: такая проверка вообще определяется главным образом у бизнесов с заказами.
Не создаём единственную «очищенную» выборку, скрывающую это смещение.""")
code("""
run_script('extra_audit.py','--source','source/case_dataset.xlsx','--output-dir','analysis')
extra=read_json('analysis/extra_audit_results.json')
table(extra['diagnostics']['pairwise_overlap'], 'new_diagnostic_overlaps')
volume_rows=[]
for diagnostic,obj in extra['diagnostics']['summary'].items():
    for scope,row in obj['by_scope'].items():
        item={'diagnostic':diagnostic,'scope':scope,'n':row['n'],'denominator_n':row['denominator_n'],
              'row_share':row['row_share'],**{'share_'+k:v for k,v in row['metric_shares'].items()}}
        volume_rows.append(item)
table(volume_rows, 'new_diagnostic_volume_weights', max_rows=150)
table(extra['content_sensitivity'], 'all_20_content_diagnostic_sensitivities', max_rows=100)
table(extra['candidate_sensitivity'], 'candidate_stability_to_diagnostics')
table(extra['outcome_dependent_exclusion_warning'], 'aov_exclusion_bias')
aov_exclusion=[]
for row in extra['outcome_dependent_exclusion_warning']:
    aov_exclusion.append({'scope':row['scope'],'sellers_before':row['baseline']['sellers'],
                          'sellers_excluded':row['excluded']['sellers'],
                          'sellers_remaining':row['retained']['sellers'],
                          'excluded_share_of_sellers':row['excluded_share_of_sellers'],
                          'excluded_share_of_orders':row['excluded_share_of_orders'],
                          'excluded_share_of_gmv':row['excluded_share_of_gmv']})
table(aov_exclusion,'aov_exclusion_bias_compact',
      percent=['excluded_share_of_sellers','excluded_share_of_orders','excluded_share_of_gmv'])
""")

section('25. Новые границы выводов при пропусках оценки автора',
"""Сохраняем все известные оценки U и все факты заказов. Неизвестным оценкам разрешаем
принимать 0 или 1. Находим минимальную и максимальную разницу долей бизнесов с заказами
между группами U = 1 и U = 0. Экстремальные значения проверены перебором допустимых
количеств неизвестных меток среди бизнесов с заказами и без них.

Это **границы совместимых заполнений**, а не доверительные интервалы, наиболее вероятная
импутация или причинный эффект оригинальности. Если диапазон содержит оба знака,
знак связи для всей выбранной совокупности без предположений о пропусках не установлен.""")
code("""
author_rows=[]
for row in extra['author_partial_identification']:
    author_rows.append({
      'scope':row['scope'],'n':row['n'],'unknown_n':row['unknown_n'],'unknown_share':row['unknown_share'],
      'known_difference_pp':row['observed_known_labels']['difference_pp'],
      'minimum_difference_pp':row['minimum_compatible_completion']['difference_pp'],
      'maximum_difference_pp':row['maximum_compatible_completion']['difference_pp'],
      'minimum_rate_ratio':row['minimum_compatible_completion']['rate_ratio'],
      'maximum_rate_ratio':row['maximum_compatible_completion']['rate_ratio'],
      'positive_sign_identified':row['positive_sign_identified'],
      'enumeration_verified':row['bound_verification']['passed'],
    })
table(author_rows, 'author_missing_value_identification_bounds', percent=['unknown_share'])
table(tree_rows(extra['author_partial_identification']), 'author_bounds_full_constructions', max_rows=1000)
""")

section('26. Сценарная мощность эксперимента',
"""Число 366 описывает профиль синтетического сегмента и само по себе не задаёт достаточный
размер эксперимента. Рассчитываем 108 сценариев и 36 минимально обнаружимых различий.

Допущения: независимые бизнесы, случайное распределение 1:1, бинарный исход «есть заказ»
за одинаковое будущее окно, одинаковая вероятность внутри ветви, двусторонний точный
тест Фишера при α = 0,05. Будущие частоты контроля 1%, 3%, 5% и 10% заданы как сценарии.
Прошлый ноль заказов в фильтре не означает нулевую будущую частоту.

Мощность — вероятность обнаружить заданное различие, если принятые допущения верны.
Расчёт не определяет ожидаемый эффект, длительность, бюджет или реальную доступность участников.
Числа 430 и 684 используются как сценарные размеры, а не как автоматическая рекомендация
расширить эксперимент за счёт менее готовых продавцов.""")
code("""
power=pd.DataFrame(robust['pilot_power']['scenarios'])
mde=pd.DataFrame(robust['pilot_power']['mde'])
example=power[np.isclose(power.future_control_rate_assumption,.03)
              & np.isclose(power.participation_assumption,1)]
table(example, 'power_example_control_3_percent',
      percent=['future_control_rate_assumption','future_treatment_rate_assumption','exact_fisher_power'])
table(power, 'all_108_power_scenarios', max_rows=150,
      percent=['participation_assumption','future_control_rate_assumption','future_treatment_rate_assumption','exact_fisher_power'])
table(mde, 'all_36_minimum_detectable_changes', max_rows=100,
      percent=['participation_assumption','future_control_rate_assumption','target_power','detectable_treatment_rate'])
table(tree_rows(robust['pilot_power']['implementation_checks']), 'fisher_implementation_verification', max_rows=500)
""")
code("""
run_script('make_extra_charts.py')
figure('13_joint_standardization.png')
figure('14_pilot_thresholds.png')
figure('15_author_bounds.png')
figure('16_pilot_power.png')
""")

section('27. Выводы и проверяемые гипотезы для проекта',
"""Анализ поддерживает работу с результатом уже подготовленных магазинов и отдельную проверку
роли общей витрины. Он не доказывает заранее причину отсутствия заказов.

| Наблюдение | Следующая проверка | Результат, который нужно измерить |
|---|---|---|
| Активные товары и подключённое оформление часто сочетаются с нулём внутренних заказов | Интервью и наблюдение реального пути бизнеса с таким профилем | Подтверждённое препятствие и польза нового процесса |
| Публикации связаны с заказами, но различие уменьшается после учёта размеров | Отдельный эксперимент с конкретной помощью в использовании товарного контента | Доля бизнесов с внутренним заказом и качество исполнения |
| Главная Маркета даёт небольшую долю открытий; витрину активно не развивают | Проверка поиска нового продавца или возвращения к товару | Дополнительные заказы по всем маршрутам, а не перенос на новый экран |
| «Другое» и обход карточки не раскрывают путь заказа | Связанный учёт источника, продавца, товара и статуса заказа | Сопоставимые знаменатели и охват регистрации |
| Оценка автора неполна; границы допускают оба знака связи | Уточнение механизма отсутствия оценки U | Возможность сравнения без произвольного заполнения |
| Размер кандидатной группы не гарантирует обнаружение умеренного эффекта | Расчёт мощности на реальной будущей базе и доступном наборе | Заранее заданный критерий решения о масштабировании |

Для витрины предлагаем роль поиска продавцов и возвращения к покупке. Основные торговые
функции должны помогать получать заказ из сообщества, контента и других подтверждённых
точек входа. Успех измеряется числом бизнесов с полезным результатом и внутренними заказами,
с проверкой исполнения и затрат. Обещанный процент роста из текущего датасета не выводится.
""")

section('28. Полные диагностические приложения',
"""Дополнительно сохраняем глубокие вложенные разрезы: частоты всех значений, положительные
квантили, типы ячеек, исходные строки с диагностическими флагами и принадлежность к группам.
В больших списках один бизнес может встречаться несколько раз. Они не являются числом
уникальных бизнесов. Для таких списков показан небольшой просмотр и явная ссылка на полный CSV.""")
code("""
frequency_rows=[]; distribution_details=[]; audit_top_rows=[]; audit_concentration=[]
for field,obj in audit['distributions'].items():
    values=obj.get('value_counts',[])
    if isinstance(values,dict):
        frequency_rows.extend({'field':field,'value':k,'count':v} for k,v in values.items())
    else:
        frequency_rows.extend({'field':field,**row} for row in values)
    for kind in ['positive_quantiles','raw_python_types','excel_cell_types','tukey_upper_diagnostic']:
        for row in tree_rows(obj.get(kind,{})):
            distribution_details.append({'field':field,'detail':kind,**row})
    audit_top_rows.extend({'field':field,**row} for row in obj.get('top_10',[]))
    concentration=obj.get('concentration',{})
    for row in tree_rows(concentration):
        audit_concentration.append({'field':field,**row})
table(frequency_rows, 'all_field_value_frequencies',max_rows=120)
table(distribution_details, 'positive_quantiles_and_source_types',max_rows=1200)
table(audit_top_rows, 'all_audit_top10_records',max_rows=300)
table(audit_concentration, 'all_audit_concentration_detail',max_rows=1000)

affected=[]; examples=[]; memberships=[]
for check,obj in audit['checks'].items():
    affected.extend({'check':check,**row} for row in obj.get('affected_rows_and_ids',[]))
    examples.extend({'check':check,**row} for row in obj.get('examples',[]))
for group,obj in audit['reconstructed_groups'].items():
    if isinstance(obj,dict):
        memberships.extend({'group':group,**row} for row in obj.get('rows_and_ids',[]))
table(affected, 'all_diagnostic_row_memberships',max_rows=120)
table(examples, 'all_diagnostic_source_examples',max_rows=500)
table(memberships, 'all_reconstructed_group_memberships',max_rows=120)
table(tree_rows(audit['missingness_analysis']), 'complete_missingness_breakdowns',max_rows=1200)
table(audit['commerce_flag_combinations'], 'all_population_tool_combinations')
table(tree_rows({
    'signed_relative_error_quantiles':aov_audit['signed_relative_error_quantiles'],
    'absolute_error_quantiles_rub':aov_audit['absolute_error_quantiles_rub'],
}), 'aov_error_quantiles')
table(tree_rows(audit['event_ratio_diagnostics']), 'all_event_ratio_quantiles',max_rows=500)
rounding=[]
for channel in ['vk','partner']:
    rounding.extend({'channel':channel,'metric':k,'difference':v}
                    for k,v in agg['monthly'][channel]['rounding_checks'].items())
table(rounding, 'all_monthly_rounding_checks')
table(tree_rows(agg['monthly_cross_channel_descriptive']['partner_to_vk_derived']),
      'partner_to_vk_derived_ratios')
table(tree_rows(agg['daily_ratio_definitions']), 'all_17_daily_ratio_definitions',max_rows=200)
table(tree_rows(agg['daily_metric_statistics']), 'all_daily_statistics_dates_and_outliers',max_rows=2000)
""")

section('29. Полные подробные разборы и матрица доказательств',
"""Ниже сохранены полные тексты исследовательских разборов, включая ограничения, таблицы
и определения, которые не помещаются в короткую сводку. Аудит, агрегатный разбор, демография и дополнительный аудит пересоздаются при запуске.
Матрица доказательств и подробные пояснения к совместной стандартизации сохранены как аналитический текст
для исходного снимка; все их числовые результаты пересчитываются в соответствующих разделах. Таблицы core и extended дополнительно
полностью представлены в разделах 4–13 и в CSV.""")
code("""
report_details('analysis/audit_findings.md','Полный аудит бизнес-данных')
report_details('analysis/agg_findings.md','Полный анализ агрегатов, источников, категорий и динамики')
report_details('analysis/buyers_findings.md','Полный анализ покупателей и регионов')
report_details('analysis/extra_audit_findings.md','Дополнительный аудит и границы при пропусках')
report_details('docs/robustness_findings.md','Совместная стандартизация и пилот: подробные пояснения')
report_details('docs/evidence_matrix.md','Матрица доказательств предыдущего анализа; новые уточнения — разделы 22–26')
""")

section('30. Итоговые проверки воспроизведения',
"""Проверяем независимые контрольные счётчики, сверки листов и неизменность исходников.
Известные несведения категориальных итогов сохраняются как ограничения данных и не маскируются
нулевыми разностями. Неожиданное расхождение останавливает выполнение.""")
code("""
validation=validate_results()
table(validation, 'reproducibility_checks')
table(verify_sources(), 'source_hashes_after_analysis')
display(Markdown('**Все контрольные проверки выполнены.** Полные результаты сохранены в analysis/, '
                 'графики — в charts/, параметры среды и журнал проверок — в metadata/.'))
""")

section('31. Источники и границы воспроизводимости',
"""1. **source/case_dataset.xlsx** — оригинальный Excel из кейса, байты сохранены без изменений;
   исходное имя и SHA-256 записаны в metadata/source_manifest.json.
2. **source/case.pdf** — оригинальное условие, в частности страницы 17–20 и 32–34.
3. Предыдущий полный анализ от 18 ноября 2026 года: вычисления перенесены в модули analysis,
   новые проверки добавлены в extra_audit.py и robustness_analysis.py.
4. Официальная документация uv: [Jupyter](https://docs.astral.sh/uv/guides/integration/jupyter/)
   и [фиксация и синхронизация зависимостей](https://docs.astral.sh/uv/concepts/projects/sync/).

На другом процессоре допустимы малые различия чисел с плавающей точкой и оформления графиков.
Счётчики, фильтры и знаменатели должны совпадать. Проверенный запуск выполнен в Linux,
Python 3.12.14, в отдельном окружении uv. Все ячейки выполнены через IPython в новом процессе.
Сетевой запуск Jupyter Server в этой среде не проверен из-за запрета локальных соединений.
Совместимость с другими ОС отдельно не проверялась.
После переноса проекта не копируйте .venv: создайте его заново командой uv sync --locked.
""")

nb=nbf.v4.new_notebook(cells=cells)
nb.metadata={
 'kernelspec':{'display_name':'Python 3 (uv project)','language':'python','name':'python3'},
 'language_info':{'name':'python','version':'3.12.14'},
 'title':'Полный анализ VK CaseChamp 2026',
}
nbf.validate(nb)
out=ROOT/'VK_full_analysis.ipynb'
nbf.write(nb,out)
print(f'Создан {out.name}: {len(cells)} ячеек, {sum(c.cell_type=="code" for c in cells)} с кодом')
