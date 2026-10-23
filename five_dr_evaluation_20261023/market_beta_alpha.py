"""Frozen market-proxy OLS/HAC extension. Local source and daily data never exported."""
import os
for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS'): os.environ[k]='1'
import sys,json,hashlib,traceback,time,math
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import pyarrow as pa
ROOT=Path(__file__).resolve().parent
OLD=ROOT.parent/'daily_factor_lab_20261016'
sys.path.insert(0,str(OLD))
from lab import Market
from research import base_config,UNIVERSES,reserve_performance_cores
pa.set_cpu_count(1);pa.set_io_thread_count(1)
BASE=ROOT/'evaluation_20261023_200711'
POOL=ROOT/'pool_sensitivity_20261023_202110'
SOURCE=ROOT.parent/'feathers.zip'
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def clean(x):
    if isinstance(x,dict):return {str(k):clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple)):return [clean(v) for v in x]
    if isinstance(x,np.generic):return clean(x.item())
    if isinstance(x,float) and not math.isfinite(x):return None
    return x
def save(p,x):Path(p).write_text(json.dumps(clean(x),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
def fit_calendar(y,x,dates,L=5):
    """OLS complete pairs; HAC scores padded with zeros on excluded SOURCE rows."""
    y=np.asarray(y,float);x=np.asarray(x,float);ok=np.isfinite(y)&np.isfinite(x)
    X=np.column_stack([np.ones(ok.sum()),x[ok]]);yy=y[ok]
    if ok.sum()<3 or np.linalg.matrix_rank(X)<2:raise ValueError('Insufficient or singular complete pairs')
    bread=np.linalg.inv(X.T@X);b=bread@X.T@yy;u=yy-X@b
    score=np.zeros((len(y),2));score[ok]=X*u[:,None]
    meat=score.T@score
    for lag in range(1,min(L,len(y)-1)+1):
        cross=score[lag:].T@score[:-lag];meat+=(1-lag/(L+1))*(cross+cross.T)
    cov=bread@meat@bread;se=np.sqrt(np.maximum(np.diag(cov),0));z=b/se
    out={'n':int(ok.sum()),'source_calendar_n':len(y),'excluded_dates':[str(d) for d in np.asarray(dates)[~ok]],'HAC_maxlags':L,'HAC_use_correction':False,'HAC_use_t':False,'R_squared':float(1-(u@u)/((yy-yy.mean())@(yy-yy.mean())))}
    for i,label in enumerate(('daily_alpha','beta')):
        out.update({label:float(b[i]),label+'_se':float(se[i]),label+'_t':float(z[i]),label+'_raw_P':math.erfc(abs(float(z[i]))/math.sqrt(2)),label+'_CI95_low':float(b[i]-1.959963984540054*se[i]),label+'_CI95_high':float(b[i]+1.959963984540054*se[i])})
    for suffix in ('','_CI95_low','_CI95_high'):out['linear_annual_alpha_252'+suffix]=out['daily_alpha'+suffix]*252
    return out

def run():
    started=time.monotonic();resource=reserve_performance_cores()
    if len(resource.get('reserved_logical_processors',[]))<4:raise RuntimeError('Two performance cores not verified')
    main=json.loads((BASE/'protocol.json').read_text(encoding='utf-8'));other=json.loads((POOL/'protocol.json').read_text(encoding='utf-8'))
    identity={'path':str(SOURCE),'size_bytes':SOURCE.stat().st_size,'sha256':sha(SOURCE)}
    if identity!=main['source_identity'] or identity!=other['source_identity']:raise ValueError('ZIP identity mismatch')
    paths={'all_liquid':BASE/'execution',**{p:POOL/p/'execution' for p in ('main_liquid','liquid1000')}}
    summaries={p:json.loads((d/'execution_summary.json').read_text(encoding='utf-8')) for p,d in paths.items()}
    inputs={str(p.relative_to(ROOT)):sha(p) for d in paths.values() for p in sorted(d.rglob('daily.csv'))}
    inputs.update({str(p.relative_to(ROOT)):sha(p) for d in paths.values() for p in sorted(d.glob('*__paired.csv'))})
    out=ROOT/('market_beta_alpha_'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S'));out.mkdir(exist_ok=False)
    protocol={'frozen_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'status':'frozen_before_computation','source_identity':identity,'script_sha256':sha(__file__),'input_daily_and_paired_sha256':inputs,'execution_summary_sha256':{p:sha(d/'execution_summary.json') for p,d in paths.items()},'old_source_sha256':{n:sha(OLD/n) for n in main['reused_V4_compute_sha256']},'periods':main['execution_periods'],'pool_rules':main['information_universes'],'base_config':main['execution_base_config'],'risk_free_daily':0,'proxy_definition':'At source date t use Market.eligible(base_config pool)[t-1]; equal fixed 1/N weights of adjusted close_t/close_t-1-1. Finite positive raw close and cumadj on both dates required. strict return missing if any selected leg missing or N=0. No complete-case reweighting. missing_leg_zero_mark assigns zero to unavailable legs, a valuation assumption only. Not official or verified investable index.','HAC':'OLS intercept, Bartlett L5 no small-sample correction; normal CI and raw two-sided P. Scores padded at excluded source rows preserve lag distances. Main cell also L20.','fixed_primary_cell':{'pool':'all_liquid','period':'evaluation_2025','strategy':'5dr','scenario':'base','proxy_pool':'all_liquid','proxy_method':'strict','HAC_maxlags':5},'expected_unique_market_fits':201,'expected_relative_P3_checks':18,'resource_policy':resource,'numeric_and_arrow_threads':1,'failure_policy':'Record each regression error and continue remaining jobs','boundaries':['Previously observed periods, no untouched out-of-sample claim','Linear annual alpha=252*daily intercept, not compound return','All constructed and source daily series remain local; only aggregate outputs eligible for later publication']}
    save(out/'protocol.json',protocol)
    (out/'local').mkdir();market=Market(SOURCE);dates=market.dates.strftime('%Y-%m-%d').to_numpy();proxies={};counts=[]
    raw=market.raw['close'];fac=market.raw['cumadj'];valid=np.isfinite(raw)&(raw>0)&np.isfinite(fac)&(fac>0)
    for pool,rules in UNIVERSES.items():
        cfg=base_config(wind_down_days=20,universe=rules['universe'],liquidity_top_n=rules['liquidity_top_n']);eligible=market.eligible(cfg);rows=[]
        for i,date in enumerate(dates):
            selected=eligible[i-1] if i else np.zeros(len(market.symbols),bool);n=int(selected.sum());ok=selected&valid[i]&(valid[i-1] if i else False);v=int(ok.sum())
            total=float(np.sum(market.close[i,ok]/market.close[i-1,ok]-1)) if i else 0.
            rows.append({'date':date,'source_position':i,'N':n,'valid_N':v,'missing_legs':n-v,'missing_weight':(n-v)/n if n else np.nan,'strict_proxy_return':total/n if n and n==v else np.nan,'missing_leg_zero_mark':total/n if n else np.nan})
        frame=pd.DataFrame(rows);frame.to_csv(out/'local'/f'proxy_daily_{pool}.csv',index=False,float_format='%.17g');proxies[pool]=frame.set_index('date')
        for period,spec in main['execution_periods'].items():
            f=frame.loc[frame.date.between(spec['start'],spec['end'])];counts.append({'proxy_pool':pool,'period':period,'calendar_days':len(f),'strict_missing_days':int(f.strict_proxy_return.isna().sum()),'days_with_missing_legs':int((f.missing_legs>0).sum()),'total_missing_legs':int(f.missing_legs.sum()),'max_missing_weight':float(f.missing_weight.max())})
    results=[];relative=[];errors=[]
    for pool,summary in summaries.items():
        for row in summary['runs']:
            try:
                daily=pd.read_csv(paths[pool]/row['run']/'daily.csv');daily.date=pd.to_datetime(daily.date).dt.strftime('%Y-%m-%d');positions=pd.Index(dates).get_indexer(daily.date)
                if np.any(positions<0) or len(set(positions))!=len(positions):raise ValueError('Invalid source calendar')
                grid=dates[positions.min():positions.max()+1];y=daily.set_index('date')['return'].reindex(grid).to_numpy()
                for proxy_pool in dict.fromkeys(('all_liquid',pool)):
                    for method,col in [('strict','strict_proxy_return'),('missing_leg_zero_mark','missing_leg_zero_mark')]:
                        lags=[5,20] if pool=='all_liquid' and row['period']=='evaluation_2025' and row['strategy']=='5dr' and row['scenario']=='base' and method=='strict' else [5]
                        for lag in lags:
                            meta={k:row[k] for k in ('period','strategy','scenario')};meta.update(pool=pool,proxy_pool=proxy_pool,proxy_method=method,proxy_role='common_primary_and_same_pool_sensitivity' if pool=='all_liquid' else ('common_primary' if proxy_pool=='all_liquid' else 'same_pool_sensitivity'))
                            try:results.append({**meta,**fit_calendar(y,proxies[proxy_pool].reindex(grid)[col],grid,lag)})
                            except Exception:errors.append({**meta,'HAC_maxlags':lag,'error':traceback.format_exc()})
            except Exception:errors.append({'pool':pool,'run':row['run'],'error':traceback.format_exc()})
        for pair in summary['paired']:
            try:
                df=pd.read_csv(paths[pool]/f"{pair['period']}__{pair['scenario']}__paired.csv");df.date=pd.to_datetime(df.date).dt.strftime('%Y-%m-%d');grid=dates[(dates>=df.date.min())&(dates<=df.date.max())];df=df.set_index('date').reindex(grid)
                fit=fit_calendar(df.added_return,df.base_return,grid);saved=pair['relative_three_factor_alpha'];relative.append({'pool':pool,'period':pair['period'],'scenario':pair['scenario'],**fit,'saved_beta':saved['beta'],'saved_daily_alpha':saved['daily_alpha'],'beta_difference_from_saved':fit['beta']-saved['beta'],'alpha_difference_from_saved':fit['daily_alpha']-saved['daily_alpha'],'saved_alpha_CI95_low':saved['HAC_daily_alpha_CI95'][0],'saved_alpha_CI95_high':saved['HAC_daily_alpha_CI95'][1],'alpha_CI95_low_difference':fit['daily_alpha_CI95_low']-saved['HAC_daily_alpha_CI95'][0],'alpha_CI95_high_difference':fit['daily_alpha_CI95_high']-saved['HAC_daily_alpha_CI95'][1]})
            except Exception:errors.append({'pool':pool,'pair':pair['period']+'__'+pair['scenario'],'error':traceback.format_exc()})
    for name,rows in [('regressions',results),('relative_p3_checks',relative),('proxy_missingness',counts)]:
        serial=[{k:json.dumps(v) if isinstance(v,list) else v for k,v in r.items()} for r in rows];pd.DataFrame(serial).to_csv(out/(name+'.csv'),index=False,float_format='%.17g');save(out/(name+'.json'),rows)
    save(out/'errors.json',errors);status={'status':'computed_pending_validation','output':str(out),'unique_market_fits':len(results),'relative_P3_checks':len(relative),'errors':len(errors),'elapsed_seconds':time.monotonic()-started};save(out/'status.json',status);print(json.dumps(status),flush=True)
if __name__=='__main__':run()
