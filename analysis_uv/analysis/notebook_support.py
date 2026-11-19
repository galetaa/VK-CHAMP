"""Утилиты воспроизводимого блокнота: запуск, таблицы, источники, проверки.

Предметные расчёты находятся в отдельных скриптах analysis/*.py.
Все пути вычисляются относительно проекта, сеть для анализа не требуется.
"""
from __future__ import annotations

import hashlib
import html
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from IPython.display import HTML, Image, Markdown, display

ROOT = Path(__file__).resolve().parents[1]
RUN_LOG = []
BIZ_NAMES = [
    'id','theme','business_type','subscribers','posts','clips','product_posts','product_clips',
    'views','likes','reposts','product_views','product_likes','product_reposts','content_opens',
    'uploaded_products','active_products','checkout','delivery','payment','original_author','referral',
    'orders','gmv','reported_aov','ad_spend','active_services','median_price',
]

def read_json(relative):
    return json.loads((ROOT / relative).read_text(encoding='utf-8'))

def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def verify_sources():
    rows=[]
    for item in read_json('metadata/source_manifest.json')['files']:
        p=ROOT/item['path']
        actual=sha256(p)
        valid=actual==item['sha256'] and p.stat().st_size==item['bytes']
        rows.append({'Файл':item['path'],'Байт':p.stat().st_size,'SHA-256':actual,'Совпадает с исходником':valid})
        if not valid:
            raise ValueError(f"Изменился исходный файл {item['path']}. Этот проект проверен на исходном снимке кейса; сначала проверьте новую схему и определения.")
    return pd.DataFrame(rows)

def run_script(name, *arguments):
    """Каждый вызов пересчитывает результат; готовые JSON не используются как кэш."""
    verify_sources()
    script=ROOT/'analysis'/name
    env=os.environ.copy()
    env.update({'MPLBACKEND':'Agg','PYTHONIOENCODING':'utf-8','PYTHONHASHSEED':'42',
                'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1'})
    start=time.perf_counter()
    result=subprocess.run([sys.executable,str(script),*map(str,arguments)],cwd=ROOT,env=env,
                          text=True,encoding='utf-8',capture_output=True)
    elapsed=time.perf_counter()-start
    logs=ROOT/'analysis'/'logs'; logs.mkdir(exist_ok=True)
    (logs/(script.stem+'.log')).write_text(result.stdout+'\n'+result.stderr,encoding='utf-8')
    if result.returncode:
        raise RuntimeError(f'{name}: код {result.returncode}\n{result.stderr[-10000:]}\n{result.stdout[-3000:]}')
    verify_sources()
    RUN_LOG.append({'script':name,'seconds':round(elapsed,3),'return_code':result.returncode})
    print(f'{name}: расчёт завершён за {elapsed:.1f} с')

def scalar_records(mapping, key_name='group'):
    rows=[]
    for key,value in mapping.items():
        if isinstance(value,dict):
            row={key_name:key,**{k:v for k,v in value.items() if not isinstance(v,(dict,list))}}
        else:
            row={key_name:key,'value':value}
        rows.append(row)
    return pd.DataFrame(rows)

def table(data, name, *, columns=None, percent=None, max_rows=120, index=False):
    """Показывает таблицу и сохраняет полный CSV без округления чисел.

    percent перечисляет только доли 0..1; для показа они переводятся в проценты.
    Исходный CSV сохраняет первоначальные имена и единицы измерения.
    """
    if isinstance(data,pd.DataFrame):
        frame=data.copy()
    elif isinstance(data,dict):
        frame=scalar_records(data)
    else:
        frame=pd.DataFrame(data)
    folder=ROOT/'analysis'/'tables'; folder.mkdir(exist_ok=True)
    filename=re.sub(r'[^a-zA-Z0-9_\-]','_',name)+'.csv'
    frame.to_csv(folder/filename,index=index,encoding='utf-8-sig')
    shown=frame if columns is None else frame.loc[:,columns]
    shown=shown.copy()
    for column in percent or []:
        if column in shown:
            shown[column]=pd.to_numeric(shown[column],errors='raise')*100
            shown.rename(columns={column:column+' (%)'},inplace=True)
    if len(shown)>max_rows:
        note=f'Показаны первые {max_rows} из {len(shown)} строк. '
        shown=shown.head(max_rows)
    else:
        note=f'Строк: {len(shown)}. '
    caption=note+f'<a href="analysis/tables/{filename}">Полная таблица CSV</a>. В CSV числовые значения не округлены.'
    body=shown.to_html(index=index,na_rep='нет данных',escape=True,
                       float_format=lambda v:f'{v:,.5g}'.replace(',',' '))
    display(HTML('<p>'+caption+'</p><div style="max-height:620px;overflow:auto">'+body+'</div>'))
    return None

def report_details(relative, title):
    """Полный подробный разбор остаётся доступен внутри блокнота и HTML."""
    import mistune
    renderer=mistune.create_markdown(escape=True,plugins=['table'])
    text=(ROOT/relative).read_text(encoding='utf-8')
    body=renderer(text)
    display(HTML('<details><summary><strong>'+html.escape(title)+'</strong></summary>'+body+'</details>'))

def figure(name, width=1000):
    display(Image(filename=str(ROOT/'charts'/name),width=width))

def save_environment():
    packages=['numpy','pandas','openpyxl','scipy','scikit-learn','matplotlib','pypdf',
              'nbformat','nbclient','nbconvert','ipykernel','jupyterlab']
    versions={p:importlib.metadata.version(p) for p in packages}
    uv=subprocess.run(['uv','--version'],capture_output=True,text=True,check=True).stdout.strip()
    result={'python':platform.python_version(),'system':platform.system(),'machine':platform.machine(),
            'uv':uv,'packages':versions,'uv_lock_sha256':sha256(ROOT/'uv.lock'),
            'executed_at_utc':datetime.now(timezone.utc).isoformat(),
            'python_executable_in_project':str(Path(sys.executable).relative_to(ROOT)) if Path(sys.executable).is_relative_to(ROOT) else Path(sys.executable).name}
    (ROOT/'metadata/environment.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return pd.DataFrame([{'Пакет':k,'Версия':v} for k,v in versions.items()])

def validate_results():
    """Контроль воспроизведения и сверки. Известные несведения не исправляются."""
    verify_sources()
    core=read_json('analysis/core_results.json')
    agg=read_json('analysis/agg_results.json')
    buyers=read_json('analysis/buyers_results.json')
    d=pd.read_csv(ROOT/'analysis/business_prepared.csv')
    strict=d.has_goods & ~d.seller & ~d.has_product_pubs & d.has_pubs & ~d.has_services & d.no_referral
    pilot=strict & d.views.ge(1000) & d.subscribers.ge(1000) & d.active_products.ge(10) & d.checkout.eq(1)
    pairs=[
      ('Число бизнесов',len(d),9754),
      ('Уникальные ID',d.id.nunique(),9754),
      ('Активные каталоги',int(d.has_goods.sum()),3961),
      ('Бизнесы с заказами',int(d.seller.sum()),298),
      ('Каталоги с заказами',int((d.has_goods & d.seller).sum()),285),
      ('Заказы выборки',int(d.orders.sum()),3478),
      ('GMV выборки, руб.',int(d.gmv.sum()),9973027),
      ('Строгий сегмент',int(strict.sum()),1817),
      ('Узкий кандидатный сегмент',int(pilot.sum()),366),
      ('Дни в агрегатах',len(agg['daily_all_rows_with_derived_metrics']),30),
      ('Сумма дневных заказов VK',sum(x['orders_vk'] for x in agg['daily_all_rows_with_derived_metrics']),agg['monthly']['vk']['orders']),
      ('Сумма дневных заказов Партнёра',sum(x['orders_partner'] for x in agg['daily_all_rows_with_derived_metrics']),agg['monthly']['partner']['orders']),
      ('Источник хеша модели совпадает с XLSX',core['metadata']['sha256'],sha256(ROOT/'source/case_dataset.xlsx')),
    ]
    for item in buyers['reconciliation']:
        pairs.append(('Покупатели/заказы: детали и контроль '+item['metric'],item['demographic_sum'],item['control_total']))
        pairs.append(('Покупатели/заказы: демография и регионы '+item['metric'],item['demographic_sum'],item['regional_sum']))
    rows=[{'Проверка':name,'Получено':actual,'Ожидалось':expected,'Совпало':bool(actual==expected)} for name,actual,expected in pairs]
    result={'checks':rows,'run_log':RUN_LOG,'all_passed':all(r['Совпало'] for r in rows),
            'known_data_issues':[
             'Категориальные заказы не сведены с общим итогом. Это сохранённое свойство источника.',
             'Пропуски и неопределённые отношения не заменены нулём.',
             'Модель описывает одновременный синтетический срез, а не прогноз следующего периода.',
             'Все сценарии мощности задают будущую базовую частоту как допущение.'
            ]}
    (ROOT/'metadata/validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    if not result['all_passed']:
        raise AssertionError([r for r in rows if not r['Совпало']])
    return pd.DataFrame(rows)
