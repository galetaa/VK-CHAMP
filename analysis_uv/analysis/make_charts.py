from pathlib import Path
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter, PercentFormatter

BASE=Path(__file__).resolve().parents[1]; OUT=BASE/'charts';OUT.mkdir(exist_ok=True)
f=next((BASE/'source').glob('*.xlsx'))
d=pd.read_csv(BASE/'analysis/business_prepared.csv')
core=json.loads((BASE/'analysis/core_results.json').read_text(encoding='utf-8'))
ext=json.loads((BASE/'analysis/extended_results.json').read_text(encoding='utf-8'))
blue='#1765C1'; navy='#17324F'; orange='#DD7D29'; gray='#8797A8'; pale='#EAF0F6'
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.titlesize':16,'axes.titleweight':'bold','axes.labelcolor':navy,'text.color':navy,'xtick.color':navy,'ytick.color':navy,'axes.spines.top':False,'axes.spines.right':False,'axes.edgecolor':'#C4CFD9','grid.color':'#E5EBF0','figure.facecolor':'white','axes.facecolor':'white','savefig.facecolor':'white'})
def save(fig,name,note):
 fig.text(.02,.018,note,fontsize=8.5,color='#526474',va='bottom')
 fig.tight_layout(rect=[0,.16 if name in ['03_tools_associations.png','11_standardization.png'] else .07,1,.99]);fig.savefig(OUT/name,dpi=165,bbox_inches='tight');plt.close(fig)
def fmt(x):return f'{x:,.0f}'.replace(',',' ')
def pct_text(x,decimals=2):return f'{100*x:.{decimals}f}'.replace('.',',')+'%'

goods_summary=core['segments']['goods']
goods_n=goods_summary['n']

fig,ax=plt.subplots(figsize=(10.8,5.6))
vals=[goods_summary[key] for key in ['n','checkout_users','product_publishers','sellers']];labels=['Есть активные товары','Включено оформление заказа','Есть новые товарные публикации','Есть внутренние заказы']
bars=ax.barh(labels[::-1],vals[::-1],color=[orange,blue,gray,navy]);ax.set_xlim(0,4500)
for bar,val in zip(bars,vals[::-1]):ax.text(val+55,bar.get_y()+bar.get_height()/2,fmt(val),va='center',fontweight='bold')
ax.set_title(f'Использование активных каталогов: {fmt(goods_n)}',loc='left',pad=18);ax.set_xlabel('Число бизнес-сообществ');ax.grid(axis='x');ax.set_axisbelow(True)
save(fig,'01_catalog_state.png','Лист «Бизнесы». Признаки пересекаются; это не последовательная воронка. Все числа среди активных каталогов.')

fig,ax=plt.subplots(figsize=(10.8,5.8))
x=core['profiles']['subscriber_band']['goods'];order=['0–100','101–1 000','1 001–5 000','5 001–20 000','20 001–100 000','100 001+'];x=sorted(x,key=lambda z:order.index(z['group']))
bars=ax.bar(range(len(x)),[z['seller_rate']*100 for z in x],color=[blue]*5+[gray])
for bar,z in zip(bars,x):ax.text(bar.get_x()+bar.get_width()/2,bar.get_height()+1.5,f"{z['seller_rate']*100:.1f}%\nn={z['n']}",ha='center',fontsize=10)
ax.set_xticks(range(len(x)),[z['group'] for z in x]);ax.set_ylim(0,86);ax.set_ylabel('Доля с внутренними заказами, %');ax.set_xlabel('Подписчики сообщества');ax.set_title('Заказы чаще встречаются у более крупных сообществ',loc='left',pad=18);ax.grid(axis='y');ax.set_axisbelow(True)
save(fig,'02_subscribers_sales.png',f"Активные каталоги. Наблюдаемая связь не доказывает эффект роста подписчиков. Последняя группа: n={fmt(x[-1]['n'])}.")

fig,ax=plt.subplots(figsize=(10.8,6))
features=[('has_product_pubs','Товарные публикации'),('delivery','Доставка'),('payment','Оплата'),('ad_active','Расходы на рекламу'),('has_opens','Открытия из контента')]
y=np.arange(len(features));h=.34
for v,color,delta,label in [(0,gray,-h/2,'Признак отсутствует'),(1,blue,h/2,'Признак есть')]:
 rows=[next(z for z in core['binary'] if z['scope']=='goods' and z['feature']==ff and z['value']==v) for ff,_ in features]
 bars=ax.barh(y+delta,[z['seller_rate']*100 for z in rows],h,color=color,label=label)
 for bar,z in zip(bars,rows):ax.text(bar.get_width()+.25,bar.get_y()+h/2,f"{z['seller_rate']*100:.1f}% ({z['sellers']}/{z['n']})",va='center',fontsize=9)
ax.set_yticks(y,[z[1] for z in features]);ax.invert_yaxis();ax.set_xlim(0,29);fig.legend(loc='lower center',bbox_to_anchor=(.5,.065),ncol=2,frameon=False,fontsize=10);ax.set_xlabel('Доля бизнесов с внутренними заказами, %');ax.set_title('Связи инструментов с наличием заказов',loc='left',pad=18);ax.grid(axis='x');ax.set_axisbelow(True)
save(fig,'03_tools_associations.png','Только активные каталоги. Сравнения не учитывают все различия бизнесов и не являются эффектами подключения.')

fig,ax=plt.subplots(figsize=(10.8,5.4))
x=core['states'];arr=np.array([[sum(z['n'] for z in x if z['goods'] and z['product_pubs']==pub and z['seller']==seller) for seller in (True,False)] for pub in (False,True)])
ax.imshow(arr,cmap='Blues',aspect='auto',vmin=0,vmax=4000)
for i in range(2):
 for j in range(2):ax.text(j,i,f'{arr[i,j]:,}'.replace(',',' '),ha='center',va='center',fontsize=25,color='white' if arr[i,j]>2000 else navy,fontweight='bold')
ax.set_xticks([0,1],['Есть внутренние заказы','Нет внутренних заказов']);ax.set_yticks([0,1],['Нет новых товарных публикаций','Есть товарные публикации']);ax.set_title('Каталоги по публикациям и заказам',loc='left',pad=18)
save(fig,'04_content_order_matrix.png',f"Активных каталогов: {fmt(goods_n)}. У {fmt(arr[0,0])} из {fmt(arr[:,0].sum())} продавцов нет новых товарных публикаций; старый контент возможен.")

fig,ax=plt.subplots(figsize=(10.8,5.5))
for metric,label,color in [('orders','Заказы',blue),('gmv','GMV',orange)]:
 vals=np.sort(d[metric].to_numpy())[::-1];cs=np.cumsum(vals)/vals.sum()*100
 ax.plot(np.arange(1,301),cs[:300],lw=2.5,label=label,color=color)
 for k in [10,100]:ax.scatter([k],[cs[k-1]],color=color);ax.annotate(f'{cs[k-1]:.1f}%',(k,cs[k-1]),xytext=(8,-15 if metric=='gmv' else 8),textcoords='offset points',color=color)
ax.set_xlim(0,300);ax.set_ylim(0,106);ax.set_xlabel('Крупнейшие бизнесы, ранжированные отдельно по каждому показателю');ax.set_ylabel('Накопленная доля, %');ax.set_title('Результат сильно сосредоточен у немногих бизнесов',loc='left',pad=18);ax.grid();ax.legend(frameon=False,loc='lower right')
top10_shares={metric:next(z['share'] for z in core['concentration'] if z['scope']=='all' and z['metric']==metric and z['cut']=='top10') for metric in ['orders','gmv']}
save(fig,'05_concentration.png',f"Всего бизнесов: {fmt(len(d))}. Показаны первые 300. Топ-10: {pct_text(top10_shares['orders'],1)} заказов и {pct_text(top10_shares['gmv'],1)} GMV.")

src=pd.read_excel(f,sheet_name='Маркет Вконтакте — источники').iloc[:6].copy()
fig,ax=plt.subplots(figsize=(10.8,6))
labels=['Другое','Витрина магазина','Лента','Клипы','Поиск','Главная Маркета'];y=np.arange(6);h=.34
for c,dx,label,color in [(1,-h/2,'Маркет VK',blue),(3,h/2,'Партнёр 1',orange)]:
 vals=src.iloc[:,c]/src.iloc[:,c].sum()*100;bars=ax.barh(y+dx,vals,h,label=label,color=color)
 for bar,val in zip(bars,vals):ax.text(val+.5,bar.get_y()+h/2,f'{val:.1f}%',va='center',fontsize=9)
ax.set_yticks(y,labels);ax.invert_yaxis();ax.set_xlim(0,84);ax.set_xlabel('Доля открытий карточек внутри каждого канала, %');ax.set_title('Каналы отличаются источниками открытий карточек',loc='left',pad=18);ax.legend(frameon=False);ax.grid(axis='x');ax.set_axisbelow(True)
save(fig,'06_traffic_sources.png','Лист «Источники», строки 2–7. Реферальная строка вложенная и не добавлена к сумме источников.')

cat=pd.read_excel(f,sheet_name='Маркет Вконтакте — категории')
fig,axs=plt.subplots(1,2,figsize=(11,6.7))
for ax,c,label,color in [(axs[0],2,'Маркет VK',blue),(axs[1],4,'Партнёр 1',orange)]:
 x=cat.sort_values(cat.columns[c],ascending=False).head(6);v=x.iloc[:,c]/cat.iloc[:,c].sum()*100
 bars=ax.barh(x.iloc[:,0].iloc[::-1],v.iloc[::-1],color=color)
 for bar,val in zip(bars,v.iloc[::-1]):ax.text(val+.4,bar.get_y()+bar.get_height()/2,f'{val:.1f}%',va='center',fontsize=9)
 ax.set_xlim(0,52);ax.set_title(label,loc='left',fontsize=13);ax.set_xlabel('Доля заказов таблицы, %');ax.grid(axis='x');ax.set_axisbelow(True)
fig.suptitle('Категории с наибольшим числом заказов',x=.02,ha='left',fontweight='bold',fontsize=16)
save(fig,'07_categories.png',f'Знаменатели: {fmt(cat.iloc[:,2].sum())} и {fmt(cat.iloc[:,4].sum())} заказов категорий. Они не совпадают с общими итогами каналов.')

daily=pd.read_excel(f,sheet_name='Маркет Вконтакте — по дням');dates=pd.to_datetime(daily.iloc[:,0])
fig,axs=plt.subplots(2,1,figsize=(10.8,7),sharex=True)
for ax,cols,title in [(axs[0],[12,13],'Оформленные заказы'),(axs[1],[15,16],'GMV оформленных заказов')]:
 for c,label,color in [(cols[0],'Маркет VK',blue),(cols[1],'Партнёр 1',orange)]:
  vals=daily.iloc[:,c];ax.plot(dates.dt.day,vals/vals.iloc[:7].mean()*100,label=label,color=color,lw=2.2)
 ax.axhline(100,color=gray,lw=1,ls='--');ax.set_ylabel('Индекс');ax.set_title(title,loc='left',fontsize=12);ax.grid();ax.legend(frameon=False,loc='upper left' if ax is axs[1] else 'upper right')
axs[1].set_xlabel(f'День месяца ({dates.iloc[0]:%m.%Y})');fig.suptitle('Динамика относительно среднего первых семи дней',x=.02,ha='left',fontsize=16,fontweight='bold')
save(fig,'08_daily_dynamics.png',f'Среднее {dates.iloc[0]:%d.%m}–{dates.iloc[6]:%d.%m.%Y} = 100 для каждого ряда. Синтетические данные; это не прогноз.')

buyers=pd.read_excel(f,sheet_name='Покупатели');age=buyers.groupby('Возраст',dropna=False).sum(numeric_only=True,min_count=1).reindex(['до 18','18–24','25–34','35–44','45–54','55+','н/д'])
fig,ax=plt.subplots(figsize=(10.8,5.8));x=np.arange(len(age));w=.36
for c,delta,label,color in [(0,-w/2,'Маркет VK',blue),(2,w/2,'Партнёр 1',orange)]:
 vals=age.iloc[:,c]/buyers.iloc[:,c+2].sum()*100;bars=ax.bar(x+delta,vals,w,label=label,color=color)
 for bar,val in zip(bars,vals):ax.text(bar.get_x()+w/2,val+.4,('<0,1%' if 0<val<.1 else f'{val:.1f}%'),ha='center',fontsize=9)
ax.set_xticks(x,age.index);ax.set_ylim(0,38);ax.set_ylabel('Доля покупателей опубликованного разреза, %');ax.set_title('Возрастная структура покупателей',loc='left',pad=18);ax.legend(frameon=False);ax.grid(axis='y');ax.set_axisbelow(True)
save(fig,'09_buyer_age.png',f'Базы: {fmt(buyers.iloc[:,2].sum())} покупателей Маркета VK и {fmt(buyers.iloc[:,4].sum())} Партнёра. Скрытые группы не приравнены к нулю.')

regions=pd.read_excel(f,sheet_name='Покупатели по регионам');known=regions[regions.iloc[:,0].ne('Не указан')].copy();vtotal=known.iloc[:,1].sum();ptotal=known.iloc[:,3].sum();top=known.sort_values(known.columns[1],ascending=False).head(8)
fig,ax=plt.subplots(figsize=(10.8,6.8));y=np.arange(len(top));h=.34
for c,total,delta,label,color in [(1,vtotal,-h/2,'Маркет VK',blue),(3,ptotal,h/2,'Партнёр 1',orange)]:
 vals=top.iloc[:,c]/total*100;bars=ax.barh(y+delta,vals,h,label=label,color=color)
 for bar,val in zip(bars,vals):ax.text(val+.09,bar.get_y()+h/2,f'{val:.2f}%',va='center',fontsize=8.5)
ax.set_yticks(y,top.iloc[:,0]);ax.invert_yaxis();ax.set_xlim(0,12);ax.set_xlabel('Доля среди покупателей с указанным регионом, %');ax.set_title('Регионы после исключения неизвестного региона',loc='left',pad=18);ax.legend(frameon=False,loc='lower right');ax.grid(axis='x');ax.set_axisbelow(True)
unknown_share_vk=(regions.iloc[:,1].sum()-vtotal)/regions.iloc[:,1].sum()
unknown_share_partner=(regions.iloc[:,3].sum()-ptotal)/regions.iloc[:,3].sum()
save(fig,'10_regions_known.png',f'Базы: {fmt(vtotal)} / {fmt(ptotal)}. Неизвестный регион: {pct_text(unknown_share_vk)} / {pct_text(unknown_share_partner)}. Состав покупателей, не проникновение.')

fig,ax=plt.subplots(figsize=(10.8,5.7));rows=[z for z in ext['standardized'] if z['scope']=='goods' and z['stratifier']=='active_products'];labels={'has_product_pubs':'Товарные публикации','delivery':'Доставка','payment':'Оплата','ad_active':'Реклама','has_opens':'Открытия из контента'}
y=np.arange(len(rows));h=.34
raw=[z['raw_rate1']/z['raw_rate0'] for z in rows];adj=[z['standardized_ratio'] for z in rows]
ax.barh(y-h/2,raw,h,color=gray,label='Исходное отношение долей');ax.barh(y+h/2,adj,h,color=blue,label='Одинаковый состав по размеру каталога')
for i,(a,b) in enumerate(zip(raw,adj)):
 ax.text(a+.04,i-h/2,f'{a:.2f}×',va='center',fontsize=9);ax.text(b+.04,i+h/2,f'{b:.2f}×',va='center',fontsize=9)
ax.set_yticks(y,[labels[z['feature']] for z in rows]);ax.invert_yaxis();ax.set_xlim(0,4.6);ax.axvline(1,color=navy,lw=1);ax.set_xlabel('Отношение долей бизнесов с заказами: есть / нет признака');ax.set_title('Часть различий связана с размером каталога',loc='left',pad=18);fig.legend(loc='lower center',bbox_to_anchor=(.5,.065),ncol=2,frameon=False,fontsize=9);ax.grid(axis='x');ax.set_axisbelow(True)
strata_label='/'.join(str(n) for n in sorted({z['strata'] for z in rows}))
save(fig,'11_standardization.png',f'Активных каталогов: {fmt(goods_n)}. Выравнивание по {strata_label} группам числа товаров. Другие различия не устранены; эффект не измерен.')

cols=['subscribers','active_products','views','product_pubs','product_views','content_opens','ad_spend','delivery','payment','orders','gmv']
labels=['Подписчики','Товары','Обычные просмотры','Товарные публикации','Товарные просмотры','Открытия из контента','Реклама','Доставка','Оплата','Заказы','GMV']
cm=d.loc[d.has_goods,cols].corr(method='spearman');fig,ax=plt.subplots(figsize=(10.2,8));im=ax.imshow(cm,vmin=-1,vmax=1,cmap='RdBu_r')
for i in range(len(cols)):
 for j in range(len(cols)):
  v=cm.iloc[i,j];ax.text(j,i,f'{v:.2f}',ha='center',va='center',fontsize=10.5,color='white' if abs(v)>.6 else navy)
ax.set_xticks(range(len(cols)),labels,rotation=48,ha='right');ax.set_yticks(range(len(cols)),labels);ax.set_title('Связи показателей среди активных каталогов',loc='left',pad=18);fig.colorbar(im,ax=ax,fraction=.035,pad=.03,label='Коэффициент Спирмена')
save(fig,'12_correlations.png',f'Активных каталогов: {fmt(goods_n)}. Ранговые связи описывают совместное изменение и не доказывают причинность.')
print('Created',len(list(OUT.glob('*.png'))),'charts')
