"""Prespecified universe contrasts, referencing accepted all_liquid without rerun."""
import os
for key in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS'):
    os.environ[key]='1'
import hashlib
import json
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import execution_evaluation as execution
from lab import Market
from research import base_config, UNIVERSES, reserve_performance_cores
from v4_factors import FactorEngine

ROOT=Path(__file__).resolve().parent
BASE=ROOT/'evaluation_20261023_200711'
SOURCE=ROOT.parent/'feathers.zip'
POOLS=('main_liquid','liquid1000')

def sha(path):
    with Path(path).open('rb') as f: return hashlib.file_digest(f,'sha256').hexdigest()

def save(path,value):
    execution._json(path,value)

def run():
    started=time.monotonic()
    affinity=reserve_performance_cores()
    if os.name=='nt' and len(affinity.get('reserved_logical_processors',[]))<4:
        raise RuntimeError('Cannot verify reservation of two performance cores')
    previous=json.loads((BASE/'protocol.json').read_text(encoding='utf-8'))
    checks={}
    expected_source=previous['source_identity']
    checks['source']=sha(SOURCE)
    if checks['source']!=expected_source['sha256'] or SOURCE.stat().st_size!=expected_source['size_bytes']:
        raise RuntimeError('Source mismatch')
    for name,expected in previous['reused_V4_compute_sha256'].items():
        checks['old/'+name]=sha(execution.OLD/name)
        if checks['old/'+name]!=expected: raise RuntimeError('Frozen old compute mismatch: '+name)
    for name,expected in previous['new_compute_sha256'].items():
        checks[name]=sha(ROOT/name)
        if checks[name]!=expected: raise RuntimeError('Frozen compute mismatch: '+name)
    out=ROOT/('pool_sensitivity_'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S'))
    out.mkdir(exist_ok=False)
    periods=previous['execution_periods']
    protocol={'frozen_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'timezone':'Asia/Shanghai',
        'source_identity':expected_source,'verified_compute_sha256':checks,'pool_sensitivity_sha256':sha(__file__),
        'all_liquid_reference':str(BASE/'execution/execution_summary.json'),
        'all_liquid_reference_sha256':sha(BASE/'execution/execution_summary.json'),
        'all_liquid_policy':'reference existing 20 runs; never copied or rerun',
        'pools':{p:UNIVERSES[p] for p in POOLS},'periods':periods,'expected_new_simulations':40,
        'execution_base_config':previous['execution_base_config'], 'resource_policy':affinity,'numeric_and_arrow_threads':1,
        'fixed_design':'Only universe and liquidity_top_n vary via research.UNIVERSES. Same 20 runs per pool. Same pool-specific current legal finite common ranks for P3/P4. No tuning.',
        'implementation':'Call frozen run_execution with runtime base_config wrapper injecting fixed pool universe/liquidity_top_n, restored after each pool. No source edits.',
        'counts_definition':'Signal-close dates inside declared interval. Entries/exits compare current eligible with immediately previous source date, including before interval. all_liquid_intersection is same-day overlap. five_dr_available is eligible AND finite reversal_w005.',
        'status':'frozen_before_computation'}
    save(out/'protocol.json',protocol)
    market=Market(SOURCE)
    execution.pa.set_cpu_count(1);execution.pa.set_io_thread_count(1)
    all_eligible=market.eligible(base_config(wind_down_days=20))
    engine=FactorEngine(market,max_cache=0)
    five_finite=np.isfinite(engine.compute('reversal_w005'));engine.clear()
    result={'status':'computing','all_liquid_reference':protocol['all_liquid_reference'],'pools':{},'failures':[]}
    original=execution.base_config
    for pool in POOLS:
        folder=out/pool;folder.mkdir()
        options=UNIVERSES[pool]
        def pool_config(**kw):
            return base_config(**{**kw,'universe':options['universe'],'liquidity_top_n':options['liquidity_top_n']})
        try:
            eligible=market.eligible(pool_config(wind_down_days=20))
            records=[];count_summary={}
            for period,spec in periods.items():
                indices=np.flatnonzero((market.dates>=spec['start'])&(market.dates<=spec['end']))
                for i in indices:
                    prev=eligible[i-1] if i>0 else np.zeros(eligible.shape[1],dtype=bool)
                    records.append({'period':period,'date':market.dates[i].strftime('%Y-%m-%d'),
                        'eligible_count':int(eligible[i].sum()),'entries':int((eligible[i]&~prev).sum()),
                        'exits':int((~eligible[i]&prev).sum()),
                        'all_liquid_intersection':int((eligible[i]&all_eligible[i]).sum()),
                        'all_liquid_count':int(all_eligible[i].sum()),
                        'five_dr_available':int((eligible[i]&five_finite[i]).sum())})
                frame=pd.DataFrame([r for r in records if r['period']==period])
                count_summary[period]={'days':len(frame),'actual_start':frame.date.iloc[0],'actual_end':frame.date.iloc[-1],
                    **{col:{'min':int(frame[col].min()),'median':float(frame[col].median()),'max':int(frame[col].max()),'sum':int(frame[col].sum())}
                       for col in ('eligible_count','entries','exits','all_liquid_intersection','all_liquid_count','five_dr_available')}}
            pd.DataFrame(records).to_csv(folder/'pool_counts.csv',index=False,float_format='%.17g')
            save(folder/'pool_counts_summary.json',count_summary)
            execution.base_config=pool_config
            summary=execution.run_execution(market,folder/'execution',periods=periods,
                progress=lambda event: save(out/'progress.json',{'pool':pool,**event}))
            result['pools'][pool]={'execution_summary':str(folder/'execution/execution_summary.json'),
                                  'pool_counts_summary':count_summary,'completed_simulations':summary['completed_simulations'],
                                  'failures':summary['failures'],
                                  'base_5dr':[r for r in summary['runs'] if r['strategy']=='5dr' and r['scenario']=='base']}
        except Exception as exc:
            result['failures'].append({'pool':pool,'error':repr(exc)})
        finally:
            execution.base_config=original
        save(out/'result_pending_validation.json',result)
    result['status']='computed_pending_validation'
    result['elapsed_seconds']=time.monotonic()-started
    save(out/'result_pending_validation.json',result)
    print(json.dumps({'output':str(out),'status':result['status'],'elapsed_seconds':result['elapsed_seconds'],'failures':result['failures']},ensure_ascii=False),flush=True)
    return result

if __name__=='__main__':run()
