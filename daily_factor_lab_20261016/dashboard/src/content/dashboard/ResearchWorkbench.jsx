import React, { useEffect, useRef, useState } from "react";
import { DataComponent, DataTable, EvidenceChart, SectionHeader, SortableItem, SortableRegion, useDataApp } from "../../data-app-public.jsx";
import { capitalLabel } from "./ResearchParameterPresets.jsx";
import { isFiniteNumber } from "./execution-config.js";
import "./research-workbench.css";

const pct=value=>!isFiniteNumber(value)?"暂无":`${(value*100).toFixed(2)}%`;
const number=(value,digits=0)=>!isFiniteNumber(value)?"暂无":value.toLocaleString("zh-CN",{maximumFractionDigits:digits});
const money=value=>!isFiniteNumber(value)?"暂无":capitalLabel(value);
const universeLabel=value=>({all_a:"全部A股",main_a:"主板A股",growth_a:"创业板与科创板"}[value]||value||"未提供范围");
const frequency=value=>({daily:"日调仓",weekly:"周调仓",monthly:"月调仓"}[value]||value||"未提供频率");
const scenarioLabel=value=>({base:"基准费用／末日单日退出（原对照）",gross:"零费用／末日单日退出",stress:"压力费用／末日单日退出",fees_only:"保留费用，滑点参数为0",capacity_tight:"容量收紧／末日单日退出",wind_down20:"提前20日连续退出（后验）",wind_down40:"提前40日连续退出（后验）",wind_down20_stress:"提前20日退出／压力费用（后验）",wind_down20_capacity_tight:"提前20日退出／容量收紧（后验）",wind_down40_capacity_tight:"提前40日退出／容量收紧（后验）"}[value]||value);
const roleLabel=value=>({selection_2024:"2024选型",evaluation_2025:"2025回顾性评价",assignment_2025_2026sep:"作业延伸至2026-09-30"}[value]||value||"期间未提供");
const periodKey=row=>`${row.period_role||"期间"}|${row.start||""}|${row.end||""}`;
const experimentLabel=row=>`${row.name||row.factor||row.experiment_id} · ${row.universe_name||universeLabel(row.universe)} · ${row.top_n==null?"目标只数未提供":`目标${row.top_n}只`} · ${frequency(row.rebalance)}`;
const sameVersion=(a,b)=>a&&b&&a.study_id===b.study_id&&(a.snapshot_revision&&b.snapshot_revision?a.snapshot_revision===b.snapshot_revision:JSON.stringify(a)===JSON.stringify(b));
const windDownDays=row=>row.wind_down_days??row.config?.wind_down_days??0;
const liquidationState=row=>{
  const positions=row.terminal_positions??row.terminal?.residual_positions;
  const residual=row.terminal_position_value??row.terminal?.residual_value;
  const reported=row.fully_liquidated??row.terminal?.fully_liquidated??row.liquidation_complete;
  if((positions!=null&&positions>0)||(residual!=null&&residual>0))return "仍有残仓";
  if(positions===0||residual===0||reported===true){
    if(row.writeoff_loss>0)return "账面残仓为0（含减记）";
    if(row.writeoff_loss===0)return "模型已清仓";
    return "账面残仓为0（减记情况暂无）";
  }
  return reported===false?"仍有残仓":"暂无";
};
const writeoffMoney=value=>!isFiniteNumber(value)?"暂无":`${number(value,2)} 元`;
function fileList(files,prefix=""){if(typeof files==="string")return[{label:prefix||files,path:files}];if(!files||typeof files!=="object")return[];return Object.entries(files).flatMap(([name,value])=>fileList(value,prefix?`${prefix} / ${name}`:name))}
function readable(value){if(value==null)return "未提供";if(typeof value==="object")return JSON.stringify(value,null,2);return String(value)}
function NoEvidence({children="此项结果尚未提供。"}){return <div className="rw-empty">{children}</div>}

const experimentColumns=[
  {key:"name",label:"因子/策略"},{key:"universeLabel",label:"选股范围"},{key:"top_n",label:"目标只数"},{key:"rebalanceLabel",label:"调仓"},
  {key:"total_return",label:"累计收益",renderCell:pct},{key:"annual_return",label:"年化收益",renderCell:pct},{key:"sharpe",label:"夏普",renderCell:v=>number(v,2)},
  {key:"max_drawdown",label:"最大回撤",renderCell:pct},{key:"mean_invested",label:"平均实际仓位",renderCell:pct},
  {key:"blocked_attempts",label:"拒单尝试数",renderCell:v=>number(v)},{key:"terminal_position_value",label:"末日残仓市值",renderCell:money},{key:"writeoff_loss",label:"缺价减记（非成交）",renderCell:writeoffMoney},
];
const splitColumns=[{key:"experimentLabel",label:"实验"},{key:"period",label:"选择/验证区间"},{key:"return",label:"期间收益",renderCell:pct},{key:"sharpe",label:"夏普",renderCell:v=>number(v,2)},{key:"max_drawdown",label:"最大回撤",renderCell:pct},{key:"mean_invested",label:"实际仓位",renderCell:pct}];
const periodColumns=[{key:"periodLabel",label:"区间用途"},{key:"start",label:"开始"},{key:"end",label:"结束"},{key:"days",label:"交易日",renderCell:v=>number(v)},{key:"total_return",label:"累计收益",renderCell:pct},{key:"max_drawdown",label:"最大回撤",renderCell:pct},{key:"mean_invested",label:"实际仓位",renderCell:pct}];
const liquidationColumns=[{key:"scenarioLabel",label:"退出与费用情景"},{key:"windDownDays",label:"预定退出窗",renderCell:v=>`${number(v)} 日`},{key:"total_return",label:"累计收益",renderCell:pct},{key:"max_drawdown",label:"最大回撤",renderCell:pct},{key:"terminal_position_value",label:"末日残仓市值",renderCell:money},{key:"writeoff_loss",label:"缺价减记（非成交）",renderCell:writeoffMoney},{key:"liquidationState",label:"模型账面退出状态"}];
const executionColumns=[{key:"reason",label:"执行/拒单原因"},{key:"count",label:"次数",renderCell:v=>number(v)},{key:"notional",label:"对应金额",renderCell:money}];
const stColumns=[{key:"scope",label:"覆盖范围",renderCell:roleLabel},{key:"total_code_days",label:"代码×日期总数",renderCell:v=>number(v)},{key:"known_st",label:"已知ST",renderCell:v=>number(v)},{key:"known_non_st",label:"已知非ST",renderCell:v=>number(v)},{key:"unknown",label:"状态未知",renderCell:v=>number(v)},{key:"actualCoverage",label:"真实状态覆盖率",renderCell:pct},{key:"proxy_excluded_code_days",label:"代理排除代码日",renderCell:v=>number(v)}];

export function ResearchWorkbench({externalBusy=false,onTaskStarted=()=>{}}){
  const {snapshot,queries}=useDataApp();
  const frozen=snapshot.research??null;
  const [apiResearch,setApiResearch]=useState(null),[connection,setConnection]=useState(""),[period,setPeriod]=useState("auto"),[scenario,setScenario]=useState("auto"),[universe,setUniverse]=useState("all"),[inspected,setInspected]=useState("");
  const [queue,setQueue]=useState({busy:null,progress:"",error:null}),[submitting,setSubmitting]=useState(false),[queueFinished,setQueueFinished]=useState(false),[queueConnection,setQueueConnection]=useState(""),[submissionError,setSubmissionError]=useState("");
  const trackedJob=useRef(null),submissionPending=useRef(false),queueRevision=useRef(0);
  useEffect(()=>{let active=true,timer;async function refresh(){try{const response=await fetch("/api/research",{cache:"no-store"});if(!response.ok)throw Error();const value=await response.json();if(!active)return;setApiResearch(value);setConnection("");timer=setTimeout(refresh,/running|pending|building/.test(value.status||"")?4000:20000)}catch{if(active){setConnection("研究接口暂不可用，当前显示已保存快照。");timer=setTimeout(refresh,20000)}}}refresh();return()=>{active=false;clearTimeout(timer)}},[]);
  useEffect(()=>{let active=true,timer;async function pollQueue(){const revision=queueRevision.current;try{const response=await fetch("/api/status",{cache:"no-store"});if(!response.ok)throw Error();const value=await response.json();if(typeof value.busy!=="boolean"||value.busy&&(!value.job_id||!value.job_kind))throw Error();if(!active)return;if(revision!==queueRevision.current){timer=setTimeout(pollQueue,3000);return}if(value.busy&&value.job_kind==="research_v3"&&value.job_id)trackedJob.current=value.job_id;if(trackedJob.current&&value.job_id===trackedJob.current&&value.job_kind==="research_v3"&&!value.busy&&["complete","partial","failed","interrupted"].includes(value.status))setQueueFinished(true);setQueue(value);setQueueConnection("");timer=setTimeout(pollQueue,value.busy?3000:10000)}catch{if(active){setQueueConnection("队列状态暂不可读，请保留页面等待服务恢复。");timer=setTimeout(pollQueue,10000)}}}pollQueue();return()=>{active=false;clearTimeout(timer)}},[]);
  const research=frozen;
  const activeStudyId=apiResearch?.study_id||frozen?.study_id;
  const freshAvailable=!!(apiResearch?.study_id&&apiResearch.generated_at&&["complete","partial"].includes(apiResearch.status)&&(!frozen||!sameVersion(apiResearch,frozen)));
  async function requestFullStudy(){if(submissionPending.current||externalBusy||queue.busy!==false||queueConnection)return;submissionPending.current=true;queueRevision.current++;setSubmitting(true);setSubmissionError("");setQueueFinished(false);try{const response=await fetch("/api/research/run",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirmed_full_study:true})});const value=await response.json();if(!response.ok)throw Error(value.error||`研究请求失败（HTTP ${response.status}）`);trackedJob.current=value.job_kind==="research_v3"?value.job_id:null;queueRevision.current++;onTaskStarted(value,"V3研究已提交");setQueue({busy:true,job_id:value.job_id,job_kind:value.job_kind,progress:"已提交固定120候选研究，等待后台计算。",error:null})}catch(error){setSubmissionError(error.message)}finally{submissionPending.current=false;setSubmitting(false)}}
  const experiments=queries.research_experiments?.rows??research?.experiments??[];
  const curves=queries.research_curves?.rows??research?.curves??[];
  const splits=queries.research_splits?.rows??research?.splits??[];
  const executions=queries.research_execution?.rows??research?.execution??[];
  const coverage=queries.research_st_coverage?.rows??research?.st_coverage??[];
  const queryReady=id=>!!queries[id];
  const periods=[...new Map(experiments.map(row=>[periodKey(row),row])).entries()];
  const defaultPeriod=periods.find(([,row])=>row.start?.startsWith("2025")&&row.end?.startsWith("2025"))?.[0]??periods[0]?.[0]??"all";
  const activePeriod=period==="auto"?defaultPeriod:period;
  const currentPeriod=periods.find(([key])=>key===activePeriod)?.[1];
  const scenarios=[...new Set(experiments.filter(row=>periodKey(row)===activePeriod).map(row=>row.scenario).filter(Boolean))];
  const defaultScenario=scenarios.includes("wind_down20")?"wind_down20":scenarios.includes("base")?"base":scenarios[0];
  const activeScenario=scenarios.includes(scenario)?scenario:defaultScenario;
  const universes=[...new Map(experiments.filter(row=>row.universe).map(row=>[row.universe,row.universe_name||universeLabel(row.universe)])).entries()];
  const selectedRows=experiments.filter(row=>(activePeriod==="all"||periodKey(row)===activePeriod)&&row.scenario===activeScenario&&(universe==="all"||row.universe===universe)).map(row=>({...row,name:row.name||row.factor||row.experiment_id,experimentLabel:experimentLabel(row),universeLabel:row.universe_name||universeLabel(row.universe),rebalanceLabel:frequency(row.rebalance),"年化收益 (%)":row.annual_return==null?null:row.annual_return*100,"最大回撤 (%)":row.max_drawdown==null?null:row.max_drawdown*100}));
  const chosenId=selectedRows.some(row=>row.experiment_id===inspected)?inspected:selectedRows.find(row=>row.experiment_id===research?.selection?.selected_id)?.experiment_id??selectedRows[0]?.experiment_id;
  const chosen=selectedRows.find(row=>row.experiment_id===chosenId);
  const ids=new Set(selectedRows.map(row=>row.experiment_id)),byId=Object.fromEntries(experiments.map(row=>[row.experiment_id,row]));
  const curveRows=curves.filter(row=>ids.has(row.experiment_id)&&row.period_role===currentPeriod?.period_role&&row.scenario===activeScenario).map(row=>({...row,experimentLabel:byId[row.experiment_id]?experimentLabel(byId[row.experiment_id]):row.experiment_id}));
  const curveCount=new Set(curveRows.map(row=>row.experiment_id)).size;
  const splitRows=splits.filter(row=>row.experiment_id===chosenId&&row.period_role===currentPeriod?.period_role&&row.scenario===activeScenario).map(row=>({...row,return:row.return??row.total_return,experimentLabel:byId[row.experiment_id]?experimentLabel(byId[row.experiment_id]):row.experiment_id}));
  const periodRows=experiments.filter(row=>row.experiment_id===chosenId&&row.scenario==="base").map(row=>({...row,periodLabel:roleLabel(row.period_role)}));
  const executionRows=executions.filter(row=>row.experiment_id===chosenId&&row.period_role===currentPeriod?.period_role&&row.scenario===activeScenario);
  const liquidationRows=experiments.filter(row=>row.experiment_id===chosenId&&row.period_role===currentPeriod?.period_role).map(row=>({...row,scenarioLabel:scenarioLabel(row.scenario),windDownDays:windDownDays(row),liquidationState:liquidationState(row)}));
  const diagnostics=chosen?.execution_diagnostics??{},terminal=chosen?.terminal??{};
  const capitalItems=chosen?[
    ["平均实际仓位",pct(chosen.mean_invested)],["末日净资产",money(chosen.final_nav)],
    ["末日残仓市值",money(chosen.terminal_position_value??terminal.residual_value)],["末日残仓股票数",number(chosen.terminal_positions??terminal.residual_positions)],
    ["预定退出窗口",`${number(windDownDays(chosen))} 日`],["退出窗口开始",terminal.wind_down_start??(windDownDays(chosen)===0?"未提前退出":"暂无")],["账面退出状态",liquidationState(chosen)],["缺价减记损失（非成交）",writeoffMoney(chosen.writeoff_loss)],
    ["交易笔数",number(chosen.trades)],["拒单尝试数",number(chosen.blocked_attempts)],["总费用",money(chosen.costs)],
    ["最大当日额参与率（事后）",pct(diagnostics.max_realized_daily_participation)],["容量裁减目标金额",money(diagnostics.requested_notional_capacity_clipped)],
    ["容量越限金额",diagnostics.max_combined_capacity_excess==null?"暂无":`${number(diagnostics.max_combined_capacity_excess,8)} 元`],
    ["价格代理排除代码日",number(diagnostics.proxy_excluded_observations)],["开盘流动性核实",diagnostics.opening_liquidity_verified==null?"暂无":diagnostics.opening_liquidity_verified?"已核实":"未核实"],
  ]:[];
  const coverageRows=coverage.map(row=>({...row,actualCoverage:row.total_code_days>0&&row.known_st!=null&&row.known_non_st!=null?(row.known_st+row.known_non_st)/row.total_code_days:null}));
  const generated=research?.generated_at?new Date(research.generated_at).toLocaleString("zh-CN",{timeZone:"Asia/Shanghai"}):null;
  const selection=research?.selection;
  const repair=research?.feasibility_repair;
  const blockRows=[
    {id:"research:comparison-row",items:["research:comparison","research:curves"],header:<SectionHeader id="research:comparison-heading" title="同期间实验比较"/>},
    {id:"research:ledger-row",items:["research:ledger"]},
    {id:"research:splits-row",items:["research:period-compare","research:splits"],header:<SectionHeader id="research:splits-heading" title="2024选型与回顾性评价"/>},
    {id:"research:liquidation-row",items:["research:liquidation"],header:<SectionHeader id="research:liquidation-heading" title="退出可行性：原对照与后验执行修订"/>},
    {id:"research:execution-row",items:["research:capital","research:execution"],header:<SectionHeader id="research:execution-heading" title="资金容量、拒单与末日残仓" filters={selectedRows.length?<label className="rw-detail-selector">检查实验<select value={chosenId??""} onChange={event=>setInspected(event.target.value)}>{selectedRows.map(row=><option key={row.experiment_id} value={row.experiment_id}>{row.experimentLabel}</option>)}</select></label>:null}/>},
    {id:"research:st-row",items:["research:st"],header:<SectionHeader id="research:st-heading" title="历史ST信息覆盖与代理筛选"/>},
  ];
  return <article className="research-workbench">
    <div className="rw-intro"><div><h2>2025研究工作台</h2><p>2024选型，保留全部2025结果；作业延伸至2026-09-30。</p></div><span>{generated?`${generated} · Asia/Shanghai`:`研究状态：${apiResearch?.status??"尚未生成"}`}{apiResearch?.status&&apiResearch.status!==research?.status?` · 后台：${apiResearch.status}`:""}</span></div>
    <div className="rw-protocol"><div><span>主回测</span><strong>2025-01-02 — 2025-12-31</strong></div><div><span>作业资金</span><strong>10亿元</strong></div><div><span>指定费用</span><strong>单边佣金1bp · 卖出印花税5bp</strong></div><div><span>信息边界</span><strong>仅原ZIP · 历史ST未知</strong></div></div>
    <section className="rw-run-controls" aria-label="完整研究运行入口"><div><strong>完整研究队列</strong><p>固定120候选：2024选型、2025全部候选评价，再运行入围敏感性与作业延伸。预计约20分钟；计算期间保留旧结果，新审核快照就绪后手动加载。</p></div><button type="button" onClick={requestFullStudy} disabled={submitting||externalBusy||queue.busy!==false||!!queueConnection}>{submitting?"正在提交…":queue.busy?"后台任务运行中…":queue.busy===null?"读取队列状态…":"重新运行完整研究"}</button><p className="rw-queue-status" role="status">{queueConnection?queueConnection:queue.busy===null?"正在读取本地队列状态…":queue.busy?(queueConnection||readable(queue.progress)||"后台计算中"):queueFinished?"后台任务已结束；研究结果与审核快照就绪后可手动加载。":"队列空闲；点击按钮将启动完整研究。"}</p>{(submissionError||queue.error)&&<p className="rw-queue-error" role="alert">{submissionError||readable(queue.error)}</p>}</section>
    {connection&&<p className="rw-status" role="status">{connection}</p>}
    {freshAvailable&&!queue.busy&&<div className="factor-ready" role="status">研究结果已更新；重新载入可同步图表与来源记录。<button type="button" disabled={!activeStudyId} onClick={()=>{if(activeStudyId&&freshAvailable)window.location.reload()}}>加载已更新研究</button></div>}
    {!research||!queryReady("research_experiments")?<NoEvidence>研究结果尚未写入审核快照。完成计算后显示实际实验、执行诊断和ST覆盖；此处不填入示例结果。</NoEvidence>:<>
      <div className="rw-controls"><label>实验期间<select value={activePeriod} onChange={event=>setPeriod(event.target.value)}>{periods.map(([key,row])=><option key={key} value={key}>{roleLabel(row.period_role)} · {row.start} — {row.end}</option>)}</select></label><label>费用情景<select value={activeScenario??""} onChange={event=>setScenario(event.target.value)}>{scenarios.map(value=><option key={value} value={value}>{scenarioLabel(value)}</option>)}</select></label><label>选股范围<select value={universe} onChange={event=>setUniverse(event.target.value)}><option value="all">全部实验范围</option>{universes.map(([value,label])=><option key={value} value={value}>{label}</option>)}</select></label><span>{selectedRows.length} 项实际实验</span></div>
      {selection&&<div className="rw-selection-note"><strong>选型记录</strong><span>选择于：{roleLabel(selection.selected_on)}</span><span>标准：{readable(selection.criterion)}</span><span>选中实验：{readable(selection.selected_id)}</span><span>此前V1已包含部分2025信息，2025不是从未触碰的独立确认样本。</span></div>}
      <div className="rw-exit-note"><strong>退出口径须与收益一起查看</strong><p className="rw-writeoff-warning">零残仓可能包含缺价减记，并不证明全部通过卖出成交；原ZIP缺少真实退市、换股和停牌处置记录。减记损失作为非成交项目单列。</p><p>base保留末日单日尝试退出的原对照。先发现残仓问题后，才对2024既定入围名单补做20/40交易日连续退出；未更换2024因子模型。这是后验执行修订，不能称为预先未看结果的验证。</p><p>提前退出窗口只按声明区间最后N个源交易日预定，目标归零并禁止买入；不按未来价格或成交额选择日期。当前情景仅展示实际已保存的候选，原对照的全部候选仍可在情景框中选择。</p></div>
      <p className="rw-status">数值核查记录：{({not_run:"尚未执行",passed:"已记录通过",complete:"已完成",failed:"存在失败",partial:"部分完成"}[research.validation?.status]||research.validation?.status||"未提供")}。所列会计与时序检查不等于真实成交核实。</p>
      <SortableRegion id="research:canvas" label="2025研究证据" variant="canvas" spacing="standard" columns={12} authoredRevision={1} rows={blockRows}>
        <SortableItem id="research:comparison" label="收益与回撤比较" kind="chart" span={6}><EvidenceChart variant="card" id="research:comparison" queryId="research_experiments" title="全部可比组合的收益与回撤" spec={{type:"scatter",x:"最大回撤 (%)",y:"年化收益 (%)",series:"universeLabel",stackable:false,xLabel:"最大回撤 (%)",yLabel:"年化收益 (%)",valueDecimals:2}} rows={selectedRows} sourceRows={selectedRows} height={290} description="保留当前期间、费用与范围下的全部实验，不按2025收益筛掉较差组合。" chartOptions={{getMarkActions:({row})=>[{label:"检查此实验的执行记录",onSelect:()=>setInspected(row.experiment_id)}]}}/></SortableItem>
        <SortableItem id="research:curves" label="保存的净值曲线" kind="chart" span={6}>{queryReady("research_curves")?<EvidenceChart variant="card" id="research:curves" queryId="research_curves" title="已保存实验的净值" spec={{type:"line",x:"date",y:"nav_index",series:"experimentLabel",stackable:false,valueDecimals:3}} rows={curveRows} sourceRows={curveRows} height={290} description={`当前范围有${selectedRows.length}项实验，其中${curveCount}项保留全日净值。完整组合比较见下表，不用少量曲线替代全部结果。`}/>:<NoEvidence>净值曲线尚未提供。</NoEvidence>}</SortableItem>
        <SortableItem id="research:ledger" label="实验比较明细" kind="table" span={12}><DataComponent variant="card" id="research:ledger" queryId="research_experiments" kind="table" title="全部实验与资金使用" sourceRows={selectedRows} displayRows={selectedRows}><DataTable rows={selectedRows} columns={experimentColumns} rowKey="experiment_id" selectedRowKey={chosenId} onRowSelect={row=>setInspected(row.experiment_id)} rowActionLabel={row=>`检查${row.experimentLabel}的执行记录`} caption="当前期间费用与股票池筛选后的全部研究实验"/></DataComponent></SortableItem>
        <SortableItem id="research:period-compare" label="同一实验跨期原对照" kind="table" span={7}><DataComponent variant="card" id="research:period-compare" queryId="research_experiments" kind="table" title="同一参数的跨期原对照（末日单日退出）" sourceRows={periodRows} displayRows={periodRows} description="只列base原对照：2024、2025及延伸区间并列，不与后验提前退出修订混用。只显示已有记录，不补造缺失区间。">{periodRows.length?<DataTable rows={periodRows} columns={periodColumns} searchable={false} caption="同一实验基准费用末日单日退出原对照跨期间结果"/>:<NoEvidence/>}</DataComponent></SortableItem>
        <SortableItem id="research:splits" label="区间分段结果" kind="table" span={5}>{queryReady("research_splits")?<DataComponent variant="card" id="research:splits" queryId="research_splits" kind="table" title="当前实验的期间分段" sourceRows={splitRows} displayRows={splitRows} description="与当前检查的实验、期间和费用情景一致。半年分段用于辅助观察，不重新选型；未保留详细路径的候选可能无分段记录。">{splitRows.length?<DataTable rows={splitRows} columns={splitColumns.filter(column=>column.key!=="experimentLabel")} searchable={false} caption="当前实验实际保存的期间分段"/>:<NoEvidence>此实验期间未保存详细分段。</NoEvidence>}</DataComponent>:<NoEvidence>区间拆分证据尚未提供。</NoEvidence>}</SortableItem>
        <SortableItem id="research:liquidation" label="退出窗口与残仓比较" kind="table" span={12}><DataComponent variant="card" id="research:liquidation" queryId="research_experiments" kind="table" title="同一因子与参数的退出窗口比较" sourceRows={liquidationRows} displayRows={liquidationRows} description="当前检查实验、当前期间的原对照与补充情景并列。退出窗口按模型账面退出状态选择，不按2025收益重新挑因子；零残仓可能含减记，不代表全部卖出。">{chosen&&<p className="rw-chosen-name" data-reviewed-rows>{chosen.experimentLabel}</p>}{liquidationRows.length?<DataTable rows={liquidationRows} columns={liquidationColumns} searchable={false} caption="当前实验实际保存的退出窗口及费用容量情景"/>:<NoEvidence/>}{!repair&&<p className="rw-status">提前退出补充尚未写入当前审核快照，原对照保留。</p>}</DataComponent></SortableItem>
        <SortableItem id="research:capital" label="实际资金与残仓" kind="custom" span={5}><DataComponent variant="card" id="research:capital" queryId="research_experiments" kind="custom" title="实际资金使用与末日状态" sourceRows={chosen?[chosen]:[]} displayRows={chosen?[chosen]:[]} description="10亿初始资金不等于全额成交。实际仓位与残仓取自本实验账本；末日清算指尝试，可能仍有未成交持仓。显示精度不等于真实撮合精度。">{chosen?<><p className="rw-chosen-name" data-reviewed-rows>{chosen.experimentLabel}</p><dl className="rw-capital-values" data-reviewed-rows>{capitalItems.map(([label,value])=><div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl><details className="rw-actual-config"><summary>查看实际参数与执行诊断原值</summary><pre>{JSON.stringify({config:chosen.config,terminal,execution_diagnostics:diagnostics},null,2)}</pre></details></>:<NoEvidence/>}</DataComponent></SortableItem>
        <SortableItem id="research:execution" label="逐原因执行诊断" kind="table" span={7}>{queryReady("research_execution")?<DataComponent variant="card" id="research:execution" queryId="research_execution" kind="table" title="执行与拒单原因" sourceRows={executionRows} displayRows={executionRows} description="次数与金额按引擎记录展示；计数单位、是否含重试及容量审计规则见来源方法。缺少订单分母时不推算成交率。">{executionRows.length?<DataTable rows={executionRows} columns={executionColumns} searchable={false} caption="所选实验的实际执行拒单记录"/>:<NoEvidence>所选实验未提供逐原因记录。</NoEvidence>}</DataComponent>:<NoEvidence>执行诊断证据尚未提供。</NoEvidence>}</SortableItem>
        <SortableItem id="research:st" label="历史ST覆盖" kind="custom" span={12}>{queryReady("research_st_coverage")?<DataComponent variant="card" id="research:st" queryId="research_st_coverage" kind="custom" title="真实状态与价格代理分别计数" sourceRows={coverageRows} displayRows={coverageRows} description="分母为代码×日期观察。原ZIP没有历史ST标签；未知不视为非ST，价格风险代理排除不等于真实ST剔除。"><p className="rw-st-warning">仅有原ZIP，无法证明已真实剔除ST。价格代理只使用当时及此前信息，其排除数量单独报告。</p>{coverageRows.length?<DataTable rows={coverageRows} columns={stColumns} searchable={false} caption="真实ST覆盖与代理排除代码日"/>:<NoEvidence/>}</DataComponent>:<NoEvidence>ST覆盖证据尚未提供。</NoEvidence>}</SortableItem>
      </SortableRegion>
      <section className="rw-methods"><h3>研究协议、局限与复核文件</h3><div className="factor-file-links">{fileList(research.files).map(file=><a key={file.path} href={`/api/research/download?path=${encodeURIComponent(file.path)}`} download>{file.label}</a>)}</div>{research.limitations?.length>0&&<ul>{research.limitations.map((text,index)=><li key={index}>{String(text)}</li>)}</ul>}<details><summary>查看保存的研究协议与验证记录</summary><pre>{JSON.stringify({study_id:research.study_id,protocol:research.protocol,selection:research.selection,validation:research.validation},null,2)}</pre></details>{repair&&<details><summary>后验退出修订协议、仅按清仓选择与验收记录</summary><pre>{JSON.stringify(repair,null,2)}</pre></details>}</section>
    </>}
  </article>;
}
