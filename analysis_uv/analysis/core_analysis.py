from pathlib import Path
import json, math, hashlib, warnings
import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.compose import ColumnTransformer
from sklearn.preprocessing import OneHotEncoder, StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold, cross_val_predict
from sklearn.metrics import roc_auc_score, average_precision_score, brier_score_loss

BASE = Path(__file__).resolve().parents[1]
OUT = BASE / 'analysis'
SOURCE = next((BASE / 'source').glob('*.xlsx'))
raw = pd.read_excel(SOURCE, sheet_name='Бизнесы')
names = ['id','theme','business_type','subscribers','posts','clips','product_posts','product_clips',
         'views','likes','reposts','product_views','product_likes','product_reposts','content_opens',
         'uploaded_products','active_products','checkout','delivery','payment','original_author','referral',
         'orders','gmv','reported_aov','ad_spend','active_services','median_price']
d = raw.copy(); d.columns = names
d['has_goods'] = d.active_products.gt(0)
d['has_services'] = d.active_services.gt(0)
d['seller'] = d.orders.gt(0)
d['pubs'] = d.posts + d.clips
d['product_pubs'] = d.product_posts + d.product_clips
d['has_product_pubs'] = d.product_pubs.gt(0)
d['has_pubs'] = d.pubs.gt(0)
d['any_pubs'] = (d.pubs + d.product_pubs).gt(0)
d['ad_active'] = d.ad_spend.gt(0)
d['has_opens'] = d.content_opens.gt(0)
d['no_referral'] = d.referral.eq(0)
d['actual_aov'] = np.where(d.orders.gt(0), d.gmv / d.orders, np.nan)
d['product_open_view_ratio'] = np.where(d.product_views.gt(0), d.content_opens / d.product_views, np.nan)
d['subscriber_band'] = pd.cut(d.subscribers, [-1,100,1000,5000,20000,100000,np.inf], labels=['0–100','101–1 000','1 001–5 000','5 001–20 000','20 001–100 000','100 001+']).astype(str)

def clean(o):
    if isinstance(o, dict): return {str(k):clean(v) for k,v in o.items()}
    if isinstance(o, (list,tuple,np.ndarray)): return [clean(v) for v in o]
    if isinstance(o, (np.integer,)): return int(o)
    if isinstance(o, (np.floating,float)): return float(o) if math.isfinite(o) else None
    if isinstance(o, (np.bool_,)): return bool(o)
    if isinstance(o, (pd.Timestamp,)): return o.isoformat()
    if pd.isna(o): return None
    return o

def summarize(x):
    n=len(x); s=x.seller.sum()
    return dict(n=n, sellers=int(s), seller_rate=float(s/n) if n else None,
                orders=int(x.orders.sum()), gmv=float(x.gmv.sum()), ad_spend=float(x.ad_spend.sum()),
                content_opens=int(x.content_opens.sum()), product_views=int(x.product_views.sum()),
                median_subscribers=x.subscribers.median(), median_active_products=x.active_products.median(),
                median_pubs=x.pubs.median(), median_content_opens=x.content_opens.median(),
                median_product_views=x.product_views.median(), median_ad_spend=x.ad_spend.median(),
                ad_users=int(x.ad_active.sum()), product_publishers=int(x.has_product_pubs.sum()),
                checkout_users=int(x.checkout.eq(1).sum()), delivery_users=int(x.delivery.eq(1).sum()),
                payment_users=int(x.payment.eq(1).sum()), referral_users=int(x.referral.eq(1).sum()),
                median_orders_sellers=x.loc[x.seller,'orders'].median(),
                median_gmv_sellers=x.loc[x.seller,'gmv'].median(),
                weighted_aov=float(x.gmv.sum()/x.orders.sum()) if x.orders.sum()>0 else None)

m = {
 'all': pd.Series(True,index=d.index),
 'goods': d.has_goods,
 'no_goods': ~d.has_goods,
 'goods_no_services': d.has_goods & ~d.has_services,
 'goods_and_services': d.has_goods & d.has_services,
 'services_only': ~d.has_goods & d.has_services,
 'no_goods_no_services': ~d.has_goods & ~d.has_services,
 'goods_no_referral': d.has_goods & d.no_referral,
 'goods_only_no_referral': d.has_goods & ~d.has_services & d.no_referral,
 'goods_referral': d.has_goods & ~d.no_referral,
 'goods_sellers': d.has_goods & d.seller,
 'goods_no_orders': d.has_goods & ~d.seller,
 'goods_product_pubs': d.has_goods & d.has_product_pubs,
 'goods_product_pubs_no_orders': d.has_goods & d.has_product_pubs & ~d.seller,
 'goods_no_product_pubs_orders': d.has_goods & ~d.has_product_pubs & d.seller,
 'goods_sellers_zero_content_opens': d.has_goods & d.seller & ~d.has_opens,
 'goods_no_orders_zero_content_opens': d.has_goods & ~d.seller & ~d.has_opens,
 'goods_pubs_no_product_no_orders': d.has_goods & d.has_pubs & ~d.has_product_pubs & ~d.seller,
 'strict_1817': d.has_goods & ~d.seller & ~d.has_product_pubs & d.has_pubs & ~d.has_services & d.no_referral,
 'strict_checkout': d.has_goods & ~d.seller & ~d.has_product_pubs & d.has_pubs & ~d.has_services & d.no_referral & d.checkout.eq(1),
 'goods_zero_pubs': d.has_goods & ~d.any_pubs,
 'goods_ad': d.has_goods & d.ad_active,
 'goods_ad_no_orders': d.has_goods & d.ad_active & ~d.seller,
 'goods_no_ad_orders': d.has_goods & ~d.ad_active & d.seller,
 'referral_no_goods': ~d.has_goods & ~d.no_referral,
 'no_goods_orders': ~d.has_goods & d.seller,
}
segments = {k:summarize(d[v]) for k,v in m.items()}

profiles = {}
for field in ['business_type','theme','subscriber_band']:
    profiles[field]={scope:[dict(group=str(k),**summarize(g)) for k,g in d[mask].groupby(field,sort=False,observed=True)]
                     for scope,mask in [('all',m['all']),('goods',m['goods']),('goods_no_referral',m['goods_no_referral'])]}

binary=[]
for scope,mask in [('all',m['all']),('goods',m['goods']),('goods_no_referral',m['goods_no_referral']),('goods_only_no_referral',m['goods_only_no_referral'])]:
 for field in ['checkout','delivery','payment','original_author','referral','has_product_pubs','has_pubs','ad_active','has_opens']:
  for val in [0,1]:
   binary.append(dict(scope=scope,feature=field,value=val,**summarize(d[mask & d[field].eq(val)])))
  if d.loc[mask,field].isna().any():
   binary.append(dict(scope=scope,feature=field,value='unknown',**summarize(d[mask & d[field].isna()])))

combos=[]
for keys,g in d[d.has_goods].groupby(['checkout','delivery','payment'],sort=True):
 combos.append(dict(checkout=keys[0],delivery=keys[1],payment=keys[2],**summarize(g)))

states=[]
for keys,g in d.groupby(['has_goods','has_product_pubs','seller'],sort=True):
 states.append(dict(goods=keys[0],product_pubs=keys[1],seller=keys[2],**summarize(g)))

bin_specs={
 'subscribers':[-1,100,1000,5000,20000,100000,np.inf],
 'active_products':[-1,0,10,50,200,1000,np.inf],
 'pubs':[-1,0,1,5,20,100,np.inf],
 'product_pubs':[-1,0,1,3,10,np.inf],
 'views':[-1,100,1000,10000,100000,np.inf],
 'product_views':[-1,0,100,1000,10000,100000,np.inf],
 'content_opens':[-1,0,9,99,999,np.inf],
 'ad_spend':[-1,0,10000,50000,200000,np.inf],
 'median_price':[-1,1000,3000,10000,30000,np.inf],
 'active_services':[-1,0,5,20,100,np.inf],
}
binned=[]
for scope,mask in [('all',m['all']),('goods',m['goods']),('goods_no_referral',m['goods_no_referral'])]:
 x=d[mask].copy()
 for field,edges in bin_specs.items():
  bands=pd.cut(x[field],edges,include_lowest=True)
  for label,g in x.groupby(bands,observed=False):
   binned.append(dict(scope=scope,feature=field,band=str(label),**summarize(g)))
  if x[field].isna().any():
   binned.append(dict(scope=scope,feature=field,band='Нет значения',**summarize(x[x[field].isna()])))

concentration=[]
for scope,mask in [('all',m['all']),('goods',m['goods']),('goods_no_referral',m['goods_no_referral'])]:
 for field in ['orders','gmv','ad_spend','subscribers','views','product_views','content_opens']:
  x=d[mask].sort_values(field,ascending=False); total=float(x[field].sum()); n=len(x)
  vals=np.sort(x[field].to_numpy(dtype=float)); gini=(2*np.sum(np.arange(1,n+1)*vals)/(n*vals.sum())-(n+1)/n) if vals.sum()>0 else None
  for kind,k in [('top1',1),('top5',5),('top10',10),('top1pct',math.ceil(n*.01)),('top5pct',math.ceil(n*.05)),('top10pct',math.ceil(n*.1))]:
   concentration.append(dict(scope=scope,metric=field,cut=kind,count=k,total=total,value=float(x[field].iloc[:k].sum()),share=float(x[field].iloc[:k].sum()/total) if total>0 else None,gini=gini))

stratified=[]
for scope,mask in [('goods',m['goods']),('goods_no_referral',m['goods_no_referral'])]:
 for strat in ['subscriber_band','business_type','delivery','ad_active','original_author']:
  for key,g in d[mask].groupby(strat,observed=True,dropna=False):
   for value in [False,True]:
    stratified.append(dict(scope=scope,stratifier=strat,stratum='Неизвестно' if pd.isna(key) else str(key),product_pubs=value,**summarize(g[g.has_product_pubs.eq(value)])))

num=[c for c in names if c not in ['id','theme','business_type']]+['pubs','product_pubs','seller']
correlations={}
for scope,mask in [('all',m['all']),('goods',m['goods']),('goods_no_referral',m['goods_no_referral'])]:
 x=d[mask]; cm=x[num].corr(method='spearman')
 pairs=[]
 for i,a in enumerate(num):
  for b in num[i+1:]:
   valid=x[[a,b]].dropna()
   pairs.append(dict(a=a,b=b,rho=cm.loc[a,b],n=len(valid)))
 correlations[scope]={'columns':num,'matrix':cm.to_numpy().tolist(),'pairs':sorted(pairs,key=lambda r:abs(r['rho']) if pd.notna(r['rho']) else -1,reverse=True)}

models={}
log_cols=['subscribers','active_products','pubs','product_pubs','views','product_views','content_opens','ad_spend','active_services','median_price']
flag_cols=['checkout','delivery','payment','original_author','referral']
for scope,mask in [('goods',m['goods']),('goods_no_referral',m['goods_no_referral'])]:
 x=d[mask].copy()
 for c in log_cols: x[c]=np.log1p(x[c])
 x['price_missing']=x.median_price.isna().astype(int)
 x['author_missing']=x.original_author.isna().astype(int)
 y=x.seller.astype(int)
 for version,numeric in [('context',['subscribers','active_products','ad_spend','active_services','median_price','price_missing','author_missing']+flag_cols),('with_content',log_cols+['price_missing','author_missing']+flag_cols)]:
  cat=['business_type','theme']
  prep=ColumnTransformer([('num',Pipeline([('impute',SimpleImputer(strategy='median')),('scale',StandardScaler())]),numeric),('cat',OneHotEncoder(handle_unknown='ignore',sparse_output=True),cat)])
  pipe=Pipeline([('prep',prep),('model',LogisticRegression(C=1,max_iter=3000,solver='lbfgs',random_state=42))])
  cv=StratifiedKFold(n_splits=5,shuffle=True,random_state=42)
  p=cross_val_predict(pipe,x[numeric+cat],y,cv=cv,method='predict_proba',n_jobs=1)[:,1]
  pipe.fit(x[numeric+cat],y)
  fn=pipe.named_steps['prep'].get_feature_names_out(); co=pipe.named_steps['model'].coef_[0]
  coeff=sorted([dict(feature=f,coefficient=float(c),exp_coefficient=float(np.exp(c)),unit='one standard deviation after log1p where applicable' if f.startswith('num__') else 'one encoded category unit') for f,c in zip(fn,co)],key=lambda a:abs(a['coefficient']),reverse=True)
  dec=pd.DataFrame({'actual':y.to_numpy(),'predicted':p}); dec['band']=pd.qcut(dec.predicted,10,duplicates='drop')
  cal=dec.groupby('band',observed=True).agg(n=('actual','size'),actual=('actual','mean'),predicted=('predicted','mean')).reset_index(); cal['band']=cal['band'].astype(str)
  models[scope+'_'+version]=dict(n=len(x),positives=int(y.sum()),prevalence=float(y.mean()),roc_auc=roc_auc_score(y,p),average_precision=average_precision_score(y,p),brier=brier_score_loss(y,p),baseline_brier=float(y.mean()*(1-y.mean())),coefficients=coeff,calibration=cal.to_dict('records'))

sensitivity=[]
for scope,mask in [('goods',m['goods']),('goods_no_referral',m['goods_no_referral'])]:
 x=d[mask]
 for variant,keep in [('all',pd.Series(True,index=x.index)),('remove_top1pct_subscribers',x.subscribers.le(x.subscribers.quantile(.99))),('remove_top1pct_gmv',x.gmv.le(x.gmv.quantile(.99))),('checkout_on',x.checkout.eq(1)),('goods_no_services',~x.has_services)]:
  for pub in [False,True]:
   sensitivity.append(dict(scope=scope,variant=variant,product_pubs=pub,**summarize(x[keep & x.has_product_pubs.eq(pub)])))

opportunity=[]
for theme,g in d[m['strict_1817']].groupby('theme'):
 opportunity.append(dict(theme=theme,**summarize(g),share_subscribers_ge1000=float(g.subscribers.ge(1000).mean()),share_views_ge1000=float(g.views.ge(1000).mean())))
opportunity.sort(key=lambda r:r['n'],reverse=True)

outputs=dict(metadata=dict(rows=len(d),columns=len(raw.columns),sha256=hashlib.sha256(SOURCE.read_bytes()).hexdigest(),source=str(SOURCE.name),date='2026-11-19',notes=['Описательный анализ синтетического среза. Ассоциации не доказывают причинность.','Сложная статистика воспроизводится скриптом, не является прогнозом рынка.']),segments=segments,profiles=profiles,binary=binary,tool_combos=combos,states=states,binned=binned,concentration=concentration,stratified_content=stratified,correlations=correlations,models=models,sensitivity=sensitivity,strict_theme_opportunity=opportunity)
(OUT/'core_results.json').write_text(json.dumps(clean(outputs),ensure_ascii=False,indent=2),encoding='utf-8')
d.to_csv(OUT/'business_prepared.csv',index=False)
xl=pd.ExcelFile(SOURCE)
source_sheets=[]
for sheet in xl.sheet_names:
 s=pd.read_excel(xl,sheet_name=sheet)
 source_sheets.append(dict(name=sheet,headers=list(s.columns),rows=clean(s.values.tolist())))
(OUT/'raw_sheets.json').write_text(json.dumps(source_sheets,ensure_ascii=False),encoding='utf-8')
print(json.dumps(clean({'segments':segments,'models':{k:{j:v for j,v in z.items() if j not in ['coefficients','calibration']} for k,z in models.items()},'top_strict_themes':opportunity[:8]}),ensure_ascii=False,indent=2))
