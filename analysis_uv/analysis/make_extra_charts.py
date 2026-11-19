"""Графики новых проверок из пересчитанных JSON."""
from pathlib import Path
import json
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'charts'; OUT.mkdir(exist_ok=True)
r=json.loads((ROOT/'analysis/robustness_results.json').read_text())
a=json.loads((ROOT/'analysis/extra_audit_results.json').read_text())
plt.rcParams.update({'font.family':'DejaVu Sans','font.size':11,'axes.spines.top':False,'axes.spines.right':False,'figure.dpi':120})
BLUE='#2563A6'; ORANGE='#CE762A'; GREY='#667085'

def save(fig,name,note):
    fig.text(.02,.015,note,fontsize=9,color=GREY,va='bottom')
    fig.savefig(OUT/name,dpi=150,bbox_inches='tight',facecolor='white')
    plt.close(fig)

summary=pd.DataFrame(r['joint_standardization']['summary'])
sel=summary[(summary.scope=='goods_no_referral') & (summary.quantiles==5) & (summary.variant=='all') & (summary.minimum_arm_n==1)].copy()
labels={'has_product_pubs':'Товарные публикации','delivery':'Доставка','payment':'Оплата','ad_active':'Рекламные траты','has_opens':'Открытия из контента'}
sel=sel.set_index('feature').loc[list(labels)].reset_index()
fig,ax=plt.subplots(figsize=(11,5.5)); y=np.arange(len(sel))
for i,row in sel.iterrows(): ax.plot([row.raw_ratio,row.standardized_ratio],[i,i],color='#CBD5E1',lw=3,zorder=1)
ax.scatter(sel.raw_ratio,y,color=ORANGE,s=65,label='Исходное отношение',zorder=3)
ax.scatter(sel.standardized_ratio,y,color=BLUE,s=65,label='После совместной стандартизации',zorder=3)
for i,row in sel.iterrows():
    ax.text(row.raw_ratio+.05,i-.08,f'{row.raw_ratio:.2f}',color=ORANGE,fontsize=10)
    ax.text(row.standardized_ratio+.05,i+.22,f'{row.standardized_ratio:.2f}',color=BLUE,fontsize=10)
ax.set_yticks(y,[labels[x] for x in sel.feature]); ax.invert_yaxis(); ax.axvline(1,color='#A0A0A0',ls='--',lw=1)
ax.set_xlabel('Отношение долей бизнесов с заказами, раз')
fig.suptitle('После выравнивания по размерам различия меньше',x=.02,ha='left',fontweight='bold',y=.98)
ax.set_xlim(.8,max(sel.raw_ratio)+.6); ax.set_ylim(len(sel)-.35,-.6); ax.grid(axis='x',alpha=.15)
handles,legend_labels=ax.get_legend_handles_labels()
fig.legend(handles,legend_labels,loc='upper left',bbox_to_anchor=(.25,.91),frameon=False)
fig.subplots_adjust(left=.25,bottom=.2,top=.74)
save(fig,'13_joint_standardization.png','Активные каталоги без рефералки; 5 × 5 групп, n = 3 859. Описательные связи, не причинные эффекты.')

thresholds=pd.DataFrame(r['candidates']['threshold_matrix'])
mat=thresholds.pivot(index='subscribers_min',columns='views_min',values='n').sort_index().sort_index(axis=1)
fig,ax=plt.subplots(figsize=(10,7)); im=ax.imshow(mat.to_numpy(),cmap='Blues',aspect='auto')
ax.set_xticks(np.arange(len(mat.columns)),[f'{int(x):,}'.replace(',',' ') for x in mat.columns])
ax.set_yticks(np.arange(len(mat.index)),[f'{int(x):,}'.replace(',',' ') for x in mat.index])
for i in range(len(mat)):
 for j in range(len(mat.columns)):
  value=int(mat.iloc[i,j]); ax.text(j,i,str(value),ha='center',va='center',color='white' if value>mat.to_numpy().max()*.55 else '#111827')
ax.set_xlabel('Минимум обычных просмотров'); ax.set_ylabel('Минимум подписчиков')
ax.set_title('Число кандидатов при разных правилах отбора',loc='left',fontweight='bold')
fig.colorbar(im,ax=ax,shrink=.7,label='Бизнесов'); fig.subplots_adjust(left=.15,bottom=.17,top=.9)
save(fig,'14_pilot_thresholds.png','Во всех ячейках: строгий сегмент, не менее 10 товаров, оформление включено. Пороги — рабочие допущения.')

scope_names={'all':'Все бизнесы','goods':'Активные каталоги','goods_no_referral':'Каталоги без рефералки','goods_only_no_referral':'Товары без услуг и рефералки'}
bounds=a['author_partial_identification']
fig,ax=plt.subplots(figsize=(11,5.3))
for i,row in enumerate(bounds):
 lo=row['minimum_compatible_completion']['difference_pp']; hi=row['maximum_compatible_completion']['difference_pp']; known=row['observed_known_labels']['difference_pp']
 ax.plot([lo,hi],[i,i],color=BLUE,lw=6,alpha=.75)
 ax.scatter([known],[i],color=ORANGE,s=65,zorder=4,label='Только известные оценки' if i==0 else None)
 ax.text(lo-.15,i-.15,f'{lo:.2f}',ha='right',fontsize=10);ax.text(hi+.15,i-.15,f'{hi:.2f}',ha='left',fontsize=10)
ax.set_yticks(range(len(bounds)),[scope_names.get(x['scope'],x['scope']) for x in bounds]);ax.invert_yaxis()
ax.axvline(0,color='#555555',lw=1,ls='--');ax.set_xlabel('Разница долей бизнесов с заказами: U = 1 минус U = 0, п.п.')
fig.suptitle('Пропуски оценки автора допускают оба знака связи',x=.02,ha='left',fontweight='bold',y=.98)
ax.set_ylim(len(bounds)-.35,-.65)
handles,legend_labels=ax.get_legend_handles_labels()
fig.legend(handles,legend_labels,loc='upper left',bbox_to_anchor=(.3,.9),frameon=False)
ax.grid(axis='x',alpha=.15)
fig.subplots_adjust(left=.3,bottom=.2,top=.75)
save(fig,'15_author_bounds.png','Синие отрезки — точные границы допустимых заполнений U. Это не доверительные интервалы и не эффект оригинальности.')

power=pd.DataFrame(r['pilot_power']['scenarios'])
sel=power[np.isclose(power.future_control_rate_assumption,.03)&np.isclose(power.participation_assumption,1.0)]
piv=sel.pivot(index='eligible_pool_n',columns='future_treatment_rate_assumption',values='exact_fisher_power')
fig,ax=plt.subplots(figsize=(10,5.7)); x=np.arange(len(piv)); width=.23
colors=[BLUE,ORANGE,'#3B8A70']
for j,column in enumerate(piv.columns):
 vals=piv[column].to_numpy()*100
 bars=ax.bar(x+(j-1)*width,vals,width,label=f'3% → {column*100:.0f}%',color=colors[j%len(colors)])
 ax.bar_label(bars,labels=[f'{v:.1f}%' for v in vals],fontsize=9,padding=3)
ax.axhline(80,color=GREY,ls='--',lw=1);ax.text(2.4,81,'80%',color=GREY,fontsize=9)
ax.set_ylim(0,112);ax.set_xticks(x,[str(int(v)) for v in piv.index]);ax.set_xlabel('Участников при полном наборе, распределение 1:1')
ax.set_ylabel('Вероятность обнаружить заданное различие, %')
fig.suptitle('Мощность пилота при условной частоте контроля 3%',x=.02,ha='left',fontweight='bold',y=.98)
handles,legend_labels=ax.get_legend_handles_labels()
fig.legend(handles,legend_labels,title='Заданные будущие частоты',ncol=3,frameon=False,loc='upper left',bbox_to_anchor=(.12,.91))
ax.grid(axis='y',alpha=.15)
fig.subplots_adjust(bottom=.2,top=.73)
save(fig,'16_pilot_power.png','Сценарий: независимые бизнесы, двусторонний точный тест Фишера, α = 0,05. Это не прогноз частоты заказов.')
print('Созданы 4 графика дополнительных проверок')
