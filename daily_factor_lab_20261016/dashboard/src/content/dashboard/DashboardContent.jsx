import React, { useEffect, useRef, useState } from "react";
import { DataComponent, DataTable, EvidenceChart, SectionHeader, SortableItem, SortableRegion, useDataApp, useDashboardTabs } from "../../data-app-public.jsx";
import { StrategyBuilder } from "./StrategyBuilder.jsx";
import { capitalLabel, ResearchParameterPresets } from "./ResearchParameterPresets.jsx";
import { ResearchWorkbench } from "./ResearchWorkbench.jsx";
import "./dashboard.css";

const FACTORS = [["momentum60","60日动量（跳过5日）"],["reversal5","5日反转"],["lowvol20","20日低波动"],["liquidity20","20日成交金额"],["combo","四因子等权"]];
const LABELS=Object.fromEntries(FACTORS);
const COLORS={"60日动量（跳过5日）":"#4f6fed","5日反转":"#d99538","20日低波动":"#25a08e","20日成交金额":"#a168c2","四因子等权":"#dc657a","动态股票池等权参考（无费用，非指数）":"#8994a7"};
const DEFAULTS={start:"2025-01-02",end:"2025-12-31",capital:1000000000,top_n:100,rebalance:"weekly",commission:.0001,min_commission:5,slippage:.0005,min_avg_amount:20000000,min_active_days20:18,min_history:120,min_price:2,open_guard:.048,execution_model:"daily_conservative",st_policy:"price_proxy",universe:"all_a",liquidity_top_n:0,risk_proxy_window:20,max_participation:.01,max_daily_participation:.01,liquidate_end:true,wind_down_days:0};
const pct=v=>v==null||!Number.isFinite(+v)?"—":`${(+v*100).toFixed(2)}%`;
const num=(v,d=2)=>v==null||!Number.isFinite(+v)?"—":(+v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
const nameOf=r=>r.factor==="reference"?"动态股票池等权参考（无费用，非指数）":r.name||LABELS[r.factor]||r.factor;
const scenarioName=s=>({base:"基准费用",gross:"零费用",stress:"压力费用"}[s]||s);
const columns=[{key:"name",label:"策略"},{key:"total_return",label:"累计收益",renderCell:pct},{key:"annual_return",label:"年化收益",renderCell:pct},{key:"sharpe",label:"夏普比率",renderCell:v=>num(v)},{key:"max_drawdown",label:"最大回撤",renderCell:pct}];
const annualColumns=[{key:"year",label:"年份"},{key:"name",label:"策略"},{key:"return",label:"年度收益",renderCell:pct}];
const icColumns=[{key:"name",label:"因子"},{key:"mean_rank_ic",label:"平均 Rank IC",renderCell:v=>num(v,4)},{key:"ci95_low",label:"95%区间下限",renderCell:v=>num(v,4)},{key:"ci95_high",label:"95%区间上限",renderCell:v=>num(v,4)},{key:"p_raw_hac_normal",label:"原始 P（HAC）",renderCell:v=>v==null?"—":+v<.0001?(+v).toExponential(2):num(v,4)},{key:"n_dates",label:"有效日期数",renderCell:v=>num(v,0)}];
function flattenFiles(files,prefix=""){
  if(!files)return [];
  if(typeof files==="string")return [{key:prefix,path:files,label:prefix}];
  return Object.entries(files).flatMap(([key,value])=>flattenFiles(value,prefix?`${prefix} / ${key}`:key));
}
const fileLabel=key=>({report:"完整方法与结果报告",summary:"绩效汇总 CSV",yearly:"年度收益 CSV",ic:"Rank IC 统计 CSV",curves:"全量净值 CSV",config:"本次运行配置",diagnostics:"运行诊断",daily:"每日净值",trades:"交易明细",holdings:"持仓明细",strategy:"策略积木 JSON",strategy_manifest:"策略编译清单"}[key]||key);
function Empty(){return <div className="factor-empty">暂无结果，请先运行回测。</div>}
function Field({label,children}){return <label className="factor-field"><span>{label}</span>{children}</label>}

export function DashboardContent(){
  const {snapshot,queries}=useDataApp();
  const {activeTabId}=useDashboardTabs([{id:"dashboard",label:"回测结果"},{id:"strategy-blocks",label:"策略积木"},{id:"research-2025",label:"2025研究"}]);
  const [status,setStatus]=useState({busy:false}),[latest,setLatest]=useState(null),[apiError,setApiError]=useState("");
  const [config,setConfig]=useState({...DEFAULTS,...snapshot.config}),[selected,setSelected]=useState("combo"),[showReference,setShowReference]=useState(false),[posting,setPosting]=useState(false);
  const [runs,setRuns]=useState([]),[requestedRun,setRequestedRun]=useState(snapshot.run_id||"");
  const mounted=useRef(true),knownRun=useRef(null);
  const set=(key,value)=>setConfig(c=>({...c,[key]:value}));
  useEffect(()=>{mounted.current=true;let timer;
    async function result(){const res=await fetch("/api/result",{cache:"no-store"});if(res.ok){const body=await res.json();if(mounted.current&&body?.run_id){setLatest(body);knownRun.current=body.run_id}}}
    async function poll(){try{const response=await fetch("/api/status",{cache:"no-store"});if(!response.ok)throw Error();const body=await response.json();if(!mounted.current)return;setStatus(body);setApiError("");const id=typeof body.latest_run==="object"?body.latest_run?.run_id:body.latest_run;if(id&&id!==knownRun.current)await result();timer=setTimeout(poll,body.busy?2500:12000)}catch{if(mounted.current){setApiError("本地服务暂不可用；保留已保存结果。");timer=setTimeout(poll,12000)}}}
    result().catch(()=>{});fetch("/api/runs",{cache:"no-store"}).then(r=>r.json()).then(d=>setRuns(d.runs??[])).catch(()=>{});poll();return()=>{mounted.current=false;clearTimeout(timer)};
  },[]);
  const summary=queries.summary?.rows??[],curves=queries.curves?.rows??[],yearly=queries.yearly?.rows??[],ic=queries.ic?.rows??[];
  const factorOptions=summary.filter(r=>r.scenario==="base"&&r.factor!=="reference").map(r=>[r.factor,nameOf(r)]);
  const availableFactors=factorOptions.length?factorOptions:FACTORS;
  const selectedFactor=availableFactors.some(([id])=>id===selected)?selected:availableFactors[0][0];
  const selectedLabel=availableFactors.find(([id])=>id===selectedFactor)?.[1]||selectedFactor;
  const base=summary.filter(r=>r.scenario==="base"&&r.factor!=="reference").map(r=>({...r,name:nameOf(r)}));
  const chosen=base.find(r=>r.factor===selectedFactor);
  const curveRows=curves.filter(r=>r.scenario==="base"&&(r.factor!=="reference"||showReference)).map(r=>({...r,name:nameOf(r),"回撤 (%)":r.drawdown==null?null:r.drawdown*100}));
  const annualRows=yearly.filter(r=>r.scenario==="base"&&r.factor!=="reference"&&/^\d{4}$/.test(String(r.year))).map(r=>({...r,name:nameOf(r),"年度收益 (%)":r.return==null?null:r.return*100}));
  const feeRows=summary.filter(r=>r.factor===selectedFactor).map(r=>({...r,scenarioLabel:scenarioName(r.scenario),"年化收益 (%)":r.annual_return==null?null:r.annual_return*100}));
  const icRows=ic.filter(r=>r.factor!=="reference").map(r=>({...r,name:nameOf(r),n_dates:r.days??r.n_dates??r.n??r.count}));
  const matchingResult=latest?.run_id===snapshot.run_id?latest:null;
  const evidence=matchingResult||snapshot;
  const files=flattenFiles({...evidence.files,strategies:evidence.strategies??evidence.files?.strategies});
  const needsReload=latest?.run_id&&latest.run_id!==snapshot.run_id;
  const busy=status.busy||posting;
  const progress=typeof status.progress==="object"?status.progress?.message||JSON.stringify(status.progress):status.progress;
  async function run(e){e.preventDefault();setPosting(true);setApiError("");try{const res=await fetch("/api/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(config)});const body=await res.json();if(!res.ok)throw Error(body.error||`HTTP ${res.status}`);setStatus(s=>({...s,busy:true,progress:"任务已提交"}))}catch(e){setApiError(e.message)}finally{setPosting(false)}}
  async function activateRun(){if(!requestedRun||requestedRun===snapshot.run_id)return;setPosting(true);try{const res=await fetch("/api/runs/activate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({run_id:requestedRun})});const body=await res.json();if(!res.ok)throw Error(body.error||"历史结果加载失败");setStatus(s=>({...s,busy:true,progress:"正在加载历史结果"}))}catch(e){setApiError(e.message)}finally{setPosting(false)}}
  const numberInput=(key,min,max,step=1)=><input type="number" value={config[key]} min={min} max={max} step={step} required onChange={e=>set(key,+e.target.value)}/>;
  const lineSpec={type:"line",x:"date",y:"nav_index",series:"name",stackable:false,colors:COLORS,valueDecimals:3,showXAxisLabel:false};
  const ddSpec={...lineSpec,y:"回撤 (%)",valueDecimals:2};
  const annualSpec={type:"bar",x:"year",y:"年度收益 (%)",series:"name",stackable:false,colors:COLORS,valueDecimals:2};
  const feeSpec={type:"bar",x:"scenarioLabel",y:"年化收益 (%)",stackable:false,valueDecimals:2};
  const rows=[
    {id:"factor:trajectory",items:["factor:nav"],header:<SectionHeader id="factor:trajectory-heading" title="策略净值比较" filters={<label className="factor-toggle"><input type="checkbox" checked={showReference} onChange={e=>setShowReference(e.target.checked)}/>显示动态股票池参考</label>}/>},
    {id:"factor:risk-year",items:["factor:drawdown","factor:annual"],header:<SectionHeader id="factor:risk-heading" title="回撤与跨年度稳定性"/>},
    {id:"factor:statistics",items:["factor:ic","factor:fees"],header:<SectionHeader id="factor:statistics-heading" title="因子检验与费用敏感性"/>},
    {id:"factor:records",items:["factor:summary"],header:<SectionHeader id="factor:records-heading" title="策略指标与复核文件"/>},
  ];
  if(activeTabId==="strategy-blocks")return <StrategyBuilder appId={snapshot.id} config={config} setConfig={setConfig} busy={busy} progress={progress} needsReload={needsReload} snapshotStrategy={snapshot.custom_strategy} onRunStarted={()=>setStatus(s=>({...s,busy:true,progress:"自定义策略已提交"}))}/>;
  if(activeTabId==="research-2025")return <ResearchWorkbench/>;
  return <article className="factor-lab">
    {runs.length>1&&<div className="factor-run-history"><label>已保存实验<select value={requestedRun} onChange={e=>setRequestedRun(e.target.value)}>{runs.map(r=><option key={r.run_id} value={r.run_id}>{r.name} · {r.config?.start}—{r.config?.end} · {r.run_id}</option>)}</select></label><button type="button" disabled={busy||requestedRun===snapshot.run_id} onClick={activateRun}>加载该实验</button></div>}
    <form className="factor-run-form" onSubmit={run} aria-label="回测参数">
      <div className="factor-form-head"><strong>实验参数</strong><span>沪深日频 · 只做多 · {snapshot.custom_strategy?"当前为自定义策略结果":"固定因子方向"}</span><button className="factor-run" type="submit" disabled={busy}>{busy?"回测运行中…":snapshot.custom_strategy?"运行默认五策略":"运行回测"}</button></div>
      <ResearchParameterPresets config={config} setConfig={setConfig}/>
      <div className="factor-fields">
        <Field label="起始日期"><input type="date" value={config.start} min="2021-01-01" max={config.end} required onChange={e=>set("start",e.target.value)}/></Field>
        <Field label="结束日期"><input type="date" value={config.end} min={config.start} max="2026-10-16" required onChange={e=>set("end",e.target.value)}/></Field>
        <Field label="初始资金（元）">{numberInput("capital",10000,1e10,10000)}<small className="factor-parameter-hint">{capitalLabel(config.capital)}</small></Field>
        <Field label="持仓数量">{numberInput("top_n",1,500,1)}</Field>
        <Field label="调仓频率"><select value={config.rebalance} onChange={e=>set("rebalance",e.target.value)}><option value="weekly">每周</option><option value="monthly">每月</option><option value="daily">每日</option></select></Field>
      </div>
      <details className="factor-advanced"><summary>费用与股票池参数</summary><div className="factor-fields factor-fields--advanced">
        <Field label={`单边佣金率（${num(config.commission*10000)} bp）`}>{numberInput("commission",0,.01,.00001)}<small className="factor-parameter-hint">输入小数：0.0001 = 1bp</small></Field>
        <Field label={`单边滑点（${num(config.slippage*10000)} bp）`}>{numberInput("slippage",0,.01,.00001)}</Field>
        <Field label="20日平均成交额下限（元）">{numberInput("min_avg_amount",0,1e11,1000000)}</Field>
        <Field label="开盘变动过滤阈值（小数）">{numberInput("open_guard",.001,.3,.001)}</Field>
        <Field label="执行模型"><select value={config.execution_model} onChange={e=>set("execution_model",e.target.value)}><option value="daily_conservative">保守日线执行</option><option value="legacy">原版执行（用于历史对照）</option></select></Field>
        <Field label="ST信息与代理"><select value={config.st_policy} onChange={e=>set("st_policy",e.target.value)}><option value="price_proxy">价格风险代理（非真实ST标签）</option><option value="legacy">原版股票池规则</option></select><small className="factor-parameter-hint">原ZIP无历史ST标签，真实ST剔除不可用。</small></Field>
        <Field label="A股范围"><select value={config.universe} onChange={e=>set("universe",e.target.value)}><option value="all_a">全部A股</option><option value="main_a">主板A股</option><option value="growth_a">创业板与科创板</option></select></Field>
        <Field label="流动性前N只（0=不限制）">{numberInput("liquidity_top_n",0,6000,1)}</Field>
        <Field label="价格风险代理窗口（交易日）">{numberInput("risk_proxy_window",1,120,1)}</Field>
        <Field label="前均额容量上限（小数）">{numberInput("max_participation",.0001,.1,.0001)}</Field>
        <Field label="当日成交额审计上限（小数）">{numberInput("max_daily_participation",.0001,.1,.0001)}</Field>
        <Field label="末日处理"><span className="factor-parameter-check"><input type="checkbox" checked={config.liquidate_end} onChange={e=>set("liquidate_end",e.target.checked)}/>尝试清仓（仍可能残留）</span></Field>
        <Field label="预定提前退出天数（0=关闭）">{numberInput("wind_down_days",0,120,1)}<small className="factor-parameter-hint">保守日线模型：最后N个源交易日目标归零、禁止买入；0沿用末日设置。</small></Field>
      </div></details>
      <div className="factor-run-status" role="status"><span className={`factor-status-dot ${busy?"is-running":""}`}/>{busy?(progress||"正在计算，请稍候"):snapshot.run_id?`当前结果：${snapshot.run_id}`:summary.length?"已加载保存的实验结果":"等待首次回测"}<span className="factor-status-zone">Asia/Shanghai</span></div>
      {needsReload&&!busy&&<div className="factor-ready" role="status">新结果已就绪。<button type="button" onClick={()=>window.location.reload()}>加载已更新页面</button></div>}
      {(apiError||status.error)&&<p className="factor-error" role="alert">{apiError||String(status.error)}</p>}
    </form>
    <div className="factor-selection"><label>重点策略 <select value={selectedFactor} onChange={e=>setSelected(e.target.value)}>{availableFactors.map(([id,label])=><option key={id} value={id}>{label}</option>)}</select></label><span>基准费用 · 净值起点 1.00</span></div>
    <DataComponent id="factor:metrics" queryId="summary" kind="custom" title={`${selectedLabel} · 基准费用`} sourceRows={chosen?[chosen]:[]} displayRows={chosen?[chosen]:[]}>
      <div className="factor-metric-strip" data-reviewed-rows>{[["累计收益",pct(chosen?.total_return)],["年化收益",pct(chosen?.annual_return)],["夏普比率",num(chosen?.sharpe)],["最大回撤",pct(chosen?.max_drawdown)]].map(([label,value])=><div key={label}><span>{label}</span><strong>{value}</strong></div>)}</div>
    </DataComponent>
    <SortableRegion id="factor:canvas" label="回测分析图表" variant="canvas" spacing="standard" columns={12} authoredRevision={1} rows={rows}>
      <SortableItem id="factor:nav" label="策略净值" kind="chart" span={12}><EvidenceChart variant="card" id="factor:nav" title="扣费后净值" queryId="curves" spec={lineSpec} rows={curveRows} sourceRows={curveRows} height={360} description="当前运行中采用同一股票池、期间与交易规则的策略。动态股票池等权参考不含费用，非指数、非可交易绩效。"/></SortableItem>
      <SortableItem id="factor:drawdown" label="历史回撤" kind="chart" span={6}><EvidenceChart variant="card" id="factor:drawdown" title="历史回撤" queryId="curves" spec={ddSpec} rows={curveRows} sourceRows={curveRows} height={250} description="当日净值相对历史最高净值的跌幅。"/></SortableItem>
      <SortableItem id="factor:annual" label="年度收益比较" kind="chart" span={6}><EvidenceChart variant="card" id="factor:annual" title="年度收益比较" queryId="yearly" spec={annualSpec} rows={annualRows} sourceRows={annualRows} height={250} description="首尾年份可能不满一年；按本次回测期间展示。"/></SortableItem>
      <SortableItem id="factor:ic" label="Rank IC统计" kind="table" span={7}><DataComponent variant="card" id="factor:ic" title="Rank IC 与不确定性" kind="table" queryId="ic" sourceRows={icRows} displayRows={icRows} description="Rank IC 为因子排序与后续收益排序的相关。显示95%置信区间与HAC原始P；多因子检验为探索性比较。">{icRows.length?<DataTable rows={icRows} columns={icColumns} searchable={false} caption="策略 Rank IC 统计"/>:<Empty/>}</DataComponent></SortableItem>
      <SortableItem id="factor:fees" label="费用敏感性" kind="chart" span={5}><EvidenceChart variant="card" id="factor:fees" title={`${selectedLabel} · 费用敏感性`} queryId="summary" spec={feeSpec} rows={feeRows} sourceRows={feeRows} height={230} description="同一交易逻辑在不同费用情景下的年化收益；模拟结果不代表未来收益。"/></SortableItem>
      <SortableItem id="factor:summary" label="基准费用策略指标" kind="table" span={12}><DataComponent variant="card" id="factor:summary" title="基准费用策略指标" queryId="summary" kind="table" sourceRows={base} displayRows={base}>{base.length?<DataTable rows={base} columns={columns} searchable={false} caption="策略基准费用指标"/>:<Empty/>}</DataComponent></SortableItem>
    </SortableRegion>
    <section className="factor-evidence" aria-label="方法与下载"><h2 data-editable-id="factor:downloads-title">完整方法与下载</h2>
      <p>原始 OHLC 与累计复权因子用于收益计算；使用 A 股可交易性代理和份额记账。涨跌停、ST、退市、停牌及真实成交可得性存在数据限制。</p>
      <div className="factor-file-links">{files.filter(f=>!f.key.startsWith("strategies")).map(f=><a key={f.key} href={`/api/download?path=${encodeURIComponent(f.path)}`} download>{fileLabel(f.key)}</a>)}</div>
      {files.some(f=>f.key.startsWith("strategies"))&&<div className="factor-strategy-downloads"><strong>{selectedLabel} · 基准费用</strong><div className="factor-file-links">{files.filter(f=>f.key.startsWith(`strategies / ${selectedFactor} /`)).map(f=><a key={f.key} href={`/api/download?path=${encodeURIComponent(f.path)}`} download>{fileLabel(f.key.split(" / ").at(-1))}</a>)}</div></div>}
      {!files.length&&<span className="factor-muted">回测完成后提供方法、统计结果与策略交易明细。</span>}
      {evidence.config&&<details className="factor-detail"><summary>当前结果的实际配置</summary><pre>{JSON.stringify(evidence.config,null,2)}</pre></details>}
      {evidence.diagnostics&&<details className="factor-detail"><summary>运行诊断</summary><pre>{JSON.stringify(evidence.diagnostics,null,2)}</pre></details>}
    </section>
  </article>;
}
