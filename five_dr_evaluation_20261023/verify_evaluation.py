"""Independent persisted-file verification. Never imports new computation modules.

Run only after computation ends: python verify_evaluation.py OUTPUT_DIRECTORY
Money absolute tolerance .001; ratios absolute tolerance 1e-10.
"""
import os
for _k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS'):
    os.environ[_k] = '1'
import argparse
import hashlib
import json
import math
import sys
import traceback
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import statsmodels.api as sm

ROOT = Path(__file__).resolve().parent
OLD = ROOT.parent / 'daily_factor_lab_20261016'
METRICS = ('ic','rank_ic','spread_q10_q1','beta','p25_p75_difference','conditional_beta')
MONEY = {'final_nav','costs','writeoff_loss','residual_value'}

def load(p):
    return json.loads(Path(p).read_text(encoding='utf-8-sig'))

def frame(p):
    return pd.read_csv(p, float_precision='round_trip', dtype={'symbol':str})

def sha(p):
    with Path(p).open('rb') as f:
        return hashlib.file_digest(f,'sha256').hexdigest()

def clean(x):
    if isinstance(x,dict): return {str(k):clean(v) for k,v in x.items()}
    if isinstance(x,(tuple,list)): return [clean(v) for v in x]
    if isinstance(x,np.generic): return clean(x.item())
    if isinstance(x,float) and not np.isfinite(x): return None
    return x

def equal(a,b,tol=1e-10):
    if a is None or b is None:
        return (a is None or (isinstance(a,(float,np.floating)) and np.isnan(a))) and (b is None or (isinstance(b,(float,np.floating)) and np.isnan(b)))
    try:
        aa,bb=np.asarray(a,float),np.asarray(b,float)
        return aa.shape==bb.shape and bool(np.allclose(aa,bb,rtol=0,atol=tol,equal_nan=True))
    except (ValueError,TypeError): return a==b

def hac(x,lag):
    # Intercept sandwich built independently via all pairs on source coordinates.
    x=np.asarray(x,float); valid=np.isfinite(x); n=int(valid.sum())
    avg=float(np.mean(x[valid])) if n else None
    out=dict(n=n,mean=avg,lag=int(lag),se=None,t=None,p_raw=None,ci95_low=None,ci95_high=None)
    if n<20:return out
    residual=np.zeros(len(x));residual[valid]=x[valid]-avg
    q=np.dot(residual,residual)
    for k in range(1,min(lag,len(x)-1)+1):
        q+=2*(lag+1-k)/(lag+1)*np.dot(residual[:-k],residual[k:])
    se=float(np.sqrt(max(0,q/(n*(n-1)))))
    out.update(se=se,ci95_low=avg-1.959963984540054*se,ci95_high=avg+1.959963984540054*se)
    if se>0:out.update(t=avg/se,p_raw=math.erfc(abs(avg/se)/math.sqrt(2)))
    return out

def describe(x,h):
    x=np.asarray(x,float);finite=x[np.isfinite(x)]
    d=hac(x,max(5,h));sd=float(np.std(finite,ddof=1)) if len(finite)>1 else None
    d.update(std=sd,positive_fraction=float(np.mean(finite>0)) if len(finite) else None,
             mean_over_std=d['mean']/sd if sd and sd>0 else None)
    if h==5:d['hac_sensitivity']={str(k):hac(x,k) for k in (10,20)}
    return d

def perf(d,capital):
    v=d.nav.to_numpy(float);previous=np.r_[capital,v[:-1]]
    r=np.divide(v,previous,out=np.ones_like(v),where=previous>0)-1
    sd=float(np.std(r,ddof=1)) if len(r)>1 else 0
    return dict(total_return=v[-1]/capital-1,annual_return=(v[-1]/capital)**(252/len(r))-1 if v[-1]>0 else -1,
      annual_vol=sd*np.sqrt(252),sharpe=float(r.mean()/sd*np.sqrt(252)) if sd>0 else 0,
      max_drawdown=float(np.min(v/np.maximum.accumulate(np.r_[capital,v])[1:]-1)),days=len(r),final_nav=v[-1],
      costs=float(d.cost.sum()),annual_two_sided_turnover=float(d.turnover.sum()*252/len(r)),
      mean_invested=float(d.invested_fraction.mean()),trades=int(d.trades.sum()),
      writeoff_loss=float(d.writeoff_loss.sum()),max_stale_fraction=float(d.stale_fraction.max())),r

class Audit:
    def __init__(self):self.checks=[]
    def check(self,name,ok,**detail):
        self.checks.append(dict(name=name,passed=bool(ok),**clean(detail)))
    def same(self,name,a,b,tol=1e-10):
        ok=equal(a,b,tol)
        self.check(name,ok,**({} if ok else {'expected':repr(a)[:1000],'actual':repr(b)[:1000]}))
    def nested(self,name,expected,saved):
        for k,v in expected.items():
            if isinstance(v,dict):self.nested(name+'.'+k,v,saved.get(k,{}))
            else:self.same(name+'.'+k,v,saved.get(k))

def cohort_folder(out,key):
    local=out/'information/cohorts'/key
    manifest=load(out/'information/reused_cohorts.json') if (out/'information/reused_cohorts.json').exists() else []
    matching=[r for r in manifest if r['cohort']==key]
    if len(matching)>1:raise ValueError('Duplicate reuse cohort '+key)
    return ROOT/matching[0]['directory'] if matching else local

def information(out,p,a,calendar):
    merged=load(out/'information/summary.json');a.check('information_no_failures',not merged.get('cohort_failures'))
    expected=[];parts={k:[] for k in ('information_daily','information_deciles','information_monthly')}
    for period,spec in p['periods'].items():
      for universe in p['information_universes']:
       for h in p['horizons']:
        key=f'{period}__{universe}__h{h:02d}';expected.append(key)
        try:
            folder=cohort_folder(out,key)
            d=frame(folder/'information_daily.csv');g=frame(folder/'information_deciles.csv')
            for table in parts:
                try:parts[table].append(frame(folder/(table+'.csv')))
                except pd.errors.EmptyDataError:pass
            s=load(folder/'summary.json')['periods'][period][universe][str(h)]
            ds=pd.to_datetime(d.signal_date);en=pd.to_datetime(d.entry_date);ex=pd.to_datetime(d.exit_date)
            loc=calendar.get_indexer(ds);a.check(key+'.source_dates',bool(np.all(loc>=0)))
            wanted=calendar[:-1][(calendar[1:]>=spec['start'])&(calendar[1:]<=spec['end'])]
            a.check(key+'.complete_signal_calendar',ds.tolist()==wanted.tolist() and not ds.duplicated().any())
            epos=loc+1;xpos=loc+1+h
            a.check(key+'.entry_positions',en.tolist()==calendar[epos].tolist())
            expected_exit=[calendar[i] if i<len(calendar) else pd.NaT for i in xpos]
            a.check(key+'.exit_positions',all((pd.isna(x) and pd.isna(y)) or x==y for x,y in zip(ex,expected_exit)))
            inside=(xpos<len(calendar))&(en>=spec['start']).to_numpy()&(ex<=spec['end']).to_numpy()
            flags=d.window_in_period.map(lambda x:x is True or str(x).lower()=='true').to_numpy()
            a.check(key+'.window_flags',np.array_equal(inside,flags))
            a.check(key+'.counts',bool(((d.signal_count>=d.paired_count)&(d.paired_count>=0)&(d.missing_count==d.signal_count-d.paired_count)&(d.unique_score_count<=d.signal_count)).all()))
            a.same(key+'.coverage',np.where(d.signal_count>0,d.paired_count/d.signal_count,np.nan),d.coverage)
            a.same(key+'.raw_ic',-d.ic,d.raw_r5_ic);a.same(key+'.raw_rank_ic',-d.rank_ic,d.raw_r5_rank_ic)
            a.check(key+'.censored_no_targets',bool((d.loc[~inside,'paired_count']==0).all()) and d.loc[~inside,list(METRICS)].isna().all().all())
            a.check(key+'.minimum_pairs',d.loc[d.paired_count<30,['ic','rank_ic','beta','p25_p75_difference']].isna().all().all())
            a.check(key+'.group_keys',len(g)==len(d)*10 and not g.duplicated(['date','decile']).any() and set(g.decile)==set(range(1,11)))
            gg=g.groupby('date',sort=False)
            for col,source in [('assigned','signal_count'),('covered','paired_count')]:
                a.same(key+'.group_'+col,gg[col].sum().reindex(d.date).to_numpy(),d[source])
            a.check(key+'.group_counts',bool(((g.assigned>=g.covered)&(g.covered>=0)).all()))
            a.same(key+'.group_coverage',np.where(g.assigned>0,g.covered/g.assigned,np.nan),g.coverage)
            wide=g.pivot(index='date',columns='decile',values='mean_simple_return').reindex(d.date)
            spread=(wide[10]-wide[1]).to_numpy();spread[(d.paired_count<30)|(d.unique_score_count<=1)]=np.nan
            a.same(key+'.spread',spread,d.spread_q10_q1)
            for metric in METRICS:a.nested(key+'.'+metric,describe(d[metric],h),s[metric])
            cov=dict(signal_total=int(d.signal_count.sum()),paired_total=int(d.paired_count.sum()),missing_total=int(d.missing_count.sum()),
                in_period_signal_total=int(d.loc[inside,'signal_count'].sum()),in_period_paired_total=int(d.loc[inside,'paired_count'].sum()),
                in_period_missing_total=int(d.loc[inside,'missing_count'].sum()),boundary_censored_signal_total=int(d.loc[~inside,'signal_count'].sum()))
            a.nested(key+'.denominators',cov,s['coverage'])
            a.check(key+'.merged_summary',s==merged['periods'][period][universe][str(h)])
            monthly=frame(folder/'information_monthly.csv') if h==5 else None
            if monthly is not None:
                labels={'month':en.dt.strftime('%Y-%m'),'half_year':en.dt.year.astype(str)+'-H'+((en.dt.month-1)//6+1).astype(str)}
                count=0
                for frequency,label in labels.items():
                    for segment,sub in d.groupby(label,sort=False):
                        for metric in METRICS:
                            row=monthly[(monthly.frequency==frequency)&(monthly.segment==segment)&(monthly.metric==metric)]
                            a.check(key+'.segment_unique.'+frequency+str(segment)+metric,len(row)==1)
                            if len(row)==1:a.nested(key+'.segment.'+frequency+str(segment)+metric,{k:v for k,v in describe(sub[metric],h).items() if k!='hac_sensitivity'},row.iloc[0].to_dict())
                            count+=1
                a.same(key+'.segment_count',count,len(monthly))
        except Exception as exc:a.check(key+'.exception',False,error=repr(exc),traceback=traceback.format_exc())
    actual={x.name for x in (out/'information/cohorts').iterdir() if x.is_dir()}
    reused=load(out/'information/reused_cohorts.json') if (out/'information/reused_cohorts.json').exists() else []
    for r in reused:
        a.check('reuse_path.'+r['cohort'],r['cohort'] not in actual and (ROOT/r['directory']).resolve().parent.parent.parent.name==p.get('reuse_information_from'))
    actual.update(r['cohort'] for r in reused)
    a.check('reuse_unique',len(reused)==len({r['cohort'] for r in reused}))
    if reused:
        prior=load(ROOT/p['reuse_information_from']/'protocol.json')
        a.check('reuse_compute_definitions',prior['source_identity']==p['source_identity'] and prior['reused_V4_compute_sha256']==p['reused_V4_compute_sha256'] and all(prior['new_compute_sha256'][k]==p['new_compute_sha256'][k] for k in ('information_evaluation.py',)))
    a.check('all_36_cohorts',actual==set(expected) and len(expected)==36,expected=expected,actual=sorted(actual))
    for table,items in parts.items():
        if not items:
            a.check('merged_csv.'+table,False,error='No complete cohort tables');continue
        combined=pd.concat(items,ignore_index=True);saved=frame(out/'information'/(table+'.csv'))
        a.check('merged_csv.'+table,combined.equals(saved))

def execution(out,p,a,calendar):
    s=load(out/'execution/execution_summary.json');a.check('execution_no_failures',not s['failures'])
    a.same('20_completed',20,s.get('completed_simulations'));a.same('20_run_records',20,len(s['runs']))
    lookup={};expected=set()
    for period in p['execution_periods']:
      for scenario in ('base','double_cost','capacity_0p1pct'):
       for strategy in ('5dr','base_combo','added_combo'):expected.add(f'{period}__{strategy}__{scenario}')
      expected.add(f'{period}__5dr_lag1__base')
    a.check('execution_exact_jobs',{r['run'] for r in s['runs']}==expected)
    for row in s['runs']:
      name=row['run']
      try:
        folder=out/'execution'/name;d=frame(folder/'daily.csv');c=load(folder/'config.json');t=load(folder/'terminal.json')
        a.check(name+'.config_same',c==row['config']);a.check(name+'.terminal_same',t==row['terminal'])
        a.check(name+'.fixed_config',c['capital']==1e9 and c['top_n']==100 and c['rebalance']=='weekly' and c['wind_down_days']==20 and c['execution_model']=='daily_conservative' and c['st_policy']=='price_proxy')
        spec=p['execution_periods'][row['period']];want=calendar[(calendar>=spec['start'])&(calendar<=spec['end'])]
        fixed=dict(p['execution_base_config']);fixed.update(start=spec['start'],end=spec['end'])
        cap=.001 if row['scenario']=='capacity_0p1pct' else .01
        fixed.update(max_participation=cap,max_daily_participation=cap)
        a.nested(name+'.all_config_fields',fixed,c)
        a.same(name+'.cost_multiplier',2. if row['scenario']=='double_cost' else 1.,c['cost_multiplier'])
        a.check(name+'.calendar',pd.to_datetime(d.date).tolist()==want.tolist())
        a.check(name+'.nonnegative_cash',bool(np.isfinite(d.cash).all() and (d.cash>=0).all()))
        metrics,r=perf(d,1e9)
        for k,v in metrics.items():a.same(name+'.metric.'+k,v,row['metrics'][k],.001 if k in MONEY else 1e-10)
        a.same(name+'.daily_return',r,d['return'])
        a.same(name+'.writeoff',metrics['writeoff_loss'],t['writeoff_loss'],.001)
        a.same(name+'.residual_positions',int(d.positions.iloc[-1]),t['residual_positions'])
        a.same(name+'.residual_value',float(d.nav.iloc[-1]-d.cash.iloc[-1]),t['residual_value'],.001)
        zero=int(d.positions.iloc[-1])==0
        a.check(name+'.terminal_semantics',t['model_position_zero']==zero and t['fully_liquidated']==zero and t['fully_exited_by_trades']==(zero and metrics['writeoff_loss']==0))
        lookup[(row['period'],row['strategy'],row['scenario'])]=d
      except Exception as exc:a.check(name+'.exception',False,error=repr(exc))
    a.same('six_paired',6,len(s['paired']))
    for pair in s['paired']:
      name=pair['period']+'__'+pair['scenario'];prefix=name+'.paired'
      try:
        b=lookup[(pair['period'],'base_combo',pair['scenario'])];ad=lookup[(pair['period'],'added_combo',pair['scenario'])]
        a.check(prefix+'.calendar',b.date.tolist()==ad.date.tolist());br=b['return'].to_numpy();ar=ad['return'].to_numpy();delta=ar-br
        saved=frame(out/'execution'/f'{name}__paired.csv')
        a.check(prefix+'.saved_calendar',saved.date.tolist()==b.date.tolist())
        for k,v in [('base_return',br),('added_return',ar),('added_minus_base',delta)]:a.same(prefix+'.'+k,v,saved[k])
        lag=min(5,len(delta)-1);kw={'maxlags':lag,'use_correction':False}
        a.same(prefix+'.n_paired_days',len(delta),pair['n_paired_days'])
        a.check(prefix+'.HAC_config',pair['HAC_maxlags']==lag and pair['HAC_use_correction'] is False and pair['HAC_use_t'] is False)
        f=sm.OLS(delta,np.ones((len(delta),1))).fit(cov_type='HAC',cov_kwds=kw,use_t=False)
        rf=sm.OLS(ar,np.column_stack([np.ones(len(br)),br])).fit(cov_type='HAC',cov_kwds=kw,use_t=False)
        a.same(prefix+'.mean',delta.mean(),pair['mean_daily_return_difference'])
        a.same(prefix+'.CI',f.conf_int()[0],pair['HAC_mean_difference_CI95']);a.same(prefix+'.P',f.pvalues[0],pair['HAC_raw_P'])
        sh=lambda x:float(np.mean(x)/np.std(x,ddof=1)*np.sqrt(252)) if np.std(x,ddof=1)>0 else None
        sd=np.std(delta,ddof=1);ir=float(delta.mean()/sd*np.sqrt(252)) if sd>0 else None
        a.same(prefix+'.IR',ir,pair['paired_active_return_annualized_IR_P4_minus_P3'])
        a.same(prefix+'.base_sharpe',sh(br),pair['base_sharpe']);a.same(prefix+'.added_sharpe',sh(ar),pair['added_sharpe'])
        a.same(prefix+'.sharpe_difference',sh(ar)-sh(br),pair['sharpe_difference'])
        rel=pair['relative_three_factor_alpha']
        a.nested(prefix+'.alpha',dict(daily_alpha=float(rf.params[0]),linear_annualized_alpha_252=float(rf.params[0]*252),beta=float(rf.params[1]),HAC_daily_alpha_CI95=rf.conf_int()[0].tolist(),HAC_linear_annualized_alpha_CI95=(rf.conf_int()[0]*252).tolist(),HAC_alpha_raw_P=float(rf.pvalues[0])),rel)
        mdd=lambda x:float(np.min(np.r_[1.,np.cumprod(1+x)]/np.maximum.accumulate(np.r_[1.,np.cumprod(1+x)])-1))
        a.same(prefix+'.base_mdd',mdd(br),pair['base_max_drawdown']);a.same(prefix+'.added_mdd',mdd(ar),pair['added_max_drawdown'])
        a.same(prefix+'.mdd_change',mdd(ar)-mdd(br),pair['paired_drawdown_change_added_minus_base'])
      except Exception as exc:a.check(prefix+'.exception',False,error=repr(exc))
    old=OLD/'research_outputs/v4_20261017_002058/runs/reversal_w005__all_liquid__n100__weekly/evaluation_2025/base/daily.csv'
    new=out/'execution/evaluation_2025__5dr__base/daily.csv'
    try:
        x,y=frame(old),frame(new)
        a.check('previous_2025_daily_exact_columns',x.columns.tolist()==y.columns.tolist())
        a.check('previous_2025_daily_roundtrip_exact',x.equals(y),old_path=str(old))
    except Exception as exc:a.check('previous_2025_daily_exception',False,error=repr(exc))

def source_sample(out,p,a):
    # Eligibility reuses preserved engine only; raw factor/target/groups/regressions below are independent.
    sys.path.insert(0,str(OLD))
    from lab import Market
    from research import base_config
    import pyarrow as pa
    pa.set_cpu_count(1);pa.set_io_thread_count(1)
    m=Market(Path(p['source_identity']['path']));dt=pd.DatetimeIndex(m.dates)
    folder=cohort_folder(out,'evaluation_2025__all_liquid__h05')
    d=frame(folder/'information_daily.csv')
    g=frame(folder/'information_deciles.csv')
    usable=d[d.window_in_period.map(lambda x:x is True or str(x).lower()=='true')]
    chosen=usable.iloc[[0,len(usable)//2,len(usable)-1]]
    mask=m.eligible(base_config(wind_down_days=20))
    # Independently reproduce the declared causal adjusted-price construction.
    # Algebraically cancelling Ffirst before the return calculation is NOT
    # IEEE-equivalent: 1-ulp changes can split/merge exact average-rank ties.
    adj=m.raw['cumadj'];valid_factor=np.isfinite(adj)&(adj>0)
    first_valid=np.argmax(valid_factor,axis=0)
    first_factor=adj[first_valid,np.arange(adj.shape[1])]
    scale=np.where(valid_factor,adj/first_factor[None,:],np.nan)
    close=m.raw['close']*scale;op=m.raw['open']*scale
    symbols=np.asarray(m.symbols,str);sample=[]
    rank=lambda x:pd.Series(x).rank(method='average').to_numpy()
    for _,r in chosen.iterrows():
        t=dt.get_loc(pd.Timestamp(r.signal_date));entry=t+1;ex=t+6
        hist=np.all(np.isfinite(close[t-5:t+1])&(close[t-5:t+1]>0)&np.isfinite(adj[t-5:t+1])&(adj[t-5:t+1]>0),axis=0)
        with np.errstate(all='ignore'):score=-(close[t]/close[t-5]-1);target=op[ex]/op[entry]-1
        sig=mask[t]&hist&np.isfinite(score)
        good=np.all(np.isfinite(op[entry:ex+1])&(op[entry:ex+1]>0)&np.isfinite(adj[entry:ex+1])&(adj[entry:ex+1]>0),axis=0)&np.isfinite(target)
        pair=sig&good;ids=np.flatnonzero(sig);n=len(ids);cp=int(pair.sum());tag='source_sample.'+str(r.signal_date)
        a.same(tag+'.signal_count',n,r.signal_count);a.same(tag+'.paired_count',cp,r.paired_count)
        corr=lambda x,y:float(np.corrcoef(x,y)[0,1]) if len(x)>=30 and np.std(x)>0 and np.std(y)>0 else np.nan
        a.same(tag+'.IC',corr(score[pair],target[pair]),r.ic);a.same(tag+'.RankIC',corr(rank(score[pair]),rank(target[pair])),r.rank_ic)
        pct=np.full(len(symbols),np.nan);pct[ids]=(rank(score[ids])-.5)/n
        beta=np.linalg.lstsq(np.column_stack([np.ones(cp),pct[pair]]),target[pair],rcond=None)[0][-1]
        a.same(tag+'.beta',beta,r.beta);a.same(tag+'.p25p75',beta*.5,r.p25_p75_difference)
        order=ids[np.lexsort((symbols[ids],score[ids]))];saved=g[g.date==r.date].set_index('decile')
        for dec,chunk in enumerate(np.array_split(order,10),1):
            valid=chunk[good[chunk]];sr=saved.loc[dec]
            a.same(tag+f'.q{dec}.assigned',len(chunk),sr.assigned);a.same(tag+f'.q{dec}.covered',len(valid),sr.covered)
            a.same(tag+f'.q{dec}.mean',float(target[valid].mean()) if len(valid) else None,sr.mean_simple_return)
        sample.append(str(r.signal_date))
    return sample

def verify(out):
    out=Path(out).resolve();a=Audit();samples=[]
    try:
        sys.path.insert(0,str(OLD))
        from research import reserve_performance_cores
        affinity=reserve_performance_cores()
        a.check('reserve_two_performance_cores',os.name!='nt' or len(affinity.get('reserved_logical_processors',[]))>=4)
        pending=load(out/'result_pending_validation.json');p=load(out/'protocol.json')
        a.check('pending_protocol_matches',pending['protocol']==p)
        a.check('compute_no_failures',not pending['failures'] and pending['status']=='computed_pending_validation')
        for name,value in p['new_compute_sha256'].items():a.same('new_hash.'+name,value,sha(ROOT/name))
        for name,value in p['reused_V4_compute_sha256'].items():a.same('old_hash.'+name,value,sha(OLD/name))
        src=Path(p['source_identity']['path']);a.same('ZIP_sha256',p['source_identity']['sha256'],sha(src));a.same('ZIP_size',p['source_identity']['size_bytes'],src.stat().st_size)
        # Read source date column independently, without loading full price matrices yet.
        import zipfile
        import pyarrow as pa
        import pyarrow.feather as feather
        pa.set_cpu_count(1);pa.set_io_thread_count(1)
        with zipfile.ZipFile(src) as z:calendar=pd.DatetimeIndex(feather.read_table(pa.BufferReader(z.read('feathers/open.feather')),columns=['date'])['date'].to_numpy())
        a.check('source_calendar_unique',calendar.is_monotonic_increasing and not calendar.has_duplicates)
        audit=load(out/'source_audit.json')
        for name,spec in p['periods'].items():
            dates=calendar[(calendar>=spec['start'])&(calendar<=spec['end'])]
            a.nested('source_calendar.'+name,dict(first_source_date=str(dates[0].date()),last_source_date=str(dates[-1].date()),source_dates=len(dates)),audit['period_calendars'][name])
        information(out,p,a,calendar);execution(out,p,a,calendar);samples=source_sample(out,p,a)
    except Exception as exc:a.check('verification_exception',False,error=repr(exc),traceback=traceback.format_exc())
    passed=bool(a.checks) and all(c['passed'] for c in a.checks)
    report=dict(passed=passed,generated_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),validator_sha256=sha(__file__),
       checks=a.checks,check_count=len(a.checks),failed_count=sum(not c['passed'] for c in a.checks),
       money_absolute_tolerance=.001,ratio_absolute_tolerance=1e-10,
       scope=['All 36 persisted information cohorts: daily calendars/counts/group identities, aggregate and monthly mean/std/HAC/CI/raw P including source-grid gaps.',
              'All 20 persisted execution daily ledgers: 13 performance metrics, returns, cash and terminal/writeoff semantics; six paired return/relative-alpha calculations.',
              'Original ZIP independent factor/target/IC/ranks/groups/OLS recomputation on first/middle/last valid target dates of 2025 all_liquid h5 only.',
              'Exact roundtrip daily ledger continuity with preserved 2025 V4 5DR baseline; code and ZIP hash identity.'],
       source_recomputed_signal_dates=samples,
       source_sample_price_construction='Independent raw_price * (cumadj / first_valid_positive_cumadj), preserving declared causal normalization and IEEE operation order; no algebraic cancellation before ranks.',
       limitations=['No full stock-day independent information recalculation; conditional beta verified as persisted daily series and inference only.',
                    'Source sample eligibility reuses preserved old Market.eligible; factor and target formulas independently recomputed.',
                    'No independent full per-trade cash/position/price/fee/capacity reconstruction; daily-ledger verification does not prove ledger generation or real execution.',
                    'No claim of untouched out-of-sample data, actual historical ST exclusion, opening-auction feasibility or physical share settlement.'])
    first_attempt=out/'validation.first_attempt.json'
    investigation=ROOT/'normalization_investigation.json'
    if first_attempt.exists():
        report['prior_validation_attempt']={'path':str(first_attempt),'sha256':sha(first_attempt),'preserved':True}
    if investigation.exists():
        report['normalization_investigation']={'path':str(investigation),'sha256':sha(investigation),
            'correction':'Validator previously used raw*cumadj; repaired to independently compute declared raw*(cumadj/first_valid_factor). No computation, data, saved statistics, or tolerance changes.'}
    (out/'validation.json').write_text(json.dumps(clean(report),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    if passed:
        final=dict(pending);final['status']='validated';final['independent_validation']=report
        (out/'result.json').write_text(json.dumps(clean(final),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    elif (out/'result.json').exists():
        # An existing previously accepted result is preserved, but current failure forbids reporting it as current.
        report['existing_result_warning']='Existing result.json is stale under this failed validation; do not report it.'
        (out/'validation.json').write_text(json.dumps(clean(report),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'passed':passed,'check_count':len(a.checks),'failed_count':report['failed_count'],'validation':str(out/'validation.json')},ensure_ascii=False))
    return passed

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output');args=parser.parse_args()
    sys.exit(0 if verify(args.output) else 1)
