"""Focused evidence for first independent-verification failure; no research rerun."""
import os
for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS'):os.environ[k]='1'
import json,sys
from pathlib import Path
import numpy as np,pandas as pd
ROOT=Path(__file__).resolve().parent;OLD=ROOT.parent/'daily_factor_lab_20261016';sys.path.insert(0,str(OLD))
from lab import Market
from research import base_config,reserve_performance_cores
from v4_factors import FactorEngine
import verify_evaluation as v

def main():
    reserve_performance_cores();m=Market(ROOT.parent/'feathers.zip');dates=pd.DatetimeIndex(m.dates)
    f=m.raw['cumadj'];ok=np.isfinite(f)&(f>0);first=np.argmax(ok,axis=0)
    base=f[first,np.arange(f.shape[1])];scale=np.where(ok,f/base[None,:],np.nan)
    normalized=m.raw['close']*scale;normalized_open=m.raw['open']*scale
    unnormalized=m.raw['close']*f;unnormalized_open=m.raw['open']*f
    score=FactorEngine(m,max_cache=0).compute('reversal_w005');mask=m.eligible(base_config(wind_down_days=20))
    out=ROOT/'evaluation_20261023_200711';folder=v.cohort_folder(out,'evaluation_2025__all_liquid__h05')
    d=v.frame(folder/'information_daily.csv');rows=[];rank=lambda a:pd.Series(a).rank(method='average').to_numpy()
    for day in ('2024-12-31','2025-07-02','2025-12-23'):
        t=dates.get_loc(day);saved=d[d.signal_date==day].iloc[0];entry=t+1;ex=t+6
        good=np.all(np.isfinite(normalized_open[entry:ex+1])&(normalized_open[entry:ex+1]>0)&np.isfinite(f[entry:ex+1])&(f[entry:ex+1]>0),axis=0)
        hist=np.all(np.isfinite(normalized[t-5:t+1])&(normalized[t-5:t+1]>0),axis=0);sig=mask[t]&hist
        pair=sig&good;rec={'signal_date':day,'saved':{k:float(saved[k]) for k in ('ic','rank_ic','beta','p25_p75_difference')},'n_signals':int(sig.sum()),'n_pairs':int(pair.sum())}
        ranks=[]
        for name,c,o in [('unnormalized',unnormalized,unnormalized_open),('causal_first_factor_normalized',normalized,normalized_open)]:
            x=-(c[t]/c[t-5]-1);y=o[ex]/o[entry]-1;ids=np.flatnonzero(sig);pct=np.full(len(x),np.nan);pct[ids]=(rank(x[ids])-.5)/len(ids)
            beta=float(np.linalg.lstsq(np.column_stack([np.ones(pair.sum()),pct[pair]]),y[pair],rcond=None)[0][-1])
            rec[name]={'IC':float(np.corrcoef(x[pair],y[pair])[0,1]),'RankIC':float(np.corrcoef(rank(x[pair]),rank(y[pair]))[0,1]),'beta':beta,'p25p75':beta*.5,'unique_signal_scores':int(len(np.unique(x[sig])))}
            ranks.append((x,y,rank(x[sig]),rank(y[pair])))
        x0,y0,r0,q0=ranks[0];x1,y1,r1,q1=ranks[1]
        changes=np.flatnonzero(r0!=r1);ids=np.flatnonzero(sig)
        rec['normalization_evidence']={'source_price_exactly_matches_Market':bool(np.array_equal(normalized,m.close,equal_nan=True)),'source_open_exactly_matches_Market':bool(np.array_equal(normalized_open,m.open,equal_nan=True)),
             'independent_score_exactly_matches_engine_on_pool':bool(np.array_equal(x1[sig],score[t,sig])),
             'max_absolute_score_difference':float(np.max(abs(x0[sig]-x1[sig]))),'score_rank_changed_names':int(len(changes)),'target_rank_changed_names':int(np.count_nonzero(q0!=q1)),
             'changed_rank_examples':[{'symbol':str(m.symbols[ids[k]]),'raw_times_factor_score':float(x0[ids[k]]),'causally_normalized_score':float(x1[ids[k]]),'raw_rank':float(r0[k]),'normalized_rank':float(r1[k])} for k in changes[:8]]}
        rows.append(rec)
    # Minimal deterministic tie example: economically equal return, different price-scaling rounding.
    examples=[]
    for p0 in (1.01,2.1,3.7,10.1,100.1):
      for p1 in (1.03,2.3,3.9,10.3,100.3):
       for b in (.1,.3,.7,1.1,3.3):
        a=p1/p0-1;z=(p1/b)/(p0/b)-1
        if a!=z:examples.append({'p0':p0,'p1':p1,'first_factor':b,'return_unscaled':a,'return_scaled':z,'difference':z-a})
    result={'purpose':'Investigate first validation failure without relaxing tolerance or changing frozen calculations','declared_price_construction':'raw_price * (cumadj / first_valid_positive_cumadj), preserving causal per-symbol first-factor normalization and IEEE operation order','rows':rows,'minimal_rounding_examples':examples[:3],'first_validation_preserved':(out/'validation.first_attempt.json').exists()}
    (ROOT/'normalization_investigation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
