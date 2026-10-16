import React from "react";
import { DataComponent, DataTable } from "../../data-app-public.jsx";
import { isFiniteNumber } from "./execution-config.js";

const number=value=>isFiniteNumber(value)?value.toLocaleString("zh-CN",{maximumFractionDigits:3}):"—";
const percent=value=>isFiniteNumber(value)?`${(value*100).toFixed(2)}%`:"—";
const columns=[{key:"period",label:"邻居情景"},{key:"positive_fraction",label:"正收益 / 已观测",renderCell:percent},{key:"return_median",label:"收益中位数",renderCell:percent},{key:"return_worst",label:"最差收益",renderCell:percent},{key:"observed",label:"已观测分母",renderCell:number},{key:"expected",label:"应有分母",renderCell:number},{key:"failed",label:"失败",renderCell:number},{key:"unknown",label:"未知",renderCell:number}];

export function neighborEvidence(study,query,experimentId){
  if(!study?.study_id||!study.snapshot_revision||!query||!experimentId)return null;
  const source=query.source??{};
  const studyId=query.study_id??source.study_id,revision=query.source_snapshot_revision??source.source_snapshot_revision,script=query.script_sha256??source.script_sha256;
  if(studyId!==study.study_id||revision!==study.snapshot_revision||typeof script!=="string"||!/^[a-f\d]{64}$/i.test(script)||!Array.isArray(query.rows))return null;
  return {rows:query.rows.filter(row=>row?.experiment_id===experimentId),experimentId};
}

export function NeighborStabilityCard({evidence}){
  const rows=evidence.rows,row=rows.length===1?rows[0]:null;
  const periods=row?[["training","2024选型"],["evaluation","2025基准"],["stress","2025费用压力"],["capacity","2025容量压力"]].map(([key,period])=>({...row[key],period})):[];
  return <DataComponent id="explorer:neighbors" queryId="research_v4_neighbors" variant="card" kind="custom" title="所选候选的单轴邻域稳定性" sourceRows={rows} displayRows={rows} description="同idea_id，仅窗口、股票范围、目标只数、调仓频率中的一个轴不同。正收益比例只以已观测邻居为分母；失败与未知保留在应有分母中。">
    {row?<div data-reviewed-rows>
      <p className="fx-count">候选 {evidence.experimentId} · 邻居 {number(row.neighbor_count)} 项 · 2024邻居夏普中位数 {number(row.training?.sharpe_median)}</p>
      <DataTable rows={periods} columns={columns} searchable={false} caption="所选候选单轴邻居的分期稳定性与有效分母"/>
      <p className="fx-count">2024夏普分母：已观测 {number(row.training?.sharpe_observed)} / 应有 {number(row.training?.sharpe_expected)}，失败 {number(row.training?.sharpe_failed)}，未知 {number(row.training?.sharpe_unknown)}。</p>
      <p className="fx-boundary">{row.isolation?.description||"孤立峰诊断暂无，不能据此认定稳定。"} 这是事后描述性补充，参数变体不构成独立证据，不改变既定选型、收益或研究筛选状态。</p>
    </div>:<div className="fx-empty">{rows.length>1?"当前候选有多条邻域汇总，尚不能确定唯一记录。":"当前候选暂无匹配的邻域汇总。"}</div>}
  </DataComponent>;
}
