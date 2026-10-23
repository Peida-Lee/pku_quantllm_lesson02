"""Render only saved aggregate market-proxy results; no trading or statistical refits."""
import os
for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS'):os.environ[k]='1'
import sys,json,argparse
from pathlib import Path
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'daily_factor_lab_20261016'))
from research import reserve_performance_cores
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter
POOLS=('all_liquid','main_liquid','liquid1000')
PERIODS=('evaluation_2025','evaluation_2026sep')
LABEL={'all_liquid':'全部流动池','main_liquid':'主板流动池','liquid1000':'金额前1000池','evaluation_2025':'2025','evaluation_2026sep':'2026年1—9月'}
def pct(v):return f'{100*float(v):.2f}%'
def ci(r,key,percent=False):
    fmt=pct if percent else lambda x:f'{x:.4f}'
    return f"{fmt(r[key])} [{fmt(r[key+'_CI95_low'])}, {fmt(r[key+'_CI95_high'])}]"
def table(rows,cols):return '\n'.join(['|'+'|'.join(c[0] for c in cols)+'|','|'+'|'.join('---' for _ in cols)+'|']+['|'+'|'.join(str(f(r)) for _,f in cols)+'|' for r in rows])
def render(out):
    resource=reserve_performance_cores();assert len(resource.get('reserved_logical_processors',[]))>=4
    r=pd.read_csv(out/'regressions.csv');p=pd.read_csv(out/'relative_p3_checks.csv');missing=pd.read_csv(out/'proxy_missingness.csv')
    validation=json.loads((out/'validation.json').read_text(encoding='utf-8')) if (out/'validation.json').exists() else {}
    status=f"独立验收：{validation.get('status','待验收')}；核对项 {validation.get('check_count','待定')}。"
    def select(pool,period,proxy=None,method='strict',lag=5):
        return r[(r.pool==pool)&(r.period==period)&(r.strategy=='5dr')&(r.scenario=='base')&(r.proxy_pool==(proxy or 'all_liquid'))&(r.proxy_method==method)&(r.HAC_maxlags==lag)].iloc[0]
    common=[select(pool,period) for pool in POOLS for period in PERIODS]
    own=[select(pool,period,pool) for pool in POOLS for period in PERIODS]
    main=common[0];zero=select('all_liquid','evaluation_2025',method='missing_leg_zero_mark');l20=select('all_liquid','evaluation_2025',lag=20)
    (out/'figures').mkdir(exist_ok=True)
    plt.rcParams.update({'font.family':'Microsoft YaHei','font.size':11,'axes.unicode_minus':False})
    fig,axes=plt.subplots(1,2,figsize=(13.6,6.4),sharey=True);y=np.arange(6)
    labels=[LABEL[x['pool']]+' · '+LABEL[x['period']] for x in common]
    for ax,key,reference,title in zip(axes,('beta','linear_annual_alpha_252'),(1,0),('对共同市场代理的 β','线性年化 α（每日截距 × 252）')):
        vals=np.array([x[key] for x in common]);lo=np.array([x[key+'_CI95_low'] for x in common]);hi=np.array([x[key+'_CI95_high'] for x in common])
        for i in range(6):ax.errorbar(vals[i],i,xerr=[[vals[i]-lo[i]],[hi[i]-vals[i]]],fmt='o',color='#2166ac' if i%2==0 else '#b35806',capsize=4,markersize=6)
        ax.axvline(reference,color='#777777',ls='--',lw=1);ax.set_title(title,pad=15);ax.grid(axis='x',alpha=.2);ax.spines[['top','right','left']].set_visible(False);ax.tick_params(axis='y',length=0);ax.set_ylim(5.5,-.5)
        if key!='beta':ax.xaxis.set_major_formatter(PercentFormatter(1))
    axes[0].set_yticks(y,labels);axes[1].tick_params(axis='y',labelleft=False)
    fig.suptitle('5DR 基础执行情景：三池、两时期，固定共同代理 all_liquid',fontsize=16,y=.98)
    fig.text(.02,.07,'点：OLS估计；线：95% HAC置信区间（Bartlett L5，无小样本修正）。蓝：2025；棕：2026年1—9月。',fontsize=10)
    fig.text(.02,.03,'严格代理保持 t−1 成员及每腿 1/N 权重；2025年2月10日缺失，HAC保留源日历距离。α × 252 不是复利收益。',fontsize=10)
    fig.subplots_adjust(left=.24,right=.98,bottom=.19,top=.86,wspace=.17);fig.savefig(out/'figures/06_market_beta_alpha.png',dpi=180);plt.close(fig)
    cols=[('股票池',lambda x:LABEL[x['pool']]),('时期',lambda x:LABEL[x['period']]),('β [95%CI]',lambda x:ci(x,'beta')),('线性年化α [95%CI]',lambda x:ci(x,'linear_annual_alpha_252',True)),('α原始P',lambda x:f"{x['daily_alpha_raw_P']:.6g}"),('R²',lambda x:f"{x['R_squared']:.4f}"),('有效/日历日',lambda x:f"{x['n']}/{x['source_calendar_n']}")]
    text=f'''# 市场代理 β / α 扩展

{status} 计算状态保留 computed_pending_validation；验收由独立 validation.json 给出。201组市场回归与18组P3相对回归计算错误为0。

## 主结论

固定主判断为2025年、all_liquid、5DR、base、strict共同代理、HAC L5，不按结果选择情景。已验收原执行净收益为37.3564%；本扩展估计 β={ci(main,'beta')}，日α={main.daily_alpha:.10f}，日α95%CI [{main.daily_alpha_CI95_low:.10f}, {main.daily_alpha_CI95_high:.10f}]；线性年化α={ci(main,'linear_annual_alpha_252',True)}，原始P={main.daily_alpha_raw_P:.6f}。β的标准误为{main.beta_se:.6f}、t={main.beta_t:.4f}、原始P={main.beta_raw_P:.6g}；日α标准误为{main.daily_alpha_se:.10f}、t={main.daily_alpha_t:.6f}；R²={main.R_squared:.4f}，有效242/243日。

这支持“未发现相对该代理的明确非零截距”，不能证明α等于0，也不能把全部净收益都归因于市场。α的不确定性很宽，β是每日收益共变斜率。净复利收益与每日截距×252的口径不同。

金额前1000池2025年对共同代理的线性年化α约−2.72%，而对自身代理约19.24%（原始P约0.1009）；这显示基准依赖，不能切换基准选赢家。两种代理及所有成本、容量、延迟情景均固定报告。

![共享代理下的β与线性年化α置信区间](figures/06_market_beta_alpha.png)

## 数据与预先固定的方法

唯一原始数据为 feathers.zip。交易不重跑，直接读取此前已验收的60条逐日净收益账本：三池×两时期×每期10情景。每期10情景为5DR、P3（base_combo）、P4（added_combo）各自的base、费用翻倍、0.1%容量，以及5DR额外延迟1日base。时期为2025与2026年1—9月，均为已接触的回顾性评价。

每个代理在收益日t使用前一源日期t−1收盘已知的Market.eligible集合，按各池相同base_config规则：至少120行历史、原始收盘价≥2元、过去20日均额≥2000万元、活跃日≥18、继承price_proxy风险筛选；主板池另限主板，金额前1000池另在合格股中按过去20日均额取前1000。集合每日动态更新，收益为累计复权收盘 C[t]/C[t−1]−1 的等权均值。每腿权重固定1/N；两日价格和复权因子都须有限且正。strict任一腿缺失则整日代理缺失，绝不按剩余股票重分配。missing_leg_zero_mark将缺失腿当日回报设0，其他腿仍1/N，仅为估值假设。

无风险利率按授权固定0。回归为 r_p=α+β×r_proxy，含截距OLS；主代理统一all_liquid，同池代理作固定敏感性，all_liquid同一拟合兼具两种角色，不重复拟合。HAC为Bartlett L5、无小样本修正、正态95%CI、原始双侧P；仅主格另给L20。缺失日保留完整源日历位置，排除观测的得分填零后按源行间隔求HAC，禁止压缩日期。α线性年化及CI都乘252，不是复利回报。全部201组回归共享数据和情景，不是201项独立发现，也未校正多重比较。

## 共同代理：固定5DR基础情景

{table(common,cols)}

## 同池代理敏感性：固定5DR基础情景

{table(own,cols)}

## 缺失与主格敏感性

2025年all_liquid和liquid1000各有1个缺失腿日（2025年2月10日），分别缺1腿；主板池无缺失日。2026年1—9月三池均无缺失。strict主格排除2月10日，但其前后日HAC距离保持原样。每日N、有效N、缺失数及缺失权重保留本地，不发布股票级或原始日序列。

|主格方法|β [95%CI]|线性年化α [95%CI]|α原始P|有效日|
|---|---|---|---|---|
|strict L5|{ci(main,'beta')}|{ci(main,'linear_annual_alpha_252',True)}|{main.daily_alpha_raw_P:.6f}|{main.n}|
|strict L20|{ci(l20,'beta')}|{ci(l20,'linear_annual_alpha_252',True)}|{l20.daily_alpha_raw_P:.6f}|{l20.n}|
|缺失腿0回报 L5|{ci(zero,'beta')}|{ci(zero,'linear_annual_alpha_252',True)}|{zero.daily_alpha_raw_P:.6f}|{zero.n}|

## 18组相对P3回归

独立从已有paired.csv重算净 r_P4=α+β×净 r_P3，HAC L5及同样正态CI/原始P。这里的β、α是相对固定P3组合，并非市场CAPM或官方多因子风险α。P4将原三项权重一起改变，不能解释成其他权重保持不变的纯5DR边际作用。下表报告全部三池×两时期×三情景；与此前保存的α、β及αCI差值在聚合检查文件内。

{table([x for _,x in p.iterrows()],[('股票池',lambda x:LABEL[x['pool']]),('时期',lambda x:LABEL[x['period']]),('情景',lambda x:x['scenario']),('相对P3 β [95%CI]',lambda x:ci(x,'beta')),('相对P3线性年化α [95%CI]',lambda x:ci(x,'linear_annual_alpha_252',True)),('α原始P',lambda x:f"{x['daily_alpha_raw_P']:.6g}"),('n',lambda x:x['n'])])}

## 解释边界与聚合文件

代理不是官方或已证明可交易的指数：动态成员、收盘等权回报没有扣除指数再平衡成本，组合则是含成本净收益，存在口径差异。组合继承20交易日期末wind-down、现金缓冲及未投满影响，截距也可能混合这些差异。行业、规模和其他风险暴露未控制，price_proxy不是真实历史ST标签，日线假设不能证明开盘竞价可成交。rf=0不代表实际现金收益为零。

可复核聚合文件：[全部201组市场回归](regressions.csv)、[18组P3相对回归与原值差](relative_p3_checks.csv)、[代理缺失统计](proxy_missingness.csv)。不发布local目录、原始ZIP、策略逐日账本或股票级构造数据。图由这些聚合结果直接绘制，不是截图。
'''
    (out/'MARKET_BETA_ALPHA.md').write_text(text,encoding='utf-8');print(out/'MARKET_BETA_ALPHA.md')
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('output',type=Path);render(parser.parse_args().output)
