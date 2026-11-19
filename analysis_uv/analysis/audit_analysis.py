"""Independent read-only audit of the case's business microdata.

No workbook is written. JSON retains exact filters, denominators, cell addresses,
all affected row/ID pairs and the distributions of all 28 original columns.
"""
from pathlib import Path
from collections import Counter
from collections import defaultdict
import hashlib
import json
import math
import zipfile
import xml.etree.ElementTree as ET
import numpy as np
import pandas as pd
import openpyxl

ROOT = Path(__file__).resolve().parents[1]
SOURCE = next((ROOT / 'source').glob('*.xlsx'))
OUTPUT = ROOT / 'analysis'
OUTPUT.mkdir(parents=True, exist_ok=True)
book = openpyxl.load_workbook(SOURCE, data_only=False)
sheet = book['Бизнесы']
raw = list(sheet.values)
headers = list(raw[0])
letters = [openpyxl.utils.get_column_letter(i+1) for i in range(len(headers))]
data = pd.DataFrame(raw[1:], columns=letters)
N = len(data)
data['excel_row'] = data.index + 2
COLS = dict(zip(letters, headers))
BINARY = ['R','S','T','U','V']
NUMERIC = [c for c in letters if c not in ['B','C']]
ADDITIVE = [c for c in NUMERIC if c not in ['A','R','S','T','U','V','Y','AB']]

def clean(x):
    if isinstance(x, dict): return {str(k): clean(v) for k,v in x.items()}
    if isinstance(x, (list, tuple)): return [clean(v) for v in x]
    if isinstance(x, (np.integer,)): return int(x)
    if isinstance(x, (np.bool_,)): return bool(x)
    if isinstance(x, (np.floating,)): return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, float) and not math.isfinite(x): return None
    if x is pd.NA: return None
    return x

def pct(n, denominator=N):
    return None if denominator == 0 else float(n/denominator*100)

def row_records(df, columns=None):
    cols = ['excel_row','A'] + (columns or [])
    return clean(df[list(dict.fromkeys(cols))].rename(columns={'A':'id'}).to_dict('records'))

def number(v):
    if pd.isna(v): return '—'
    if isinstance(v,(int,np.integer)) or float(v).is_integer(): return f'{int(v):,}'.replace(',',' ')
    return f'{float(v):,.2f}'.replace(',',' ').replace('.',',')

def ratio_text(n,d):
    return f'{number(n)} / {number(d)} = {pct(n,d):.2f}%'.replace('.',',')

def summary(df):
    return {
        'n':len(df),'orders':int(df['W'].sum()),'gmv_rub':int(df['X'].sum()),
        'ads_rub':int(df['Z'].sum()),'active_catalog_n':int(df['Q'].gt(0).sum()),
        'active_services_n':int(df['AA'].gt(0).sum()),
        'selling_n':int(df['W'].gt(0).sum()),
        'product_publications_n':int((df['G']+df['H']).gt(0).sum()),
        'ordinary_publications_field_n':int((df['E']+df['F']).gt(0).sum()),
        'product_views_n':int(df['L'].gt(0).sum()),'content_opens_n':int(df['O'].gt(0).sum()),
        'product_views':int(df['L'].sum()),'content_opens':int(df['O'].sum()),
        'checkout_n':int(df['R'].eq(1).sum()),'delivery_n':int(df['S'].eq(1).sum()),
        'payment_n':int(df['T'].eq(1).sum()),'referral_n':int(df['V'].eq(1).sum()),
        'original_yes_n':int(df['U'].eq(1).sum()),'original_missing_n':int(df['U'].isna().sum())
    }

def distribution(c):
    vals = data[c]
    actual = [r[letters.index(c)] for r in raw[1:]]
    nonnull = vals.dropna()
    out = {'excel_column':c,'header':COLS[c],'range':f"'Бизнесы'!{c}2:{c}{N+1}",
           'n':N,'nonmissing_n':int(vals.notna().sum()),'missing_n':int(vals.isna().sum()),
           'missing_pct':pct(vals.isna().sum()),'distinct_nonmissing':int(vals.nunique()),
           'raw_python_types':dict(Counter(type(v).__name__ for v in actual)),
           'excel_cell_types':dict(Counter(sheet.cell(r,letters.index(c)+1).data_type for r in range(2,N+2))),
           'pandas_dtype':str(vals.dtype),
           'empty_string_n':sum(isinstance(v,str) and v=='' for v in actual),
           'whitespace_only_n':sum(isinstance(v,str) and v!='' and v.strip()=='' for v in actual)}
    if c in NUMERIC:
        conv = pd.to_numeric(vals,errors='coerce')
        finite = conv.dropna()
        out.update({'numeric_parse_fail_n':int((vals.notna()&conv.isna()).sum()),
                    'zero_n':int(conv.eq(0).sum()),'positive_n':int(conv.gt(0).sum()),
                    'negative_n':int(conv.lt(0).sum()),'nonfinite_n':int((~np.isfinite(finite)).sum()),
                    'noninteger_n':int((finite%1!=0).sum()),
                    'min':float(finite.min()),'max':float(finite.max()),'mean':float(finite.mean()),
                    'std':float(finite.std()),'sum':float(finite.sum()),
                    'quantiles':{str(q):float(finite.quantile(q)) for q in [0,.01,.05,.1,.25,.5,.75,.9,.95,.99,.999,1]},
                    'positive_quantiles':{str(q):float(finite[finite>0].quantile(q)) for q in [.1,.25,.5,.75,.9,.95,.99,1]} if (finite>0).any() else {},
                    'sum_interpretation':('additive counts/amounts; population overlaps may exist' if c in ADDITIVE else 'not a meaningful aggregate; identifier, flag, price or average')})
        q1,q3 = finite.quantile(.25),finite.quantile(.75)
        fence = q3+1.5*(q3-q1)
        out['tukey_upper_diagnostic']={'fence':float(fence),'above_n':int(finite.gt(fence).sum()),'warning':'Not a data-error definition. For zero-inflated columns the fence can equal zero.'}
        if c != 'A' and c not in BINARY:
            out['top_10'] = row_records(data.nlargest(10,c),['B','C',c])
        if c in ADDITIVE:
            total=finite.sum()
            out['concentration']={}
            for label,k in [('top_1',1),('top_3',3),('top_10',10),('top_100',100),('top_1pct',math.ceil(N*.01)),('top_5pct',math.ceil(N*.05)),('top_10pct',math.ceil(N*.1))]:
                amount=data.nlargest(k,c)[c].sum()
                out['concentration'][label]={'n':k,'sum':float(amount),'share_of_total':float(amount/total) if total else None}
    # Full frequency distribution except the unique identifier. This is sufficient
    # to reconstruct any univariate binned count without dumping the raw matrix.
    if c!='A':
        counts=Counter('<MISSING>' if pd.isna(v) else v for v in vals)
        out['value_counts']=[{'value':clean(k),'n':int(v)} for k,v in sorted(counts.items(),key=lambda item:(str(type(item[0])),str(item[0])))]
    return clean(out)

checks={}
def check(name,mask,rule,classification,columns=None,meaning=''):
    mask=mask.fillna(False)
    d=data[mask]
    checks[name]={'rule':rule,'classification':classification,'n':len(d),'pct_all':pct(len(d)),
                  'interpretation':meaning,'affected_rows_and_ids':row_records(d),
                  'examples':row_records(d.head(10),columns or [])}

# Structural and numerical checks.
check('duplicate_id',data['A'].duplicated(keep=False),'A duplicate, keep=False','hard structural issue',['A'])
check('duplicate_complete_row',data[letters].duplicated(keep=False),'All 28 original cells identical','hard structural issue')
check('duplicate_except_id',data[[c for c in letters if c!='A']].duplicated(keep=False),'27 non-ID original cells identical','possible coincidental identical profiles')
for c in NUMERIC:
    check(f'negative_{c}',data[c].lt(0),f'{c}<0','numeric domain diagnostic',[c])
for c in BINARY:
    check(f'invalid_binary_{c}',data[c].notna()&(~data[c].isin([0,1])),f'{c} not blank and not in {{0,1}}','hard type/domain issue',[c])
for c in ['B','C']:
    check(f'outer_whitespace_{c}',data[c].str.strip().ne(data[c]),f'trim({c}) != {c}','text normalization diagnostic',[c])

check('active_goods_above_uploaded',data['Q']>data['P'],'Q>P','potential count inconsistency',['P','Q'])
check('active_goods_with_zero_uploaded',data['Q'].gt(0)&data['P'].eq(0),'Q>0 and P=0','potential count inconsistency',['P','Q'])
check('uploaded_no_active_goods',data['P'].gt(0)&data['Q'].eq(0),'P>0 and Q=0','valid inventory state / definition needed',['P','Q','AB'])
check('without_catalog_label_has_active_goods',data['C'].eq('без каталога')&data['Q'].gt(0),"C='без каталога' and Q>0",'label/state mismatch',['C','P','Q','AA'])
check('without_catalog_label_has_active_services',data['C'].eq('без каталога')&data['AA'].gt(0),"C='без каталога' and AA>0",'label/state mismatch',['C','Q','AA'])
check('goods_label_no_active_goods',data['C'].eq('товарное')&data['Q'].eq(0),"C='товарное' and Q=0",'not automatically an error; inactive assortment',['C','P','Q','AA'])
check('goods_label_has_active_services',data['C'].eq('товарное')&data['AA'].gt(0),"C='товарное' and AA>0",'label/state mismatch or nonexclusive labeling',['C','Q','AA'])
check('services_label_has_active_goods',data['C'].eq('услужное')&data['Q'].gt(0),"C='услужное' and Q>0",'label/state mismatch',['C','Q','AA'])
check('services_label_no_active_services',data['C'].eq('услужное')&data['AA'].eq(0),"C='услужное' and AA=0",'not automatically an error; services not configured',['C','Q','AA'])
check('mixed_label_missing_one_active_kind',data['C'].eq('товарное и услужное')&((data['Q']==0)|(data['AA']==0)),"C='товарное и услужное' and (Q=0 or AA=0)",'not automatically an error; current setup differs from business role',['C','Q','AA'])

check('orders_without_checkout',data['W'].gt(0)&data['R'].eq(0),'W>0 and R=0','temporal or semantic inconsistency',['R','W','X'], 'Could reflect different snapshot/event windows, or synthetic inconsistency. Do not infer checkout is unnecessary.')
check('orders_without_delivery',data['W'].gt(0)&data['S'].eq(0),'W>0 and S=0','not automatically an error',['R','S','T','W','X'])
check('orders_without_payment',data['W'].gt(0)&data['T'].eq(0),'W>0 and T=0','not automatically an error',['R','S','T','W','X'],'W is placed checkout orders, not proof of payment; T may represent a specific enabled online payment feature.')
check('orders_without_active_goods',data['W'].gt(0)&data['Q'].eq(0),'W>0 and Q=0','temporal/assortment scope diagnostic',['C','Q','AA','W','X'])
check('orders_zero_gmv',data['W'].gt(0)&data['X'].eq(0),'W>0 and X=0','meaning/zero-value order diagnostic',['W','X','Y'])
check('gmv_without_orders',data['X'].gt(0)&data['W'].eq(0),'X>0 and W=0','mathematical aggregate inconsistency',['W','X','Y'])
check('aov_missing_with_orders',data['W'].gt(0)&data['Y'].isna(),'W>0 and Y blank','missing calculable metric / definition needed',['W','X','Y'])
check('aov_present_without_orders',data['W'].eq(0)&data['Y'].notna(),'W=0 and Y not blank','undefined average diagnostic',['W','X','Y'])
check('median_price_missing_with_active_goods',data['Q'].gt(0)&data['AB'].isna(),'Q>0 and AB blank','missing commercially relevant input',['P','Q','AB'])
check('median_price_present_without_active_goods',data['Q'].eq(0)&data['AB'].notna(),'Q=0 and AB not blank','likely broader/previous assortment; definition needed',['P','Q','AB'])
check('median_price_present_without_uploaded_goods',data['P'].eq(0)&data['AB'].notna(),'P=0 and AB not blank','potential scope inconsistency',['P','Q','AB'])
check('median_price_zero',data['AB'].eq(0),'AB=0','zero/free/placeholder price; not automatically error',['C','Q','AB'])
check('checkout_without_active_goods',data['R'].eq(1)&data['Q'].eq(0),'R=1 and Q=0','feature flag is not a sequential catalog funnel',['C','Q','AA','R'])
check('payment_without_checkout',data['T'].eq(1)&data['R'].eq(0),'T=1 and R=0','flag independence or inconsistent state',['R','T','W'])
check('delivery_without_checkout',data['S'].eq(1)&data['R'].eq(0),'S=1 and R=0','flag independence or inconsistent state',['R','S','W'])

# The post counters are not documented as a reliably nested partition.
check('product_posts_above_posts',data['G']>data['E'],'G>E','ambiguous counter subset assumption',['E','F','G','H'])
check('product_clips_above_clips',data['H']>data['F'],'H>F','ambiguous counter subset assumption',['E','F','G','H'])
check('product_publications_above_other_counter',data['G']+data['H']>data['E']+data['F'],'G+H>E+F','ambiguous counter subset assumption',['E','F','G','H'])
check('product_publications_with_EF_zero',(data['G']+data['H']).gt(0)&(data['E']+data['F']).eq(0),'G+H>0 and E+F=0','ambiguous counter subset assumption',['E','F','G','H'])
check('ordinary_views_without_EF_publications',data['I'].gt(0)&(data['E']+data['F']).eq(0),'I>0 and E+F=0','could be views of older content',['E','F','I'])
check('product_views_without_GH_publications',data['L'].gt(0)&(data['G']+data['H']).eq(0),'L>0 and G+H=0','could be views of older content',['G','H','L','O'])
check('content_opens_without_GH_publications',data['O'].gt(0)&(data['G']+data['H']).eq(0),'O>0 and G+H=0','could be interaction with older content',['G','H','L','O'])
check('EF_publications_without_ordinary_views',(data['E']+data['F']).gt(0)&data['I'].eq(0),'E+F>0 and I=0','scope/counter definition diagnostic',['E','F','I'])
check('GH_publications_without_product_views',(data['G']+data['H']).gt(0)&data['L'].eq(0),'G+H>0 and L=0','event visibility diagnostic',['G','H','L'])
for views,likes,reposts,label in [('I','J','K','ordinary'),('L','M','N','product')]:
    check(f'{label}_likes_without_views',data[likes].gt(0)&data[views].eq(0),f'{likes}>0 and {views}=0','event counter scope diagnostic',[views,likes,reposts])
    check(f'{label}_reposts_without_views',data[reposts].gt(0)&data[views].eq(0),f'{reposts}>0 and {views}=0','event counter scope diagnostic',[views,likes,reposts])
    check(f'{label}_likes_above_views',data[likes]>data[views],f'{likes}>{views}','not automatically impossible without matching event definitions',[views,likes,reposts])
    check(f'{label}_reposts_above_views',data[reposts]>data[views],f'{reposts}>{views}','not automatically impossible without matching event definitions',[views,likes,reposts])
    check(f'{label}_reactions_above_views',data[likes]+data[reposts]>data[views],f'{likes}+{reposts}>{views}','not a unique-user engagement rate',[views,likes,reposts])
check('content_opens_without_product_views',data['O'].gt(0)&data['L'].eq(0),'O>0 and L=0','event counter scope diagnostic',['G','H','L','O'])
check('content_opens_above_product_views',data['O']>data['L'],'O>L','not a clean event funnel without definition/attribution',['L','O'])
check('orders_above_content_opens',data['W']>data['O'],'W>O','NOT an error: O covers content source only',['O','W'])
check('orders_without_content_opens',data['W'].gt(0)&data['O'].eq(0),'W>0 and O=0','NOT an error: O covers content source only',['O','W','X'])

# Missing values and average-ticket identity.
missing_cross={}
for col in ['U','Y','AB']:
    m=data[col].isna()
    missing_cross[col]={'missing':summary(data[m]),'nonmissing':summary(data[~m]),
                        'by_type':{str(t):{'n':len(g),'missing_n':int(g[col].isna().sum())} for t,g in data.groupby('C')}}
missing_cross['Y']['by_orders_gmv']=[{'orders_positive':bool(w),'gmv_positive':bool(x),'n':len(g),'missing_y_n':int(g['Y'].isna().sum())} for (w,x),g in data.groupby([data['W']>0,data['X']>0])]
missing_cross['AB']['by_uploaded_active']=[{'uploaded_positive':bool(p),'active_positive':bool(q),'n':len(g),'missing_price_n':int(g['AB'].isna().sum()),'zero_price_n':int(g['AB'].eq(0).sum())} for (p,q),g in data.groupby([data['P']>0,data['Q']>0])]
missing_cross['U']['by_current_publications']=[{'EF_positive':bool(k),'n':len(g),'unknown_n':int(g['U'].isna().sum())} for k,g in data.groupby((data['E']+data['F'])>0)]

valid=data[data['W'].gt(0)&data['Y'].notna()].copy()
valid['aov_recomputed']=valid['X']/valid['W']
valid['aov_abs_error']=valid['Y']-valid['aov_recomputed']
valid['aov_relative_error']=valid['Y']/valid['aov_recomputed']-1
diffmask=data['W'].gt(0)&data['Y'].notna()&((data['Y']-data['X']/data['W'].replace(0,np.nan)).abs()>1)
check('aov_identity_error_gt_1rub',diffmask,'W>0, Y nonblank, abs(Y-X/W)>1 ₽','confirmed failure of stated identity, beyond simple ±1 ₽ rounding',['W','X','Y'])
aov={
    'definition_source':'PDF p.34: average ticket = GMV / orders',
    'denominator_nonmissing_with_orders':len(valid),'all_positive_orders_n':int(data['W'].gt(0).sum()),
    'exact_equal_n':int(valid['aov_abs_error'].eq(0).sum()),
    'abs_diff_le_half_ruble_n':int(valid['aov_abs_error'].abs().le(.5).sum()),
    'abs_diff_le_one_ruble_n':int(valid['aov_abs_error'].abs().le(1).sum()),
    'abs_diff_gt_one_ruble_n':int(valid['aov_abs_error'].abs().gt(1).sum()),
    'abs_relative_error_gt_1pct_n':int(valid['aov_relative_error'].abs().gt(.01).sum()),
    'abs_relative_error_gt_5pct_n':int(valid['aov_relative_error'].abs().gt(.05).sum()),
    'abs_relative_error_gt_10pct_n':int(valid['aov_relative_error'].abs().gt(.1).sum()),
    'abs_relative_error_gt_20pct_n':int(valid['aov_relative_error'].abs().gt(.2).sum()),
    'signed_relative_error_quantiles':valid['aov_relative_error'].quantile([0,.1,.25,.5,.75,.9,.95,1]).to_dict(),
    'absolute_error_quantiles_rub':valid['aov_abs_error'].abs().quantile([0,.1,.25,.5,.75,.9,.95,1]).to_dict(),
    'all_business_gmv_over_orders':float(data['X'].sum()/data['W'].sum()),
    'order_weighted_reported_aov_where_available':float((valid['Y']*valid['W']).sum()/valid['W'].sum()),
    'same_rows_gmv_over_orders':float(valid['X'].sum()/valid['W'].sum()),
    'all_rows':row_records(valid,['W','X','Y','aov_recomputed','aov_abs_error','aov_relative_error']),
    'largest_absolute_errors':row_records(valid.loc[valid['aov_abs_error'].abs().nlargest(10).index],['B','C','W','X','Y','aov_recomputed','aov_abs_error','aov_relative_error']),
    'largest_relative_errors':row_records(valid.loc[valid['aov_relative_error'].abs().nlargest(10).index],['B','C','W','X','Y','aov_recomputed','aov_abs_error','aov_relative_error'])
}

# Reconstruct earlier groups from first principles, no previous result files.
catalog=data['Q']>0
selling=data['W']>0
product=(data['G']+data['H'])>0
activity=(data['E']+data['F'])>0
strict=catalog&(~selling)&(~product)&activity&data['AA'].eq(0)&data['V'].eq(0)
group_defs={
 'strict_1817':(strict,'Q>0; W=0; G+H=0; E+F>0; AA=0; V=0',1817),
 'strict_checkout_1670':(strict&data['R'].eq(1),'strict_1817 and R=1',1670),
 'catalog_content_no_orders_204':(catalog&product&(~selling),'Q>0; G+H>0; W=0',204),
 'catalog_content_no_orders_lt10_opens_163':(catalog&product&(~selling)&data['O'].lt(10),'Q>0; G+H>0; W=0; O<10',163),
 'catalog_sellers_285':(catalog&selling,'Q>0; W>0',285),
 'catalog_sellers_zero_content_opens_206':(catalog&selling&data['O'].eq(0),'Q>0; W>0; O=0',206),
 'active_catalog_3961':(catalog,'Q>0',3961),
 'all_sellers_298':(selling,'W>0',298),
 'catalog_no_content_no_orders_3472':(catalog&(~product)&(~selling),'Q>0; G+H=0; W=0',3472),
 'catalog_posting_246':(catalog&product,'Q>0; G+H>0',246),
 'catalog_selling_posting_42':(catalog&product&selling,'Q>0; G+H>0; W>0',42),
 'catalog_no_content_3715':(catalog&(~product),'Q>0; G+H=0',3715),
 'catalog_selling_no_content_243':(catalog&(~product)&selling,'Q>0; G+H=0; W>0',243)
}
groups={k:{'rule':rule,'previous_count':previous,'recomputed_count':int(mask.sum()),'matches_previous':int(mask.sum())==previous,
           'summary':summary(data[mask]),'rows_and_ids':row_records(data[mask])} for k,(mask,rule,previous) in group_defs.items()}
posting_zero=data[catalog&product&(~selling)]
groups['204_partition']={'rule':'Q>0; G+H>0; W=0','n':len(posting_zero),
                       'zero_product_views_n':int(posting_zero['L'].eq(0).sum()),
                       'zero_opens_n':int(posting_zero['O'].eq(0).sum()),
                       'opens_1_9_n':int(posting_zero['O'].between(1,9).sum()),
                       'opens_10_99_n':int(posting_zero['O'].between(10,99).sum()),
                       'opens_100plus_n':int(posting_zero['O'].ge(100).sum())}

type_matrix=[]
for (kind,goods,services),g in data.groupby(['C',data['Q']>0,data['AA']>0]):
    type_matrix.append({'business_type':kind,'active_goods':bool(goods),'active_services':bool(services),**summary(g)})
flag_matrix=[{'checkout':int(r),'delivery':int(s),'payment':int(t),**summary(g)} for (r,s,t),g in data.groupby(['R','S','T'])]
profile_members=defaultdict(list)
for i,row in enumerate(raw[1:],start=2):
    profile_members[tuple(row[1:])].append({'excel_row':i,'id':row[0]})
identical_profiles=[{'n':len(members),'members':members,'non_id_values':dict(zip(letters[1:],profile))} for profile,members in profile_members.items() if len(members)>1]

ratio_diagnostics={}
for label,numerator,denominator in [('ordinary_likes_per_view','J','I'),('ordinary_reposts_per_view','K','I'),('product_likes_per_view','M','L'),('product_reposts_per_view','N','L'),('content_opens_per_product_view','O','L')]:
    d=data[data[denominator]>0]
    rat=d[numerator]/d[denominator]
    ratio_diagnostics[label]={'numerator':numerator,'denominator':denominator,'eligible_n':len(d),
                              'ratio_of_totals':float(d[numerator].sum()/d[denominator].sum()),
                              'community_ratio_quantiles':rat.quantile([0,.25,.5,.75,.9,.95,.99,1]).to_dict(),
                              'warning':'Event-count diagnostic, not a unique-user causal conversion. Counter coverage and periods need confirmation.'}

out={
 'source':{'path':str(SOURCE),'sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(),'sheet':'Бизнесы','range':sheet.calculate_dimension(),
           'n_data_rows':N,'n_columns':len(headers),'month_context':'Workbook aggregate sheets: 2026-09-01–2026-09-30. No business-level timestamps.',
           'synthetic_note':'PDF p.32 says these are synthetic anonymized case data, not real company KPIs.',
           'definition_notes':['PDF p.33 describes monthly activity, connected tools, orders and ads. It does not specify whether flags are end-of-month snapshots.',
                               'P is a numeric count (0..47021), not a binary flag.',
                               'O explicitly counts card openings from content, not all product card openings.',
                               'W/X cover checkout only; Z covers all advertising expenses.',
                               'PDF p.34 defines GMV before cancellation/return/commission deductions.']},
 'structure':{'all_nonempty_rows':int(data[letters].notna().any(axis=1).sum()),'all_blank_rows_n':int(data[letters].isna().all(axis=1).sum()),
              'unique_ids':int(data['A'].nunique()),'id_min':int(data['A'].min()),'id_max':int(data['A'].max()),
              'id_set_is_1_to_9754':set(data['A'])==set(range(1,N+1)),
              'missing_cells':int(data[letters].isna().sum().sum()),'nonmissing_cells':int(data[letters].notna().sum().sum()),
              'formulas':[(cell.coordinate,cell.value) for row in sheet for cell in row if cell.data_type=='f'],
              'excel_errors':[(cell.coordinate,cell.value) for row in sheet for cell in row if cell.data_type=='e'],
              'comments':[(cell.coordinate,cell.comment.text) for row in sheet for cell in row if cell.comment],
              'hidden_rows':[i for i,v in sheet.row_dimensions.items() if v.hidden],
              'hidden_columns':[i for i,v in sheet.column_dimensions.items() if v.hidden],
              'merged_ranges':[str(r) for r in sheet.merged_cells.ranges],
              'sheet_state':sheet.sheet_state},
 'totals':summary(data),
 'distributions':{c:distribution(c) for c in letters},
 'binary_value_counts':{c:{('<MISSING>' if pd.isna(k) else str(int(k))):int(v) for k,v in data[c].value_counts(dropna=False).items()} for c in BINARY},
 'missingness_analysis':missing_cross,'checks':checks,'average_ticket_identity':aov,
 'type_vs_actual_setup':type_matrix,'commerce_flag_combinations':flag_matrix,
 'identical_non_id_profiles':{'n_groups':len(identical_profiles),'n_rows':sum(g['n'] for g in identical_profiles),'groups':identical_profiles,'interpretation':'Different unique IDs can legitimately share sparse rounded synthetic profiles; not a basis for deleting observations.'},
 'reconstructed_groups':groups,'event_ratio_diagnostics':ratio_diagnostics,
 'limitations':[
  'No business sampling method, sampling weights or mapping to full Market totals; do not extrapolate counts to the whole VK universe.',
  'Cross-sectional observation, no random assignment or before/after timestamps; associations are not effects of tools/content.',
  'No lifetime history or first-sale date; W=0 means no recorded checkout orders in the observed window, not never sold.',
  'No complete sales outside checkout, messages, partner flows or offline; zero W is not no sales.',
  'O only covers openings from content, no post–product–business–order attribution keys.',
  'V/U describe overlapping roles, U is a model estimate and has missing values; authors and own-product sellers must not be conflated.',
  'No post IDs/age, equal exposure windows, paid/organic split, unique reach or reliable nesting of E/F and G/H; do not estimate per-post recommendation suppression.',
  'R/S/T snapshot time and feature scope are unspecified; observed orders with off flags cannot establish necessity/irrelevance of the tool.',
  'Missing U is unknown, missing price is unavailable; not zero. Missing Y largely follows zero GMV.',
  'Reported Y fails the declared GMV/orders identity for most nonmissing rows; use explicit recomputation on matched rows if needed, preserving source values.',
  'Z all-advertising spend and X checkout-only GMV do not support total-business ROAS; no margins, commissions, customer acquisition cost or contribution profit.',
  'Counts of views, reactions and openings are not unique users; repeated activity and differing scopes can invalidate direct funnel ratios.',
  'Heavy concentration and zero inflation make means sensitive to a few business rows; tails are not automatically errors.',
  'Synthetic rounding/perturbation may generate inconsistencies. Preserve and disclose rather than silently repair.'
 ]
}
out=clean(out)
(OUTPUT/'audit_results.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf8')

# Human-readable audit, generated from the same independently recomputed results.
lines=['# Независимый аудит бизнес-микроданных VK CaseChamp 2026','',
       f"Источник: `{SOURCE.name}`, лист «Бизнесы», A1:AB9755. SHA-256: `{out['source']['sha256']}`.",'',
       'Проверены все 9 754 строки данных и 28 исходных полей. Исходная книга не изменялась. Это синтетические данные кейса (PDF, с. 32), а не реальные показатели VK.','',
       '## 1. Контроль структуры и основные итоги','',
       f"- Уникальных ID: {number(out['structure']['unique_ids'])}; множество ID точно равно 1…9 754.",
       f"- Полных дубликатов: {checks['duplicate_complete_row']['n']}; дубликатов ID: {checks['duplicate_id']['n']}; одинаковых профилей без ID: {checks['duplicate_except_id']['n']} строк.",
       f"- Заполненных ячеек: {number(out['structure']['nonmissing_cells'])}; пустых: {number(out['structure']['missing_cells'])}.",
       '- Формул, ошибок Excel, комментариев, скрытых строк/колонок и объединённых ячеек на листе нет.',
       '- Отрицательных чисел, нецелых чисел и недопустимых значений бинарных флагов не обнаружено.',
       f"- Активных каталогов: {number(out['totals']['active_catalog_n'])}; бизнесов с заказами через чекаут: {number(out['totals']['selling_n'])}.",
       f"- Сумма W: {number(out['totals']['orders'])} заказов; сумма X: {number(out['totals']['gmv_rub'])} ₽ GMV; сумма Z: {number(out['totals']['ads_rub'])} ₽ рекламных расходов.",'',
       '## 2. Распределения всех 28 полей','',
       'В таблице N/A — пустая ячейка. Средние, квантили, полный набор частот (кроме уникального ID), положительные квантили, топ-10 и концентрация аддитивных показателей сохранены в JSON. Суммы цен, среднего чека, идентификаторов и бинарных флагов не трактуются как бизнес-агрегаты.','',
       '| Поле | Название | Пусто | Нулей | Уникальных непустых | Минимум | Медиана | P90 | P99 | Максимум |',
       '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
for c in letters:
    d=out['distributions'][c]
    if c in NUMERIC:
        lines.append(f"| {c} | {COLS[c]} | {number(d['missing_n'])} | {number(d['zero_n'])} | {number(d['distinct_nonmissing'])} | {number(d['min'])} | {number(d['quantiles']['0.5'])} | {number(d['quantiles']['0.9'])} | {number(d['quantiles']['0.99'])} | {number(d['max'])} |")
    else:
        lines.append(f"| {c} | {COLS[c]} | {number(d['missing_n'])} | — | {number(d['distinct_nonmissing'])} | — | — | — | — | — |")
lines+=['','### Бинарные поля','', '| Флаг | 0 | 1 | Пусто |','|---|---:|---:|---:|']
for c in BINARY:
    freq=out['binary_value_counts'][c]
    lines.append(f"| {c}: {COLS[c]} | {number(freq.get('0',0))} | {number(freq.get('1',0))} | {number(freq.get('<MISSING>',0))} |")
lines+=['','### Частоты всех тематик и типов','', '| Поле | Значение | Строк | Доля |','|---|---|---:|---:|']
for c in ['B','C']:
    for v in sorted(out['distributions'][c]['value_counts'],key=lambda r:r['n'],reverse=True):
        lines.append(f"| {c} | {v['value']} | {number(v['n'])} | {number(pct(v['n']))}% |")
lines+=['','## 3. Пропуски: структурные и содержательные','',
       '- U: 2 570 неизвестных модельных оценок. Это отдельное состояние, а не доказанное отсутствие оригинальности.',
       '- Y: 9 463 пропуска. Они распределяются по отсутствию заказов и положительным заказам с нулевым GMV так, как показано ниже. Не следует заменять все пропуски средним чеком 0.',
       '- AB: 5 412 пропусков. При активном каталоге цена отсутствует у 120 бизнесов. Значения при отсутствии активных товаров могут относиться к загруженному/историческому ассортименту; нужен словарь определения поля.','',
       '### Средний чек Y по наличию заказов и GMV','', '| W>0 | X>0 | Строк | Пустой Y |','|---|---|---:|---:|']
for g in out['missingness_analysis']['Y']['by_orders_gmv']:
    lines.append(f"| {g['orders_positive']} | {g['gmv_positive']} | {number(g['n'])} | {number(g['missing_y_n'])} |")
lines+=['','### Цена AB по состоянию товара','', '| P>0 | Q>0 | Строк | Пустая AB | Нулевая AB |','|---|---|---:|---:|---:|']
for g in out['missingness_analysis']['AB']['by_uploaded_active']:
    lines.append(f"| {g['uploaded_positive']} | {g['active_positive']} | {number(g['n'])} | {number(g['missing_price_n'])} | {number(g['zero_price_n'])} |")
lines+=['','## 4. Тип бизнеса и фактические активные товары/услуги','',
       'Тип C описывает роль, а Q/AA — наблюдаемый активный ассортимент. Их нельзя автоматически считать одним и тем же признаком. «Без каталога» с активными товарами — явное расхождение названия и состояния; «услужное» без загруженных услуг не доказывает отсутствие услуг у бизнеса.','',
       '| Тип | Q>0 | AA>0 | Строк | С заказами W>0 |','|---|---|---|---:|---:|']
for g in out['type_vs_actual_setup']:
    lines.append(f"| {g['business_type']} | {g['active_goods']} | {g['active_services']} | {number(g['n'])} | {number(g['selling_n'])} |")
lines+=['',f"Одинаковых профилей без ID: {len(identical_profiles)} групп, {sum(g['n'] for g in identical_profiles)} строки. У всех разные уникальные ID. Разреженные округлённые синтетические профили могут совпадать; оснований автоматически удалять эти строки нет.",'']
lines+=['','## 5. Проверки согласованности','',
       'Ниже включены все смысловые проверки, в том числе с нулевым числом нарушений. Полные списки строк/ID и примеры с исходными значениями — в `checks` JSON. Диагностическое расхождение не объявляется ошибкой без проверки определения счётчиков.','',
       '| Проверка | Правило | Строк | Классификация |','|---|---|---:|---|']
for k,v in checks.items():
    if k.startswith('negative_') or k.startswith('invalid_binary_') or k.startswith('outer_whitespace_'):continue
    lines.append(f"| {k} | {v['rule']} | {number(v['n'])} | {v['classification']} |")
lines+=['','## 6. Проверка формулы среднего чека','',
       'PDF, с. 34: средний чек = GMV / число заказов. Для бизнеса это Y = X/W на одинаковом множестве заказов.','',
       f"- Сравнение возможно для {number(aov['denominator_nonmissing_with_orders'])} строк с W>0 и заполненным Y.",
       f"- Точное равенство: {number(aov['exact_equal_n'])}; отклонение не больше 0,5 ₽: {number(aov['abs_diff_le_half_ruble_n'])}; не больше 1 ₽: {number(aov['abs_diff_le_one_ruble_n'])}.",
       f"- Отклонение больше 1 ₽: {number(aov['abs_diff_gt_one_ruble_n'])}. Это нельзя объяснить только округлением до рубля.",
       f"- Абсолютное относительное отклонение >1%: {number(aov['abs_relative_error_gt_1pct_n'])}; >5%: {number(aov['abs_relative_error_gt_5pct_n'])}; >10%: {number(aov['abs_relative_error_gt_10pct_n'])}; >20%: {number(aov['abs_relative_error_gt_20pct_n'])}.",
       f"- Совокупный средний чек по всем W/X: {number(aov['all_business_gmv_over_orders'])} ₽. Он включает 7 строк с заказами и нулевым GMV.",
       '- Источник Y сохраняется. Для анализа вычисляется отдельный средний чек из согласованно отобранных X и W; нельзя молча править исходник или усреднять Y как средний чек всей выборки.','',
       '### Крупнейшие относительные расхождения','', '| Строка Excel | ID | W | X | Y | X/W | Отклонение Y/(X/W)-1 |','|---:|---:|---:|---:|---:|---:|---:|']
for r in out['average_ticket_identity']['largest_relative_errors']:
    lines.append(f"| {r['excel_row']} | {r['id']} | {number(r['W'])} | {number(r['X'])} | {number(r['Y'])} | {number(r['aov_recomputed'])} | {number(r['aov_relative_error']*100)}% |")
lines+=['','## 7. Независимое восстановление прежних групп','',
       'Все группы восстановлены по текущему исходнику, без чтения прежних расчётов. Нулевая W означает отсутствие наблюдаемых checkout-заказов за окно, не отсутствие всех продаж и не отсутствие продаж за всю историю.','',
       '| Группа | Точный фильтр | Прежнее число | Пересчитано | Совпало |','|---|---|---:|---:|---|']
for k,g in out['reconstructed_groups'].items():
    if k=='204_partition':continue
    lines.append(f"| {k} | {g['rule']} | {number(g['previous_count'])} | {number(g['recomputed_count'])} | {g['matches_previous']} |")
part=groups['204_partition']
lines+=['',f"Разбиение группы 204: нулевых L — {part['zero_product_views_n']}; O=0 — {part['zero_opens_n']}; O=1…9 — {part['opens_1_9_n']}; O=10…99 — {part['opens_10_99_n']}; O≥100 — {part['opens_100plus_n']}. 55+108=163 имеют меньше 10 открытий именно из контента.",'',
       '## 8. Концентрация и крупнейшие значения','',
       'Ниже доля суммы показателя, которую дают 1, 10 и 100 строк. Это не доля уникальных людей. Высокое значение — кандидат для проверки, не автоматическая ошибка.','',
       '| Поле | Сумма | Топ-1 | Топ-10 | Топ-100 |','|---|---:|---:|---:|---:|']
for c in ADDITIVE:
    d=out['distributions'][c]; conc=d['concentration']
    fmt=lambda k: number(100*conc[k]['share_of_total'])+'%' if conc[k]['share_of_total'] is not None else '—'
    lines.append(f"| {c}: {COLS[c]} | {number(d['sum'])} | {fmt('top_1')} | {fmt('top_10')} | {fmt('top_100')} |")
lines+=['','### Максимумы количественных полей','', '| Поле | Строка | ID | Тематика | Максимум |','|---|---:|---:|---|---:|']
for c in NUMERIC:
    if c=='A' or c in BINARY:continue
    r=out['distributions'][c]['top_10'][0]
    lines.append(f"| {c}: {COLS[c]} | {r['excel_row']} | {r['id']} | {r['B']} | {number(r[c])} |")
lines+=['','## 9. Ограничения вывода и правила использования','']
limits_ru=[
 'Нет описания отбора и весов бизнес-выборки. 9 754 строки нельзя пропорционально переносить на все бизнес-сообщества VK.',
 'Нет дат регистрации, подключения инструмента, первой продажи, истории до/после и рандомизации. Срез не доказывает причинный эффект контента, оплаты, доставки или каталога.',
 'Нет полной картины продаж через сообщения, партнёра, внешние сайты и офлайн. W/X охватывают чекаут; отсутствие W не означает отсутствия бизнеса или спроса.',
 'O — открытия карточки из контента. Это не все открытия карточки и не все клиенты. W/O не является сквозной конверсией конкретного товарного контента.',
 'Нет ключей «публикация — товар — продавец — заказ». Реферальный контент может продвигать чужое предложение, а W относится к собственному чекауту.',
 'Оценка U модельная и неполная. Авторы, рефералы и собственные продавцы — пересекающиеся роли, а не автоматически взаимоисключающие типы.',
 'Для сравнения распространения обычного и товарного контента нет одинакового возраста публикаций, источника просмотров, охвата и однозначных определений E/F относительно G/H. Делить просмотры на новые посты как меру эффективности рекомендаций нельзя.',
 'Дата и смысл состояния R/S/T не раскрыты. Расхождения с событиями W требуют уточнения, но не доказывают отсутствие пользы инструментов.',
 'Нельзя заменять неизвестную U на 0 или отсутствующую AB на бесплатную цену. Структурный пропуск Y при W=0 отличается от пропуска при W>0.',
 'Большинство заполненных Y не выполняет объявленную формулу. Расчёт среднего чека должен явно использовать X/W на согласованных строках с сохранением оригинала.',
 'GMV не равен выручке или прибыли. Z — все рекламные траты, X — только checkout-GMV. Полный ROAS, CAC, LTV и прибыль из этих полей не идентифицируются.',
 'Счётчики событий не равны уникальным пользователям. Сумма лайков и репостов не является числом уникальных вовлечённых людей.',
 'Распределения сильно сосредоточены у нуля и в верхнем хвосте. Средние надо дополнять медианами, долями активных бизнесов и чувствительностью к крупнейшим строкам.',
 'Данные синтетические. Зафиксированные несогласования сохраняются и оговариваются; исходник не очищается молча.'
]
lines += [f'- {s}' for s in limits_ru]
lines += ['','## 10. Воспроизводимость','',
          'Скрипт `audit_analysis.py` читает исходную книгу и создаёт `audit_results.json` и этот отчёт. В JSON находятся частоты всех полей, точные проверки, полные списки затронутых ID/строк, все 291 сопоставления среднего чека, контрольные группы и распределения. Исходная книга не перезаписывается.','']
(OUTPUT/'audit_findings.md').write_text('\n'.join(lines),encoding='utf8')
print(json.dumps({'totals':out['totals'],'critical_counts':{k:checks[k]['n'] for k in ['duplicate_id','duplicate_complete_row','duplicate_except_id','orders_without_checkout','orders_zero_gmv','aov_identity_error_gt_1rub','median_price_missing_with_active_goods','product_posts_above_posts','ordinary_likes_above_views','ordinary_reposts_above_views','product_likes_above_views','product_reposts_above_views','content_opens_without_product_views','content_opens_above_product_views']},'groups':{k:g['recomputed_count'] for k,g in groups.items() if 'recomputed_count' in g},'outputs':['audit_analysis.py','audit_results.json','audit_findings.md']},ensure_ascii=False,indent=2))
