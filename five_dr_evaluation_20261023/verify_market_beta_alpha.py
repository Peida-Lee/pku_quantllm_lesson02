"""Independent ZIP-to-proxy rebuild and all 219 OLS/HAC audits. No compute imports."""
import os
for k in ('OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','ARROW_NUM_THREADS'):os.environ[k]='1'
import sys,json,hashlib,zipfile,math
from pathlib import Path
import numpy as np,pandas as pd,pyarrow as pa,pyarrow.feather as feather
from scipy.stats import norm
import statsmodels.api as sm
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT.parent/'daily_factor_lab_20261016'))
from research import reserve_performance_cores
pa.set_cpu_count(1);pa.set_io_thread_count(1)
def load(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def frame(p):return pd.read_csv(p,float_precision='round_trip')
def sha(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
checks=[]
def check(name,ok,**details):checks.append(dict(name=name,passed=bool(ok),**details))
def same(name,a,b,tol=1e-10):
    a,b=np.asarray(a,float),np.asarray(b,float)
    check(name,a.shape==b.shape and np.allclose(a,b,rtol=0,atol=tol,equal_nan=True),max_abs_error=float(np.nanmax(np.abs(a-b))) if a.size and b.shape==a.shape and np.isfinite(a-b).any() else None,tolerance=tol)
def fit(y,x,lag):
    y,x=np.asarray(y,float),np.asarray(x,float);ok=np.isfinite(y)&np.isfinite(x);pos=np.flatnonzero(ok)
    X=sm.add_constant(x[ok]);model=sm.OLS(y[ok],X).fit();b=model.params;res=model.resid
    # Independent pair-distance kernel: use source positions, never compressed rows.
    distance=np.abs(pos[:,None]-pos[None,:]);kernel=np.maximum(1-distance/(lag+1),0)
    scores=X*res[:,None];bread=np.linalg.inv(X.T@X);cov=bread@(scores.T@kernel@scores)@bread
    se=np.sqrt(np.diag(cov));out={'n':int(ok.sum()),'source_calendar_n':len(y),'R_squared':model.rsquared}
    for i,key in enumerate(('daily_alpha','beta')):
        out.update({key:b[i],key+'_se':se[i],key+'_t':b[i]/se[i],key+'_raw_P':2*norm.sf(abs(b[i]/se[i])),key+'_CI95_low':b[i]-norm.ppf(.975)*se[i],key+'_CI95_high':b[i]+norm.ppf(.975)*se[i]})
    for suffix in ('','_CI95_low','_CI95_high'):out['linear_annual_alpha_252'+suffix]=252*out['daily_alpha'+suffix]
    return out,ok
def run(out):
    resource=reserve_performance_cores();check('reserve_two_performance_cores',len(resource.get('reserved_logical_processors',[]))>=4)
    p=load(out/'protocol.json');source=Path(p['source_identity']['path'])
    check('source_zip_identity',source.name=='feathers.zip' and source.stat().st_size==p['source_identity']['size_bytes'] and sha(source)==p['source_identity']['sha256'])
    check('frozen_compute_script',sha(ROOT/'market_beta_alpha.py')==p['script_sha256'])
    for name,digest in p['input_daily_and_paired_sha256'].items():check('frozen_input.'+name,sha(ROOT/name)==digest)
    raw={}
    with zipfile.ZipFile(source) as z:
        for name in ('close','cumadj','high','low','amt'):
            t=feather.read_table(pa.BufferReader(z.read('feathers/'+name+'.feather')))
            d=pd.DatetimeIndex(t['date'].to_numpy()).strftime('%Y-%m-%d').to_numpy();s=np.array(t.column_names[1:])
            if raw:check('matrix_alignment.'+name,np.array_equal(d,dates) and np.array_equal(s,symbols))
            dates,symbols=d,s;raw[name]=np.column_stack([t.column(j).to_numpy() for j in range(1,t.num_columns)])
    c,f=raw['close'],raw['cumadj'];validf=np.isfinite(f)&(f>0);base=f[validf.argmax(axis=0),np.arange(len(symbols))]
    adjusted=c*np.where(validf,f/base,np.nan);valid=np.isfinite(c)&(c>0)&validf
    main=np.array([(s.endswith('.SH') and s.startswith('6') and not s.startswith(('688','689'))) or (s.endswith('.SZ') and s.startswith('00')) for s in symbols]);growth=np.array([(s.endswith('.SH') and s.startswith(('688','689'))) or(s.endswith('.SZ') and s.startswith('30')) for s in symbols])
    prev=np.vstack([np.full((1,len(symbols)),np.nan),c[:-1]])
    upper=np.floor(prev*1.05*100+.5+1e-9)/100;lower=np.floor(prev*.95*100+.5+1e-9)/100
    with np.errstate(invalid='ignore',divide='ignore'):
        move=c/prev-1;ratio=f[1:]/f[:-1]
    action=np.zeros(c.shape,bool);action[1:]=np.isfinite(ratio)&(f[1:]>0)&(f[:-1]>0)&(np.abs(ratio-1)>1e-8)
    event=main[None,:]&np.isfinite(prev)&(prev>0)&(raw['amt']>0)&(np.abs(move)>=.04)&~action&(((np.abs(c-upper)<.0049)&(np.abs(raw['high']-upper)<.0049))|((np.abs(c-lower)<.0049)&(np.abs(raw['low']-lower)<.0049)))
    risk=pd.DataFrame(event).rolling(20,min_periods=1).max().to_numpy()>0
    amount=pd.DataFrame(raw['amt']).rolling(20,min_periods=20).mean().to_numpy();active=pd.DataFrame(raw['amt']>0).rolling(20,min_periods=20).sum().to_numpy()
    eligible=(np.cumsum(np.isfinite(c),axis=0)>=120)&(c>=2)&np.isfinite(adjusted)&(amount>=20000000)&(active>=18)&~risk
    proxies={};entry_counts={}
    for pool in ('all_liquid','main_liquid','liquid1000'):
        members=eligible&(main if pool=='main_liquid' else(main|growth))[None,:]
        if pool=='liquid1000':members&=pd.DataFrame(np.where(members,amount,np.nan)).rank(axis=1,ascending=False,method='first').to_numpy()<=1000
        rebuilt=[];entered=0
        for i in range(len(dates)):
            selected=members[i-1] if i else np.zeros(len(symbols),bool);n=int(selected.sum());good=selected&valid[i]&(valid[i-1] if i else False);v=int(good.sum())
            # The first held day of a new member is included using t-1 to t return.
            if i>1:entered+=int((selected&~members[i-2]&good).sum())
            total=float(np.sum(adjusted[i,good]/adjusted[i-1,good]-1)) if i else 0
            rebuilt.append([n,v,n-v,(n-v)/n if n else np.nan,total/n if n==v and n else np.nan,total/n if n else np.nan])
        columns=['N','valid_N','missing_legs','missing_weight','strict_proxy_return','missing_leg_zero_mark'];r=pd.DataFrame(rebuilt,index=dates,columns=columns);saved=frame(out/'local'/('proxy_daily_'+pool+'.csv'))
        check(pool+'.entire_source_calendar',saved.date.tolist()==dates.tolist());same(pool+'.source_position',saved.source_position,np.arange(len(dates)))
        for col in columns:same(pool+'.raw_rebuild.'+col,r[col],saved[col],1e-13 if 'return' in col or 'mark' in col else 1e-10)
        proxies[pool]=r;entry_counts[pool]=entered
    paths={'all_liquid':ROOT/'evaluation_20261023_200711/execution',**{pool:ROOT/'pool_sensitivity_20261023_202110'/pool/'execution' for pool in ('main_liquid','liquid1000')}}
    runs={};pairs={}
    for pool,path in paths.items():
        summary=load(path/'execution_summary.json');check(pool+'.20_accepted_runs',len(summary['runs'])==20)
        for row in summary['runs']:
            d=frame(path/row['run']/'daily.csv');spec=p['periods'][row['period']];grid=dates[(dates>=spec['start'])&(dates<=spec['end'])];check(pool+'.calendar.'+row['run'],d.date.tolist()==grid.tolist())
            nav=d.nav.to_numpy();returns=np.r_[nav[0]/1e9-1,nav[1:]/nav[:-1]-1];same(pool+'.net_return.'+row['run'],returns,d['return'])
            runs[(pool,row['period'],row['strategy'],row['scenario'])]=d.set_index('date').reindex(grid)['return']
        for pair in summary['paired']:pairs[(pool,pair['period'],pair['scenario'])]=pair
    regressions=load(out/'regressions.json');check('201_unique_market_fits',len(regressions)==201 and len({tuple(r[k] for k in ('pool','period','strategy','scenario','proxy_pool','proxy_method','HAC_maxlags')) for r in regressions})==201)
    gap_effect=None
    for j,row in enumerate(regressions):
        y=runs[(row['pool'],row['period'],row['strategy'],row['scenario'])];col='strict_proxy_return' if row['proxy_method']=='strict' else 'missing_leg_zero_mark';x=proxies[row['proxy_pool']].reindex(y.index)[col];fitted,ok=fit(y,x,row['HAC_maxlags'])
        for k,v in fitted.items():same('fit'+str(j)+'.'+k,v,row[k])
        check('fit'+str(j)+'.excluded_source_dates',list(y.index[~ok])==row['excluded_dates']);check('fit'+str(j)+'.HAC_configuration',row['HAC_use_correction'] is False and row['HAC_use_t'] is False)
        if row['pool']=='all_liquid' and row['period']=='evaluation_2025' and row['strategy']=='5dr' and row['scenario']=='base' and row['proxy_method']=='strict' and row['HAC_maxlags']==5:
            compact,_=fit(y.to_numpy()[ok],x.to_numpy()[ok],5);gap_effect={'calendar_se':fitted['daily_alpha_se'],'compressed_se':compact['daily_alpha_se'],'excluded_dates':row['excluded_dates']}
    relative=load(out/'relative_p3_checks.json');check('18_relative_checks',len(relative)==18)
    for j,row in enumerate(relative):
        y=runs[(row['pool'],row['period'],'added_combo',row['scenario'])];x=runs[(row['pool'],row['period'],'base_combo',row['scenario'])];result,_=fit(y,x,5)
        for k,v in result.items():same('relative'+str(j)+'.'+k,v,row[k])
        for k in ('beta_difference_from_saved','alpha_difference_from_saved','alpha_CI95_low_difference','alpha_CI95_high_difference'):same('relative'+str(j)+'.'+k,row[k],0)
    check('computation_no_errors',load(out/'errors.json')==[])
    result={'status':'passed' if all(c['passed'] for c in checks) else 'failed','check_count':len(checks),'failed_checks':[c for c in checks if not c['passed']],'independence':'Reads original ZIP independently; reconstructs all pools, all dates; OLS statsmodels and pair-distance Bartlett kernel independently verify every 201 market and 18 relative fits. Never imports market_beta_alpha or Market.','source_calendar_gap_demonstration':gap_effect,'valid_new_member_first_days_included':entry_counts,'resource_policy':resource,'checks':checks}
    (out/'validation.json').write_text(json.dumps(result,indent=2,ensure_ascii=False,allow_nan=False),encoding='utf-8');print(json.dumps({k:v for k,v in result.items() if k not in ('checks','resource_policy')},ensure_ascii=False))
if __name__=='__main__':run(Path(sys.argv[1]) if len(sys.argv)>1 else sorted(ROOT.glob('market_beta_alpha_*'))[-1])
