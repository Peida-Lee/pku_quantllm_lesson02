"""Fixed five-dimensional 5DR re-evaluation; no search or external writes."""
import os
for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS'):
    os.environ[k] = '1'
import argparse
import hashlib
import json
import platform
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pyarrow as pa

ROOT = Path(__file__).resolve().parent
OLD = ROOT.parent / 'daily_factor_lab_20261016'
SOURCE = ROOT.parent / 'feathers.zip'
sys.path.insert(0, str(OLD))
from lab import Market
from research import base_config, UNIVERSES, reserve_performance_cores
from v4_factors import FactorEngine
from information_evaluation import evaluate_information
from execution_evaluation import run_execution
pa.set_cpu_count(1)
pa.set_io_thread_count(1)

PERIODS = {
    'reference_2024': {'start':'2024-01-02','end':'2024-12-31','role':'previously_observed_reference'},
    'evaluation_2025': {'start':'2025-01-02','end':'2025-12-31','role':'primary_retrospective'},
    'evaluation_2026sep': {'start':'2026-01-05','end':'2026-09-30','role':'previously_observed_fixed_extension'},
}
HORIZONS = (1,5,10,20)

def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()

def clean(v):
    if isinstance(v,dict): return {str(k):clean(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)): return [clean(x) for x in v]
    if isinstance(v,np.generic): return clean(v.item())
    if isinstance(v,float) and not np.isfinite(v): return None
    return v

def save(p,v):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    temp=p.with_name(p.name+'.writing')
    temp.write_text(json.dumps(clean(v),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temp.replace(p)

def digest(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def run(reuse_information=None):
    affinity=reserve_performance_cores()
    if os.name=='nt' and len(affinity.get('reserved_logical_processors',[]))<4:
        raise RuntimeError('Cannot verify reservation of two performance cores')
    started=time.monotonic()
    out=ROOT / ('evaluation_'+datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d_%H%M%S'))
    out.mkdir(exist_ok=False)
    previous=json.loads((OLD/'research_outputs/v4_20261017_002058/protocol.json').read_text(encoding='utf-8'))
    old_hashes={k:digest(OLD/k) for k in previous['code_sha256']}
    if old_hashes != previous['code_sha256']:
        raise RuntimeError('The six reused execution/factor sources differ from the preserved V4 version')
    source={'path':str(SOURCE),'size_bytes':SOURCE.stat().st_size,'sha256':digest(SOURCE)}
    if source != previous['source_identity']:
        raise RuntimeError('Original ZIP differs from the previously evaluated source')
    computation={p.name:digest(p) for p in (ROOT/'run_evaluation.py',ROOT/'information_evaluation.py',ROOT/'execution_evaluation.py')}
    protocol={
        'run_id':out.name,'frozen_at':now(),'timezone':'Asia/Shanghai',
        'task_source':'https://github.com/aslan9/pku_quantllm/issues/9',
        'issue_body_status':'No description provided when actually read through Edge on 2026-10-23',
        'source_identity':source,'reused_V4_compute_sha256':old_hashes,'new_compute_sha256':computation,
        'periods':PERIODS,'primary_period':'evaluation_2025','primary_universe':'all_liquid','primary_horizon':5,'horizons':HORIZONS,
        'factor':'R5=C_adj[t]/C_adj[t-5]-1; main score=-R5; full six-row historical price path',
        'target':'O_adj[t+1+h]/O_adj[t+1]-1; entry/exit in period, signal may precede start; full open/factor path',
        'direction':'Fixed reversal interpretation from previous exercise; raw R5 signs also disclosed, no new direction selection',
        'information_universes':UNIVERSES,'information_min_pairs':30,
        'HAC':'Information Bartlett L=max(5,h), finite-observation n/(n-1) correction; h5 L10/L20 sensitivities. Portfolio paired/relative-alpha L5, no small-sample correction. Both normal approximate, raw two-sided P only.',
        'missingness':'Keep source-date positions; no per-stock valid-date compression; boundary censoring distinct from in-period missing labels. Sorting/groups use signal-known data before observing labels.',
        'execution_base_config':asdict(base_config(wind_down_days=20)),
        'execution_periods':{k:v for k,v in PERIODS.items() if k!='reference_2024'},
        'execution_expected_simulations':20,
        'combination':'Same four-component past-finite pool; P3=equal ranks(momentum20,lowvol20,liquidity20); P4=equal ranks(same three,-R5); original weights also change.',
        'increment':'IR(P4-P3) and net Sharpe difference; not a common-benchmark delta-IR. Alpha means intercept of daily net rP4 on rP3, not CAPM/official risk alpha.',
        'resource_policy':affinity,'numeric_and_arrow_threads':1,
        'versions':{'python':platform.python_version(),'numpy':np.__version__,'pandas':pd.__version__,'pyarrow':pa.__version__},
        'boundaries':['All periods previously observed; no untouched out-of-sample claim.',
            'Historical ST truth unavailable; price_proxy is not accurate ST exclusion.',
            'Daily OHLC/amount and action rejection are post-session audits, not opening-auction feasibility proof.',
            'Decile Q10-Q1 is an overlapping-window gross diagnostic, not an executable short portfolio.',
            'Synthetic total-return units do not establish physical dividend/share settlement.',
            'Industry, capitalization, official risk factors, investable index and cash interest are not available.',
            'Fixed wind-down20 is inherited from earlier retrospective exit repair; no new parameter optimization.',
            'No external posting/upload is performed by this program.'],
        'failure_policy':'Record failed information cohort or execution scenario and continue remaining jobs.',
    }
    reuse_path=None
    if reuse_information:
        reuse_path=Path(reuse_information).resolve()
        if reuse_path.parent!=ROOT or not reuse_path.name.startswith('evaluation_'):
            raise ValueError('Reuse only a previous evaluation directory in this project')
        prior=json.loads((reuse_path/'protocol.json').read_text(encoding='utf-8'))
        if (prior['source_identity']!=source or prior['reused_V4_compute_sha256']!=old_hashes or
            prior['new_compute_sha256']['information_evaluation.py']!=computation['information_evaluation.py'] or
            prior['periods']!=PERIODS or list(prior['horizons'])!=list(HORIZONS)):
            raise ValueError('Cached information source, calculation or fixed grid differs')
        protocol['reuse_information_from']=reuse_path.name
        protocol['reuse_scope']='Only completed aggregate information cohorts with unchanged input/metrics code. Execution is newly run after queue-guard repair.'
    save(out/'protocol.json',protocol)
    failures=[];completed=0
    def progress(msg):
        state={'run_id':out.name,'updated_at':now(),'elapsed_seconds':time.monotonic()-started,'completed_information_cohorts':completed,'planned_information_cohorts':36,'message':msg}
        save(out/'progress.json',state)
        print(json.dumps(clean(state),ensure_ascii=False),flush=True)
    progress('loading fixed original ZIP')
    market=Market(SOURCE)
    pa.set_cpu_count(1);pa.set_io_thread_count(1)
    calendar={}
    for name,spec in PERIODS.items():
        rows=market.dates[(market.dates>=spec['start'])&(market.dates<=spec['end'])]
        calendar[name]={'first_source_date':str(rows[0].date()),'last_source_date':str(rows[-1].date()),'source_dates':len(rows)}
        if calendar[name]['first_source_date']!=spec['start'] or calendar[name]['last_source_date']!=spec['end']:
            raise RuntimeError('Declared period boundaries do not match actual source trading dates: '+name)
    save(out/'source_audit.json',{'dates':len(market.dates),'symbols':len(market.symbols),'first':str(market.dates[0].date()),'last':str(market.dates[-1].date()),'period_calendars':calendar,'client_date':'2026-10-23','data_not_updated_beyond_source':True,'historical_ST_truth_coverage':0})
    engine=FactorEngine(market,max_cache=0)
    score=engine.compute('reversal_w005').copy()
    controls={k:engine.compute(fid).copy() for k,fid in {'momentum20':'momentum_w020','lowvol20':'lowvol_w020','liquidity20':'liquidity_w020'}.items()}
    engine.clear()
    masks={}
    for name,pool in UNIVERSES.items():
        cfg=base_config(wind_down_days=20,universe=pool['universe'],liquidity_top_n=pool['liquidity_top_n'])
        masks[name]=market.eligible(cfg)
    coverage={}
    for name,mask in masks.items():
        current=mask & np.isfinite(market.factors['reversal5'])
        full=mask & np.isfinite(score)
        coverage[name]={'endpoint_only_factor_stock_dates':int(current.sum()),'complete_path_factor_stock_dates':int(full.sum()),'removed_incomplete_historical_paths':int(np.count_nonzero(current&~full))}
    save(out/'signal_path_audit.json',coverage)
    merged={'periods':{},'cohort_failures':[]};frames={'information_daily':[],'information_deciles':[],'information_monthly':[]};reused=[]
    for period,spec in PERIODS.items():
        for universe,mask in masks.items():
            for h in HORIZONS:
                cohort=f'{period}__{universe}__h{h:02d}'
                folder=out/'information'/'cohorts'/cohort
                try:
                    prior_folder=reuse_path/'information'/'cohorts'/cohort if reuse_path else None
                    if prior_folder and all((prior_folder/n).is_file() for n in ('summary.json','information_daily.csv','information_deciles.csv','information_monthly.csv')):
                        folder=prior_folder
                        item=json.loads((folder/'summary.json').read_text(encoding='utf-8'))
                        reused.append({'cohort':cohort,'directory':str(folder.relative_to(ROOT)),'mode':'read original complete aggregate files; no transfer or re-simulation'})
                    else:
                        item=evaluate_information(market,score,{universe:mask},controls,{period:spec},folder,horizons=(h,))
                    merged['method']=item['method']
                    merged['periods'].setdefault(period,{}).setdefault(universe,{})[str(h)]=item['periods'][period][universe][str(h)]
                    for name in frames:
                        path=folder/(name+'.csv')
                        if path.stat().st_size>1:
                            try:frames[name].append(pd.read_csv(path,float_precision='round_trip'))
                            except pd.errors.EmptyDataError:pass
                except Exception as exc:
                    failure={'phase':'information','cohort':cohort,'error':str(exc)}
                    failures.append(failure);merged['cohort_failures'].append(failure)
                    save(out/'failures'/f'{cohort}.json',{'failure':failure,'traceback':traceback.format_exc()})
                completed+=1;progress(cohort)
    for name,parts in frames.items():
        if parts:pd.concat(parts,ignore_index=True).to_csv(out/'information'/(name+'.csv'),index=False,float_format='%.17g')
    save(out/'information'/'summary.json',merged)
    save(out/'information'/'reused_cohorts.json',reused)
    execution=None
    try:
        execution=run_execution(market,out/'execution',periods=protocol['execution_periods'],progress=progress)
        failures.extend(execution['failures'])
    except Exception as exc:
        failures.append({'phase':'execution','error':str(exc)})
        save(out/'failures'/'execution_phase.json',{'error':str(exc),'traceback':traceback.format_exc()})
    result={'run_id':out.name,'generated_at':now(),'status':'computed_pending_validation' if not failures else 'partial_pending_validation','protocol':protocol,'source_calendars':calendar,'information':merged,'execution':execution,'failures':failures,'independent_validation':{'status':'not_run'}}
    save(out/'result_pending_validation.json',result)
    save(ROOT/'latest_computed.json',{'run_id':out.name})
    progress('computation complete; independent validation still required')
    print('COMPUTED '+str(out),flush=True)
    return out

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--reuse-information-from',type=Path)
    args=parser.parse_args()
    run(args.reuse_information_from)
