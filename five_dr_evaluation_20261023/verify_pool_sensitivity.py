"""Independent supplemental pool audit; never runs original information verification.
Run after calculation: python verify_pool_sensitivity.py SUPPLEMENT_OUTPUT
Daily execution checks are copied from the accepted independent verifier, not simulator.
"""
import os
for _k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS'):os.environ[_k]='1'
import argparse,json,sys,traceback
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np,pandas as pd
import statsmodels.api as sm
from verify_evaluation import Audit,load,frame,sha,clean,perf,MONEY,ROOT,OLD

class Prefixed:
    def __init__(self,a,prefix):self.a,self.prefix=a,prefix
    def check(self,name,*args,**kw):self.a.check(self.prefix+name,*args,**kw)
    def same(self,name,*args,**kw):self.a.same(self.prefix+name,*args,**kw)
    def nested(self,name,*args,**kw):self.a.nested(self.prefix+name,*args,**kw)

def audit_execution(out,p,a,calendar,pool):
    s=load(out/'execution/execution_summary.json');a.check('execution_no_failures',not s['failures'])
    a.same('20_completed',20,s.get('completed_simulations'));a.same('20_run_records',20,len(s['runs']))
    lookup={};expected=set()
    for period in p['periods']:
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
        spec=p['periods'][row['period']];want=calendar[(calendar>=spec['start'])&(calendar<=spec['end'])]
        fixed=dict(p['execution_base_config']);fixed.update(start=spec['start'],end=spec['end'],universe=p['pools'][pool]['universe'],liquidity_top_n=p['pools'][pool]['liquidity_top_n'])
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

def pool_counts(out,p,a,market):
    # Eligibility policy is reused explicitly, not represented as independent implementation.
    from research import base_config
    dates=pd.DatetimeIndex(market.dates)
    all_mask=market.eligible(base_config(wind_down_days=20))
    f=market.raw['cumadj'];valid=np.isfinite(f)&(f>0);first=np.argmax(valid,axis=0)
    scale=np.where(valid,f/f[first,np.arange(f.shape[1])][None,:],np.nan)
    c=market.raw['close']*scale
    closevalid=np.isfinite(c)&(c>0)
    # Independent full six-source-row historical availability, no factor-engine calls.
    bad=np.vstack([np.zeros((1,c.shape[1]),dtype=np.int32),np.cumsum(~closevalid,axis=0,dtype=np.int32)])
    finite=np.zeros_like(closevalid)
    with np.errstate(all='ignore'):
        finite[5:]=(bad[6:]==bad[:-6])&np.isfinite(-(c[5:]/c[:-5]-1))
    samples=[]
    columns=('eligible_count','entries','exits','all_liquid_intersection','all_liquid_count','five_dr_available')
    for pool,options in p['pools'].items():
      try:
        folder=out/pool;d=frame(folder/'pool_counts.csv');summary=load(folder/'pool_counts_summary.json')
        cfg=base_config(wind_down_days=20,universe=options['universe'],liquidity_top_n=options['liquidity_top_n'])
        eligible=market.eligible(cfg)
        want=[]
        for period,spec in p['periods'].items():
            indices=np.flatnonzero((dates>=spec['start'])&(dates<=spec['end']))
            want.extend((period,str(dates[i].date())) for i in indices)
            sub=d[d.period==period]
            a.check(pool+'.counts_calendar.'+period,sub.date.tolist()==[str(dates[i].date()) for i in indices])
            expected={'days':len(sub),'actual_start':sub.date.iloc[0],'actual_end':sub.date.iloc[-1]}
            for col in columns:expected[col]={'min':int(sub[col].min()),'median':float(sub[col].median()),'max':int(sub[col].max()),'sum':int(sub[col].sum())}
            a.nested(pool+'.counts_summary.'+period,expected,summary[period])
            a.check(pool+'.count_bounds.'+period,bool(((sub.eligible_count>=sub.five_dr_available)&(sub.eligible_count>=sub.all_liquid_intersection)&(sub.all_liquid_count>=sub.all_liquid_intersection)&(sub[list(columns)]>=0).all(axis=1)).all()))
            a.check(pool+'.counts_integer.'+period,np.array_equal(sub[list(columns)].to_numpy(),sub[list(columns)].to_numpy().astype(np.int64)))
            if pool=='liquid1000':a.check(pool+'.top1000_bound.'+period,bool((sub.eligible_count<=1000).all()))
            # Entire count series must obey adjacent-date set cardinality identity.
            a.same(pool+'.entry_exit_identity.'+period,np.diff(sub.eligible_count.to_numpy()),(sub.entries-sub.exits).to_numpy()[1:])
            for i in indices[[0,len(indices)//2,-1]]:
                day=str(dates[i].date());r=sub[sub.date==day].iloc[0]
                prev=eligible[i-1] if i else np.zeros(eligible.shape[1],bool)
                e=eligible[i]
                actual=dict(eligible_count=int(e.sum()),entries=int((e&~prev).sum()),exits=int((~e&prev).sum()),all_liquid_intersection=int((e&all_mask[i]).sum()),all_liquid_count=int(all_mask[i].sum()),five_dr_available=int((e&finite[i]).sum()))
                a.nested(pool+'.sample_counts.'+day,actual,r.to_dict());samples.append({'pool':pool,'period':period,'date':day})
        a.check(pool+'.exact_count_rows',list(zip(d.period,d.date))==want)
        a.check(pool+'.summary_pending_consistency',summary==load(out/'result_pending_validation.json')['pools'][pool]['pool_counts_summary'])
      except Exception as exc:a.check(pool+'.counts_exception',False,error=repr(exc),traceback=traceback.format_exc())
    return samples

def verify(out):
    out=Path(out).resolve();a=Audit();samples=[]
    try:
        sys.path.insert(0,str(OLD))
        from research import reserve_performance_cores,UNIVERSES
        affinity=reserve_performance_cores()
        a.check('reserve_two_performance_cores',os.name!='nt' or len(affinity.get('reserved_logical_processors',[]))>=4)
        p=load(out/'protocol.json');pending=load(out/'result_pending_validation.json')
        a.check('computed_no_global_failures',pending['status']=='computed_pending_validation' and not pending['failures'])
        a.check('exact_two_supplemental_pools',set(p['pools'])=={'main_liquid','liquid1000'} and set(pending['pools'])==set(p['pools']))
        a.same('planned_40_new_runs',40,p['expected_new_simulations'])
        a.same('pool_script_hash',p['pool_sensitivity_sha256'],sha(ROOT/'pool_sensitivity.py'))
        for name,value in p['verified_compute_sha256'].items():
            if name=='source':target=Path(p['source_identity']['path'])
            elif name.startswith('old/'):target=OLD/name[4:]
            else:target=ROOT/name
            a.same('compute_hash.'+name,value,sha(target))
        src=Path(p['source_identity']['path'])
        a.same('source_hash',p['source_identity']['sha256'],sha(src));a.same('source_size',p['source_identity']['size_bytes'],src.stat().st_size)
        reference=Path(p['all_liquid_reference']);base=reference.parent.parent
        a.same('reference_summary_hash',p['all_liquid_reference_sha256'],sha(reference))
        basevalidation=load(base/'validation.json');baseresult=load(base/'result.json');baseprotocol=load(base/'protocol.json')
        a.check('reference_previous_independent_validation',basevalidation['passed'] is True and baseresult['independent_validation']['passed'] is True and baseresult['status']=='validated')
        a.check('same_base_source',p['source_identity']==baseprotocol['source_identity'])
        a.check('same_base_periods',p['periods']==baseprotocol['execution_periods'])
        a.check('same_base_config',p['execution_base_config']==baseprotocol['execution_base_config'])
        expected_hashes={'source':baseprotocol['source_identity']['sha256'],
            **{'old/'+k:v for k,v in baseprotocol['reused_V4_compute_sha256'].items()},
            **baseprotocol['new_compute_sha256']}
        a.check('exact_frozen_compute_inventory',p['verified_compute_sha256']==expected_hashes)
        a.check('reference_path_consistent',pending['all_liquid_reference']==p['all_liquid_reference'])
        for pool,options in p['pools'].items():a.check('pool_options.'+pool,options==UNIVERSES[pool])
        import pyarrow as pa
        pa.set_cpu_count(1);pa.set_io_thread_count(1)
        from lab import Market
        market=Market(src);calendar=pd.DatetimeIndex(market.dates)
        a.check('source_calendar',calendar.is_monotonic_increasing and not calendar.has_duplicates)
        for pool in p['pools']:
            try:
                entry=pending['pools'][pool]
                a.check(pool+'.no_recorded_failures',not entry['failures'])
                a.same(pool+'.completed_20',20,entry['completed_simulations'])
                a.check(pool+'.summary_path',Path(entry['execution_summary']).resolve()==(out/pool/'execution/execution_summary.json').resolve())
                audit_execution(out/pool,p,Prefixed(a,pool+'.'),calendar,pool)
                summary=load(out/pool/'execution/execution_summary.json')
                a.check(pool+'.pending_base5dr_same',entry['base_5dr']==[r for r in summary['runs'] if r['strategy']=='5dr' and r['scenario']=='base'])
            except Exception as exc:a.check(pool+'.execution_exception',False,error=repr(exc),traceback=traceback.format_exc())
        samples=pool_counts(out,p,a,market)
    except Exception as exc:a.check('verification_exception',False,error=repr(exc),traceback=traceback.format_exc())
    passed=bool(a.checks) and all(c['passed'] for c in a.checks)
    report={'passed':passed,'generated_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),'validator_sha256':sha(__file__),
      'shared_independent_verifier_sha256':sha(ROOT/'verify_evaluation.py'),'checks':a.checks,'check_count':len(a.checks),'failed_count':sum(not c['passed'] for c in a.checks),
      'money_absolute_tolerance':.001,'ratio_absolute_tolerance':1e-10,'sampled_pool_counts':samples,
      'scope':['40 supplemental simulation daily ledgers: all 13 performance metrics, daily returns, cash, exact configurations/calendar and terminal/writeoff semantics.',
               '12 supplemental paired comparisons: saved daily differences, net IR/Sharpe/drawdown, HAC mean differences and relative-three-factor alpha.',
               'Pool count full calendars/integer bounds/cardinality transitions and saved descriptive summaries; source count recomputation at first/middle/last date per period per pool.',
               'Source/code hashes and reference accepted all_liquid result identity; original 13830 checks and original 20 simulations are not rerun.'],
      'limitations':['No independent full per-trade cash/position/price/fee/capacity reconstruction.',
                     'Pool count sample eligibility reuses frozen Market.eligible, explicitly not an independent policy implementation; 5DR complete-path availability independently constructed.',
                     'Pool changes alter universe membership and all within-pool ranks; effects are not isolated industry/size causal effects.',
                     'Previously observed retrospective sensitivity only; historical ST truth and opening auction fills remain unavailable.']}
    (out/'validation.json').write_text(json.dumps(clean(report),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    if passed:
        final=dict(pending);final['status']='validated';final['independent_validation']=report
        (out/'result.json').write_text(json.dumps(clean(final),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    print(json.dumps({'passed':passed,'check_count':len(a.checks),'failed_count':report['failed_count'],'validation':str(out/'validation.json')},ensure_ascii=False))
    return passed

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output');args=parser.parse_args()
    sys.exit(0 if verify(args.output) else 1)
