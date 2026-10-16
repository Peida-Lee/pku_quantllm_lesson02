export const isFiniteNumber=value=>typeof value==="number"&&Number.isFinite(value);
export const numericInputValue=value=>value===""?"":Number(value);
export const scaledNumber=(value,scale)=>isFiniteNumber(value)?value*scale:null;

const limits={capital:[10000,1e10,"初始资金"],top_n:[1,500,"目标入选只数",true],min_history:[65,500,"历史门槛",true],min_active_days20:[1,20,"20日活跃天数",true],commission:[0,.01,"佣金"],slippage:[0,.01,"滑点"],min_commission:[0,100,"最低佣金"],min_price:[0,100,"最低股价"],min_avg_amount:[0,1e11,"平均成交额门槛"],writeoff_missing_days:[1,120,"缺价减记天数",true],liquidity_top_n:[0,6000,"流动性范围",true],risk_proxy_window:[1,120,"风险代理窗口",true],wind_down_days:[0,120,"提前退出天数",true],open_guard:[0,.3,"开盘过滤阈值"],max_participation:[0,.1,"前均额容量参与率"],max_daily_participation:[0,.1,"当日容量参与率"],cash_buffer:[0,.5,"现金缓冲"]};
const dateValid=value=>typeof value==="string"&&/^\d{4}-\d{2}-\d{2}$/.test(value)&&Number.isFinite(Date.parse(`${value}T00:00:00Z`))&&new Date(`${value}T00:00:00Z`).toISOString().slice(0,10)===value;

export function executionConfigError(config){
  if(!dateValid(config.start)||!dateValid(config.end))return "请填写有效的起止日期（YYYY-MM-DD）。";
  if(config.start>config.end)return "开始日期不能晚于结束日期。";
  for(const key of ["capital","top_n","wind_down_days"])if(config[key]===undefined)return `实验参数缺少${limits[key][2]}。`;
  for(const [key,value] of Object.entries(config)){
    const rule=limits[key];if(!rule)continue;
    const [min,max,label,integer]=rule;
    if(!isFiniteNumber(value))return `${label}不能为空，且须为有限数值；如需0，请明确输入0。`;
    if(value<min||value>max||integer&&!Number.isInteger(value))return `${label}须为${min}至${max}${integer?"的整数":"之间的数值"}。`;
    if(["open_guard","max_participation","max_daily_participation"].includes(key)&&value===0)return `${label}须大于0。`;
    if(key==="cash_buffer"&&value===.5)return "现金缓冲须小于0.5。";
  }
  if(!["daily","weekly","monthly"].includes(config.rebalance))return "请选择有效的调仓频率。";
  if(!["legacy","daily_conservative"].includes(config.execution_model))return "请选择有效的执行模型。";
  if(config.wind_down_days>0&&config.execution_model!=="daily_conservative")return "提前退出窗口需要保守日线执行模型；请切换执行模型，或将退出天数明确设为0。";
  if(config.st_policy==="strict")return "原ZIP没有历史ST标签，无法运行真实ST剔除政策。";
  if(!["legacy","price_proxy"].includes(config.st_policy))return "请选择可用的ST信息政策。";
  if(!["all_a","main_a","growth_a"].includes(config.universe))return "请选择有效的股票范围。";
  if(typeof config.liquidate_end!=="boolean")return "末日处理参数须为明确的布尔值。";
  return "";
}
