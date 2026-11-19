from pathlib import Path
import json, math
import numpy as np
import pandas as pd
BASE=Path(__file__).resolve().parents[1]
d=pd.read_csv(BASE/'analysis/business_prepared.csv')

def clean(o):
 if isinstance(o,dict):return {str(k):clean(v) for k,v in o.items()}
 if isinstance(o,(list,tuple,np.ndarray)):return [clean(v) for v in o]
 if isinstance(o,(np.integer,)):return int(o)
 if isinstance(o,(np.floating,float)):return float(o) if math.isfinite(o) else None
 if isinstance(o,np.bool_):return bool(o)
 if pd.isna(o):return None
 return o
def s(x):
 return dict(n=len(x),sellers=int(x.seller.sum()),seller_rate=float(x.seller.mean()) if len(x) else None,orders=int(x.orders.sum()),gmv=float(x.gmv.sum()),subscribers_median=x.subscribers.median(),views_median=x.views.median(),content_opens=int(x.content_opens.sum()),product_views=int(x.product_views.sum()),ad_spend=float(x.ad_spend.sum()))

strict=d.has_goods & ~d.seller & ~d.has_product_pubs & d.has_pubs & ~d.has_services & d.no_referral
rules=[('Строгая группа 1817',strict),('Из неё просмотры обычного контента ≥1000',strict & d.views.ge(1000)),('Из неё подписчики ≥1000',strict & d.subscribers.ge(1000)),('Из неё оба порога ≥1000',strict & d.views.ge(1000) & d.subscribers.ge(1000)),('Оба порога ≥1000 и ≥10 товаров',strict & d.views.ge(1000) & d.subscribers.ge(1000) & d.active_products.ge(10)),('Оба порога ≥1000 и ≥10 товаров и чекаут',strict & d.views.ge(1000) & d.subscribers.ge(1000) & d.active_products.ge(10) & d.checkout.eq(1))]
readiness=[dict(rule=n,**s(d[m])) for n,m in rules]
for threshold in [100,500,1000,5000,10000]:
 readiness.append(dict(rule=f'Строгая группа: подписчики и обычные просмотры ≥{threshold}',**s(d[strict & d.views.ge(threshold) & d.subscribers.ge(threshold)])))

non_order_interest=[]
for gname,mask in [('Все каталоги без заказов',d.has_goods & ~d.seller),('Каталоги с новыми товарными публикациями без заказов',d.has_goods & d.has_product_pubs & ~d.seller),('Каталоги без реферальной программы без заказов',d.has_goods & d.no_referral & ~d.seller)]:
 for low,high in [(0,0),(1,9),(10,99),(100,999),(1000,np.inf)]:
  non_order_interest.append(dict(group=gname,band=f'{low}–{high}',**s(d[mask & d.content_opens.between(low,high)])))

formats=[]
d['format']=np.select([d.product_posts.gt(0)&d.product_clips.gt(0),d.product_posts.gt(0),d.product_clips.gt(0)],['Посты и клипы','Только посты','Только клипы'],default='Нет новых товарных публикаций')
for scope,mask in [('all',pd.Series(True,index=d.index)),('goods',d.has_goods),('goods_no_referral',d.has_goods & d.no_referral)]:
 for label,x in d[mask].groupby('format'):
  formats.append(dict(scope=scope,format=label,**s(x)))

standardized=[]
strata_rows=[]
for scope,mask in [('goods',d.has_goods),('goods_no_referral',d.has_goods & d.no_referral)]:
 x=d[mask].copy()
 for stratifier in ['subscribers','active_products']:
  x['stratum']=pd.qcut(x[stratifier],5,duplicates='drop').astype(str)
  for feature in ['has_product_pubs','delivery','payment','ad_active','has_opens']:
   valid=[]
   for st,g in x.groupby('stratum'):
    n0=int(g[feature].eq(0).sum());n1=int(g[feature].eq(1).sum())
    a=int(g.loc[g[feature].eq(1),'seller'].sum());c=int(g.loc[g[feature].eq(0),'seller'].sum())
    rr=dict(scope=scope,stratifier=stratifier,stratum=st,feature=feature,n0=n0,n1=n1,sellers0=c,sellers1=a,rate0=c/n0 if n0 else None,rate1=a/n1 if n1 else None)
    strata_rows.append(rr)
    if n0 and n1:valid.append(rr)
   total=sum(z['n0']+z['n1'] for z in valid)
   r0=sum(z['rate0']*(z['n0']+z['n1'])/total for z in valid) if total else None
   r1=sum(z['rate1']*(z['n0']+z['n1'])/total for z in valid) if total else None
   standardized.append(dict(scope=scope,stratifier=stratifier,feature=feature,covered_n=total,strata=len(valid),standardized_rate0=r0,standardized_rate1=r1,standardized_ratio=r1/r0 if r0 else None,raw_rate0=float(x.loc[x[feature].eq(0),'seller'].mean()),raw_rate1=float(x.loc[x[feature].eq(1),'seller'].mean())))

top_records={}
for metric in ['orders','gmv','ad_spend','content_opens','product_views','subscribers']:
 top_records[metric]=d.sort_values(metric,ascending=False)[['id','theme','business_type','subscribers','active_products','active_services','product_pubs','content_opens','orders','gmv','ad_spend','referral']].head(20).to_dict('records')

prices=[]
for theme,x in d[d.has_goods].groupby('theme'):
 if len(x)>=30:
  prices.append(dict(theme=theme,n=len(x),sellers=int(x.seller.sum()),price_median=x.median_price.median(),aov_aggregate=x.gmv.sum()/x.orders.sum() if x.orders.sum() else None,price_missing=int(x.median_price.isna().sum())))

result=dict(readiness=readiness,no_order_interest=non_order_interest,content_formats=formats,standardized=standardized,strata_rows=strata_rows,top_records=top_records,theme_prices=prices)
(BASE/'analysis/extended_results.json').write_text(json.dumps(clean(result),ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(clean({k:result[k] for k in ['readiness','no_order_interest','content_formats','standardized']}),ensure_ascii=False,indent=2))
