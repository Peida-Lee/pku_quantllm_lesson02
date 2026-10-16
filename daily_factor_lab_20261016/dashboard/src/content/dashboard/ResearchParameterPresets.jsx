import React from "react";
import { isFiniteNumber } from "./execution-config.js";

export const RESEARCH_PARAMETER_PRESETS = [
  {id:"research-2025",label:"2025 主回测",start:"2025-01-02",end:"2025-12-31"},
  {id:"coursework-2026",label:"作业补充至 2026-09-30",start:"2025-01-02",end:"2026-09-30"},
];

export function capitalLabel(value){
  if(!isFiniteNumber(value))return "暂无";
  if(+value>=100000000)return `${(+value/100000000).toLocaleString("zh-CN",{maximumFractionDigits:4})} 亿元`;
  if(+value>=10000)return `${(+value/10000).toLocaleString("zh-CN",{maximumFractionDigits:2})} 万元`;
  return `${(+value).toLocaleString("zh-CN")} 元`;
}

export function ResearchParameterPresets({config,setConfig}){
  return <div className="research-parameter-presets" aria-label="研究参数预设">
    <span>载入参数</span>
    {RESEARCH_PARAMETER_PRESETS.map(preset=><button type="button" key={preset.id}
      aria-pressed={config.start===preset.start&&config.end===preset.end&&config.capital===1000000000&&config.commission===0.0001&&config.execution_model==="daily_conservative"&&config.st_policy==="price_proxy"&&config.liquidate_end===true&&config.wind_down_days===20}
      onClick={()=>setConfig(current=>({...current,start:preset.start,end:preset.end,capital:1000000000,commission:0.0001,min_commission:5,slippage:.0005,min_avg_amount:20000000,min_active_days20:18,min_history:120,min_price:2,execution_model:"daily_conservative",st_policy:"price_proxy",universe:"all_a",liquidity_top_n:0,risk_proxy_window:20,max_participation:.01,max_daily_participation:.01,open_guard:.048,liquidate_end:true,wind_down_days:20}))}>{preset.label}</button>)}
    <small>10亿元 · 单边佣金1bp · 卖出印花税5bp · 建议预定20日退出</small>
  </div>;
}
