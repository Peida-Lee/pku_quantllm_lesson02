"""Render independently accepted aggregates only. Usage: python render_report.py OUT."""
import argparse
import html
import json
import math
import os
import re
from pathlib import Path

PERIODS = ('reference_2024', 'evaluation_2025', 'evaluation_2026sep')
POOLS = ('all_liquid', 'main_liquid', 'liquid1000')
METRICS = ('ic', 'rank_ic', 'spread_q10_q1', 'beta', 'p25_p75_difference', 'conditional_beta')
SCENARIO_LABELS = {'base': '基础：日金额容量1%', 'double_cost': '全部模型费用及滑点翻倍', 'capacity_0p1pct': '日金额容量1%降至0.1%'}
AUDIT_SCOPE = ('独立复核覆盖36个信息cohort、20条逐日组合账本及6组配对统计，并从原ZIP抽取三个交易日独立抽算。'
               '这不是全量逐笔交易独立重放，也不是条件beta对全部股票日的独立重算；验收通过不证明真实ST排除或开盘可成交性。')


def local_inline(text):
    """Escape prose and activate only local relative Markdown file links."""
    parts, start = [], 0
    for match in re.finditer(r'\[([^\]\n]+)\]\(([^)\s]+)\)', text):
        parts.append(html.escape(text[start:match.start()]))
        label, target = match.groups()
        if ':' not in target and not target.startswith(('/', '\\')) and '\\' not in target:
            parts.append('<a href="' + html.escape(target, quote=True) + '">' + html.escape(label) + '</a>')
        else:
            parts.append(html.escape(match.group()))
        start = match.end()
    parts.append(html.escape(text[start:]))
    return ''.join(parts)


def html_document(markdown):
    body, in_code, in_table = [], False, False
    for line in markdown.splitlines():
        if line.startswith('```'):
            if in_table:
                body.append('</table>'); in_table = False
            body.append('</pre>' if in_code else '<pre>')
            in_code = not in_code
        elif in_code:
            body.append(html.escape(line) + '\n')
        elif line.startswith('|'):
            if not in_table:
                body.append('<table>'); in_table = True
            cells = line.strip('| ').split(' | ')
            if not all(c.strip() == '---' for c in cells):
                body.append('<tr>' + ''.join('<td>' + local_inline(c) + '</td>' for c in cells) + '</tr>')
        else:
            if in_table:
                body.append('</table>'); in_table = False
            if line.startswith('!['):
                caption, source = line[2:].split('](', 1)
                body.append(f'<figure><img src="{html.escape(source[:-1], quote=True)}" alt="{html.escape(caption, quote=True)}"></figure>')
            elif line.startswith('#'):
                level = len(line) - len(line.lstrip('#'))
                body.append(f'<h{level}>' + html.escape(line[level:].strip()) + f'</h{level}>')
            elif line:
                body.append('<p>' + local_inline(line) + '</p>')
    if in_table:
        body.append('</table>')
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>5DR五维评价</title><style>body{font:15px/1.7 "Microsoft YaHei",sans-serif;max-width:1200px;margin:30px auto;padding:0 20px;color:#243447}table{border-collapse:collapse;display:block;overflow:auto;font-size:12px}td{border:1px solid #ccd5df;padding:7px;max-width:530px;min-width:85px;vertical-align:top}tr:first-child{background:#eaf1f7;font-weight:bold}img{max-width:100%}pre{overflow:auto;background:#f4f6f8;padding:15px;font-size:12px}h2{margin-top:35px}a{color:#1261a0}</style><body>' + '\n'.join(body) + '</body></html>'


def fmt(x):
    if x is None:
        return '缺失'
    if isinstance(x, bool):
        return '是' if x else '否'
    if isinstance(x, (list, tuple)):
        return '[' + ', '.join(fmt(v) for v in x) + ']'
    if isinstance(x, (int, float)):
        return f'{x:.6g}' if math.isfinite(x) else '缺失'
    return str(x).replace('|', '\\|').replace('\n', ' ')


def table(headers, rows):
    return '\n'.join(['| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join(['---'] * len(headers)) + ' |'] +
                     ['| ' + ' | '.join(fmt(v) for v in row) + ' |' for row in rows])


def render(output, pool_sensitivity=None):
    out = Path(output).resolve(strict=True)
    result = json.loads((out / 'result.json').read_text(encoding='utf-8'))
    validation = result.get('independent_validation', {})
    if validation.get('passed') is not True:
        raise RuntimeError('拒绝渲染：result.json 的 independent_validation.passed 必须为 true。')
    if result.get('failures'):
        raise RuntimeError('拒绝渲染：result.json 仍含计算失败。')
    import pandas as pd
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.font_manager import FontProperties
    font = FontProperties(fname=r'C:\Windows\Fonts\msyh.ttc')
    plt.rcParams.update({'font.family': font.get_name(), 'axes.unicode_minus': False, 'figure.dpi': 150})
    info = result['information']['periods']
    primary_pool = result['protocol'].get('primary_universe', 'all_liquid')
    if primary_pool != 'all_liquid':
        raise ValueError('本报告主池固定为all_liquid，协议primary_universe不一致。')
    execution = result['execution']
    pool_result = None
    pool_summaries = {'all_liquid': execution}
    if pool_sensitivity is not None:
        pool_dir = Path(pool_sensitivity).resolve(strict=True)
        pool_result = json.loads((pool_dir / 'result.json').read_text(encoding='utf-8'))
        if pool_result['status'] != 'validated' or pool_result['independent_validation']['passed'] is not True or pool_result['failures']:
            raise RuntimeError('拒绝渲染：补充股票池敏感性尚未独立验收通过或存在失败。')
        if Path(pool_result['all_liquid_reference']).resolve() != (out / 'execution' / 'execution_summary.json').resolve():
            raise ValueError('补充结果引用的all_liquid主运行与本目录不一致。')
        for pool in ('main_liquid', 'liquid1000'):
            pool_summaries[pool] = json.loads(Path(pool_result['pools'][pool]['execution_summary']).read_text(encoding='utf-8'))
            if pool_summaries[pool]['failures']:
                raise ValueError('补充执行汇总仍含失败。')
    daily = pd.read_csv(out / 'information' / 'information_daily.csv')
    deciles = pd.read_csv(out / 'information' / 'information_deciles.csv')
    figs = out / 'figures'
    figs.mkdir(exist_ok=True)
    images = []

    def save(fig, name, caption):
        fig.tight_layout()
        fig.savefig(figs / name, bbox_inches='tight')
        plt.close(fig)
        images.append((name, caption))

    fig, ax = plt.subplots(figsize=(8, 4))
    for metric, offset, label in [('ic', -.12, 'IC'), ('rank_ic', .12, 'RankIC')]:
        estimates = [info['evaluation_2025'][primary_pool][str(h)][metric] for h in (1, 5, 10, 20)]
        x = [i + offset for i in range(4)]
        for xx, s in zip(x, estimates):
            if s['mean'] is not None and s['ci95_low'] is not None:
                ax.vlines(xx, s['ci95_low'], s['ci95_high'], color='C0' if metric == 'ic' else 'C1')
        ax.plot(x, [s['mean'] for s in estimates], 'o-', label=label)
    ax.set_xticks(range(4), ['1日', '5日（主）', '10日', '20日'])
    ax.axhline(0, color='grey', lw=.7)
    ax.set(title='2025 原包A股流动性主池：-R5 预测信息及 HAC 95%CI', ylabel='每日截面相关系数均值')
    ax.legend()
    save(fig, '01_horizon_information.png', '2025 all_liquid；误差线为各期限协议 HAC 均值95%CI。')

    d = deciles[(deciles.period == 'evaluation_2025') & (deciles.universe == primary_pool) & (deciles.horizon == 5)]
    means = d.groupby('decile').mean_simple_return.mean().reindex(range(1, 11))
    if means.notna().sum() != 10:
        raise ValueError('十分层数据不完整，拒绝绘制。')
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.bar(means.index, means * 100)
    ax.set_xticks(range(1, 11))
    ax.axhline(0, color='grey', lw=.7)
    ax.set(title='2025 主池 h5：-R5 十分层', xlabel='评分从低 Q1 至高 Q10', ylabel='5日简单收益均值（%）')
    save(fig, '02_deciles.png', '每日组内简单均值，再对有效日期等权平均；无费、重叠5日收益，不作复利或可空组合解释。')

    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharey=True)
    for ax, period in zip(axes, PERIODS):
        d = daily[(daily.period == period) & (daily.universe == primary_pool) & (daily.horizon == 5)].copy()
        d = d.sort_values('date')
        ax.plot(pd.to_datetime(d.date), d.rank_ic, alpha=.35, label='每日 RankIC')
        ax.plot(pd.to_datetime(d.date), d.rank_ic.rolling(20, min_periods=20).mean(), label='20源日期滚动均值')
        ax.axhline(0, color='grey', lw=.7)
        ax.set(title=period, ylabel='RankIC')
        ax.legend(loc='upper left', fontsize=8)
    save(fig, '03_rankic_periods.png', '主池 h5 RankIC 时间序列；保留源日期空值，滚动均值要求20日完整有效。这是相关系数，不是收益。')

    fig, axes = plt.subplots(2, 1, figsize=(10, 7))
    capital = result['protocol']['execution_base_config']['capital']
    for ax, period in zip(axes, PERIODS[1:]):
        for strategy, label in [('5dr', '-R5'), ('base_combo', 'P3'), ('added_combo', 'P4'), ('5dr_lag1', '-R5额外延迟1日')]:
            d = pd.read_csv(out / 'execution' / f'{period}__{strategy}__base' / 'daily.csv')
            ax.plot(pd.to_datetime(d.date), d.nav / capital, label=label)
        ax.set(title=period + '：基础费用净值', ylabel='净值 / 初始资本')
        ax.legend(fontsize=8)
    save(fig, '04_execution_nav.png', '日线约束模型下净值，已含模型费用与可能减记；减记不是卖出，非实盘可实现性证明。')

    pool_preview = []
    if pool_result is not None:
        def pool_run(pool, period):
            return next(r for r in pool_summaries[pool]['runs'] if r['period'] == period and r['strategy'] == '5dr' and r['scenario'] == 'base')

        pool_rows = []
        for period in PERIODS[1:]:
            for pool in POOLS:
                r = pool_run(pool, period)
                m, t = r['metrics'], r['terminal']
                rank_ic = info[period][pool]['5']['rank_ic']['mean']
                pool_rows.append([period, pool, m['days'], f"{m['total_return']:.4%}", f"{m['max_drawdown']:.4%}", m['sharpe'], m['annual_two_sided_turnover'], m['writeoff_loss'], t['residual_value'], t['fully_exited_by_trades'], rank_ic])
        pool_table = table(['期间', '动态股票池', '交易日', '−R5净总收益', '最大回撤', '净Sharpe', '年双边换手', '减记损失/元', '残仓估值/元', '全由交易退出', 'h5 RankIC'], pool_rows)
        eligibility_note = ('三池按同一10亿元初始资金、目标100只、周调仓、仅多头及相同成本/执行参数对照，不按结果挑选赢家。'
                            'all_liquid为原包A股动态资格池，main_liquid限主板，liquid1000为动态资格池中按信号日历史20日均额排序的前1000只；不拿当前名单回填历史。'
                            '共同资格为至少120个有效历史收盘、价格≥2元、20日均额≥2000万元、20日内至少18日活跃，加price_proxy代理筛选。'
                            '原包是否覆盖历史退市股票未知，存在上市/生存覆盖限制；不能称官方全市场。真实历史ST未知，行业/市值缺失，不能把池效应作行业或规模因果分解。')
        count_summary = pool_result['pools']['main_liquid']['pool_counts_summary']['evaluation_2025']
        counts = [count_summary['all_liquid_count']['median'], count_summary['eligible_count']['median'],
                  pool_result['pools']['liquid1000']['pool_counts_summary']['evaluation_2025']['eligible_count']['median']]
        eligibility_note += (' 2025每日合格数量中位数依次为' + '/'.join(fmt(c) for c in counts) +
                             '只，固定top100的选中比例约' + '/'.join(f'{100/c:.2%}' for c in counts) +
                             '。这是固定持股数下的股票池敏感性，截面尾部强度也同时改变，不能称纯因果股票池效应；不因2025表现较高而选择liquid1000为赢家。')
        comparison_note = ('三个股票池得到不同的收益和回撤；尚未检验池间差异是否统计显著。2025与2026年1—9月样本长度不同，应在各期间内比较股票池，不直接把两个期间总收益当同长度年度收益比较。'
                           'liquid1000在2025年的减记约1072.68万元，即使期末残仓为0，也不代表全部由交易卖出；all_liquid在2026年仍有约45.20万元模型残仓。'
                           '2026三池均未检出明确RankIC关联，未进行跨期差异检验，不能说相对2025显著下降。'
                           '2025主池条件beta原始P约0.0538，主板池和前1000池则分别约0.0212、0.0287，说明条件评价也依赖股票池；这些辅助比较未经多重搜索校正。'
                           '三池基础情景的P4与P3配对净收益差均未发现明确证据，不能把条件关联直接解释成可实现的组合增益。')
        pool_information_rows = []
        for period in PERIODS[1:]:
            for pool in POOLS:
                cell = info[period][pool]['5']
                ri, cb = cell['rank_ic'], cell['conditional_beta']
                pool_information_rows.append([period, pool, ri['mean'], [ri['ci95_low'], ri['ci95_high']], ri['p_raw'], cb['mean'], [cb['ci95_low'], cb['ci95_high']], cb['p_raw']])
        pool_information_table = table(['期', '池', 'RankIC', 'RankIC HAC95%CI', '原始P', '条件beta', '条件beta HAC95%CI', '原始P'], pool_information_rows)
        fig, axes = plt.subplots(1, 2, figsize=(11, 4))
        labels = ['all_liquid', 'main_liquid', 'liquid1000']
        for ax, metric, title in zip(axes, ('total_return', 'max_drawdown'), ('基础−R5净总收益（%）', '基础−R5最大回撤（%）')):
            for i, period in enumerate(PERIODS[1:]):
                ax.bar([j + (i-.5)*.36 for j in range(3)], [100*pool_run(p, period)['metrics'][metric] for p in POOLS], width=.36, label='2025全年' if i == 0 else '2026年1—9月')
            ax.set_xticks(range(3), labels, rotation=12)
            ax.axhline(0, color='grey', lw=.7)
            ax.set_title(title)
            ax.legend(fontsize=8)
        save(fig, '05_pool_comparison.png', '同10亿元/目标100只/周调仓，3个动态池；期间长度不同，图为描述性敏感性，减记已计入净表现。')
        pool_image = '![三池收益与回撤对照](figures/05_pool_comparison.png)'
        supplemental_scope = ('补充独立验收通过2443项：核对40条补充逐日账本的13项表现、收益/现金/配置/日历与终态语义，'
                              '12组配对差及HAC/相对alpha；核对全期池数量/转换，并在每池每期间首中末日抽算池数量。'
                              '沿用已验收all_liquid原20条运行，原13830项验收未重跑。未独立逐笔重建现金/头寸/价格/费用/容量；'
                              '资格抽算复用冻结Market.eligible，非独立资格实现，完整5DR路径另行构建。不能证明历史ST或开盘成交。')
        source_link = Path(os.path.relpath(pool_dir / 'result.json', out)).as_posix()
        pool_preview = ['## 股票集合敏感性：同规则三池对照', eligibility_note, pool_table, comparison_note, pool_image,
                        'h5信息关联与条件关联的股票池对照：', pool_information_table,
                        supplemental_scope, '[完整三池敏感性](POOL_SENSITIVITY.md) · [三池HTML](POOL_SENSITIVITY.html)']
        pool_sections = ['# 股票集合执行敏感性', eligibility_note, pool_table, comparison_note, pool_image, supplemental_scope,
                         '## h5预测信息和条件关联', pool_information_table,
                         '补充原文件：[已验收补充结果](' + source_link + ')；主结果：[原始已验收结果](result.json)。',
                         '## 全部压力情景（60条运行）',
                         table(['池', '期', '策略', '情景', '日数', '净总收益', '净Sharpe', '最大回撤', '年双边换手', '减记/元', '残仓/元', '全由交易退出'],
                               [[pool, r['period'], r['strategy'], SCENARIO_LABELS[r['scenario']]] + [r['metrics'][k] for k in ('days', 'total_return', 'sharpe', 'max_drawdown', 'annual_two_sided_turnover', 'writeoff_loss')] + [r['terminal']['residual_value'], r['terminal']['fully_exited_by_trades']] for pool in POOLS for r in pool_summaries[pool]['runs']]),
                         '## P3/P4配对对照', 'IR(P4−P3)不等于ΔIR；原三项权重同时变化，跨0不表示等效。',
                         table(['池', '期', '情景', '配对日数', 'IR(P4−P3)', '日均差95%CI', '原始P', '净Sharpe差', '回撤差', '日alpha', 'alpha95%CI', 'alpha原始P'],
                               [[pool, r['period'], SCENARIO_LABELS[r['scenario']], r['n_paired_days'], r['paired_active_return_annualized_IR_P4_minus_P3'], r['HAC_mean_difference_CI95'], r['HAC_raw_P'], r['sharpe_difference'], r['paired_drawdown_change_added_minus_base'], r['relative_three_factor_alpha']['daily_alpha'], r['relative_three_factor_alpha']['HAC_daily_alpha_CI95'], r['relative_three_factor_alpha']['HAC_alpha_raw_P']] for pool in POOLS for r in pool_summaries[pool]['paired']])]
        pool_markdown = '\n\n'.join(pool_sections) + '\n'
        (out / 'POOL_SENSITIVITY.md').write_text(pool_markdown, encoding='utf-8')
        (out / 'POOL_SENSITIVITY.html').write_text(html_document(pool_markdown), encoding='utf-8')

    main = info['evaluation_2025'][primary_pool]['5']
    best_decile = int(means.idxmax())
    monotonic_note = ('十分层均值并不随评分单调上升。' if not means.diff().dropna().ge(0).all()
                      else '十分层均值在本期呈非递减形状，但不构成未来单调性保证。')
    shape_note = (f'分层形状：2025 h5最高组均值在Q{best_decile}（{means.loc[best_decile]:.4%}），'
                  f'Q1为{means.loc[1]:.4%}，Q10为{means.loc[10]:.4%}。'
                  + monotonic_note +
                  '这10个组的均值不应仅用顶底差概括；最高评分组不一定是收益最高组，分层均值是回顾性形状诊断，不据此追加阈值优化。')
    def assessment(s):
        if s.get('ci95_low') is None:
            return '有效推断不足'
        return ('区间跨0，未发现明确方向' if s['ci95_low'] <= 0 <= s['ci95_high'] else
                '区间位于0以上' if s['ci95_low'] > 0 else '区间位于0以下')
    primary_exec = next(r for r in execution['runs'] if r['period'] == 'evaluation_2025' and r['strategy'] == '5dr' and r['scenario'] == 'base')
    paired = next(r for r in execution['paired'] if r['period'] == 'evaluation_2025' and r['scenario'] == 'base')
    sections = ['# 5DR 五维评价报告',
        f"运行：`{result['run_id']}`；北京时间。独立验收通过。2025为主评价，2024仅统计参照，2026年1—9月为固定分期对照。",
        AUDIT_SCOPE,
        '## 定义与结论边界',
        '原始 R5=C_adj[t]/C_adj[t-5]−1；主评分为 −R5，要求完整过去六行价格路径。原始R5的IC/RankIC与主评分符号相反，下表并列披露；两个方向不算独立发现。目标为下一日开盘至h日后开盘的复权简单收益，完整开盘/复权路径、进出均落在期间内；h5为主。所有区间此前均已观察，不能称真正未触碰的样本外。',
        '主池all_liquid：原包全部A股中的动态合格池，20日均额≥2000万元；原包是否包含历史所有上市股票无法独立证明。price_proxy仅为价格代理筛选，不代表准确排除真实历史ST。main_liquid与liquid1000仅作敏感性对照。成交金额按人民币元解释沿用原假设，缺少供应商单位字典的独立确认。',
        'IC是每日横截面上因子值与未来收益的Pearson相关；Rank IC是二者转为秩后的Spearman相关，回答能否排序。本文主评分是−R5，因此正Rank IC对应短期反转；若直接使用原始R5，同一结果为负。ICIR是日IC均值除以日IC标准差，这里仅作描述，不能当策略Sharpe、HAC t或独立日样本的年化指标。',
        '经济斜率beta来自未来收益对信号日已确定百分位的横截面回归；第25到75百分位对应0.5×beta的预测收益差。条件beta仅控制momentum20、lowvol20、liquidity20的百分位，不含行业、市值或官方风险因子。有效日n是时间序列推断样本数，股票×日配对数量不能当作独立观测量。',
        '交易对照固定10亿元、目标100只、每周调仓、仅多头，延续佣金单边1bp和卖出印花税5bp，另含最低佣金5元、过户费和不利刻度滑点。提前20个源交易日退出来自先前回顾性修复；统计标签仍评价所有期内可完整观察窗口，因此与提前退出的组合持有区间不同。',
        '## 五维结论（2025 all_liquid h5为统计主格）',
        table(['维度', '实际结果与解释'], [
            ['预测信息', f"RankIC={fmt(main['rank_ic']['mean'])}；{assessment(main['rank_ic'])}；IC={fmt(main['ic']['mean'])}"],
            ['经济幅度', f"Q10−Q1={fmt(main['spread_q10_q1']['mean'])}；{assessment(main['spread_q10_q1'])}；beta={fmt(main['beta']['mean'])}"],
            ['统计可信度', f"RankIC有效日n={fmt(main['rank_ic']['n'])}；HAC t={fmt(main['rank_ic']['t'])}，原始P={fmt(main['rank_ic']['p_raw'])}；分期是回顾性稳定性检查"],
            ['交易可实现性', f"基础5DR净Sharpe={fmt(primary_exec['metrics'].get('sharpe'))}，年双边换手={fmt(primary_exec['metrics'].get('annual_two_sided_turnover'))}；成本/延迟/容量见压力表"],
            ['组合增量', f"条件beta={fmt(main['conditional_beta']['mean'])}；{assessment(main['conditional_beta'])}；IR(P4−P3)={fmt(paired.get('paired_active_return_annualized_IR_P4_minus_P3'))}；不能称ΔIR"]]),
        shape_note,
        '## 全部分期、池与期限统计',
        '均值与区间均为简单收益或相关系数原单位。mean/std只作描述，不年化、不充当t；CI为正态近似HAC。只报告原始P，多期限/池/指标未校正多重搜索。',
    ]
    rows, coverage, sensitivity = [], [], []
    for period in PERIODS:
        for pool in POOLS:
            for h in (1, 5, 10, 20):
                cell = info[period][pool][str(h)]
                for metric in METRICS:
                    s = cell[metric]
                    rows.append([period, pool, h, metric, s.get('n'), s.get('mean'), -s['mean'] if metric in ('ic', 'rank_ic') and s.get('mean') is not None else '—', [s.get('ci95_low'), s.get('ci95_high')], s.get('lag'), s.get('t'), s.get('p_raw'), s.get('mean_over_std')])
                    for lag, v in s.get('hac_sensitivity', {}).items():
                        sensitivity.append([period, pool, metric, lag, v.get('n'), [v.get('ci95_low'), v.get('ci95_high')], v.get('t'), v.get('p_raw')])
                c = cell['coverage']
                denom = c['in_period_signal_total']
                coverage.append([period, pool, h, denom, c['in_period_paired_total'], c['in_period_missing_total'], c['in_period_paired_total']/denom if denom else None, c['boundary_censored_signal_total']])
    sections += [table(['期', '池', 'h', '指标', '有效日n', '−R5均值', '原R5均值（仅IC）', '95%CI', 'HAC L', 't', '原始P', '均值/std'], rows),
        '## h5 HAC滞后敏感性', table(['期', '池', '指标', 'L', 'n', '95%CI', 't', '原始P'], sensitivity),
        '## 费用、容量与额外延迟执行压力',
        '2024不进行执行回测。各项收益、Sharpe、回撤为模型净表现；年双边换手含买入和卖出。base为基础费用、日金额容量1%；double_cost将佣金/最低佣金、印花税、过户费与滑点翻倍，刻度取整可使实际总成本不恰好翻倍；capacity_0p1pct将日金额容量1%降至0.1%；5dr_lag1在原信号执行基础上额外延迟1个源交易日。容量均为事后日线约束，不是开盘成交量。',
        table(['期', '策略', '情景', '日数', '净总收益', '净Sharpe', '最大回撤', '年双边换手', '总成本', '减记损失'],
              [[r['period'], r['strategy'], SCENARIO_LABELS[r['scenario']]] + [r['metrics'].get(k) for k in ('days', 'total_return', 'sharpe', 'max_drawdown', 'annual_two_sided_turnover', 'costs', 'writeoff_loss')] for r in execution['runs']]),
        '## 组合增量',
        'P3为momentum20、lowvol20、liquidity20三项等权rank；P4加入−R5后四项等权。二者采用同一四项过去均有效池，但原三项权重同时变化，不能解释成固定其余权重的纯边际效应。相对alpha来自净rP4=a+b净rP3，不是CAPM或官方风险模型alpha。没有共同可投资基准，IR(P4−P3)不能称ΔIR；回撤差为P4−P3，正值表示回撤较浅。',
        table(['期', '情景', '配对日数', 'IR(P4−P3)', '日均差95%CI', '差原始P', '净Sharpe差', '回撤差', '日alpha', '日alpha95%CI', 'alpha原始P', 'alpha×252', 'alpha×252的95%CI'],
              [[r['period'], r['scenario'], r.get('n_paired_days'), r.get('paired_active_return_annualized_IR_P4_minus_P3'), r.get('HAC_mean_difference_CI95'), r.get('HAC_raw_P'), r.get('sharpe_difference'), r.get('paired_drawdown_change_added_minus_base')] + [r['relative_three_factor_alpha'].get(k) for k in ('daily_alpha', 'HAC_daily_alpha_CI95', 'HAC_alpha_raw_P', 'linear_annualized_alpha_252', 'HAC_linear_annualized_alpha_CI95')] for r in execution['paired']]),
        '## 覆盖、期末截断与缺失',
        '下表为股票×信号日期计数，不是独立样本数。有效统计日数见主统计表。期内覆盖率只在完整期间窗口分母内计算，边界截断单列，不能当作期内缺失。',
        table(['期', '池', 'h', '期内信号', '期内配对', '期内缺失', '期内覆盖率', '边界截断信号'], coverage),
        '源交易日历：\n\n```json\n' + json.dumps(result['source_calendars'], ensure_ascii=False, indent=2) + '\n```',
        '## 图形']
    for name, caption in images:
        sections.append(f'![{caption}](figures/{name})\n\n{caption}')
    if pool_result is not None:
        sections += ['## 三池执行敏感性补充', pool_table, comparison_note, supplemental_scope,
                     '[完整三池敏感性](POOL_SENSITIVITY.md) · [三池HTML](POOL_SENSITIVITY.html) · [已验收补充结果](' + source_link + ')']
    sections += ['## 残仓、拒单与分半期诊断',
        '提前20个源交易日退出继承旧后验修复，不能保证残仓为0。减记不是市场卖出。以下完整保留终态、拒单及日历分半期汇总，避免用净值掩盖执行失败。',
        '```json\n' + json.dumps([{'period': r['period'], 'strategy': r['strategy'], 'scenario': r['scenario'], 'terminal': r['terminal'], 'blocked': r['blocked'], 'calendar_halves': r.get('calendar_halves'), 'equal_trading_day_halves': r.get('equal_trading_day_halves')} for r in execution['runs']], ensure_ascii=False, indent=2) + '\n```',
        '## 局限与解释',
        '仅使用feathers.zip，行情截至2026-10-16，不含10月23日新行情。缺少真实历史ST、行业/市值、官方风险因子、可投资指数与现金利息、官方限价、开盘竞价深度和实物交收信息。价格代理不等于准确ST排除。日线OHLC/成交额和拒单是事后审计，不证明开盘时可成交；容量情景也不是实际市场冲击估计。复权合成单位不证明真实分红/股份交收。十分层Q10−Q1是gross重叠5日诊断，不能当可做空净利润或复利累加。相关性和条件斜率不构成因果识别。分期均已观察，选择方向沿用旧反转解释；不能宣称独立样本外或无多重搜索偏差。统计HAC使用n/(n−1)修正、L=max(5,h)，h5另列L10/L20；组合配对/alpha使用L5且不作小样本修正，不能混同两种设置。所有P为原始双侧P。区间跨0表示未发现明确差异，不能宣称等效。',
        '## 复现与审计文件',
        '先运行run_evaluation.py产生新目录，再执行独立验收；只有验收生成result.json且independent_validation.passed=true时，本渲染器才接受。渲染命令：`C:\\Python314\\python.exe render_report.py <evaluation目录>`。渲染仅读取聚合结果和CSV，不重算因子、统计或交易。',
        '[冻结协议](protocol.json) · [已验收结果](result.json) · [每日信息](information/information_daily.csv) · [十分层](information/information_deciles.csv) · [月度/半年度](information/information_monthly.csv)',
        '[完整独立验收明细](validation.json)；下列仅展示验收范围、计数与限制：\n\n```json\n' + json.dumps({k: v for k, v in validation.items() if k != 'checks'}, ensure_ascii=False, indent=2) + '\n```']
    markdown = '\n\n'.join(sections) + '\n'
    (out / 'REPORT.md').write_text(markdown, encoding='utf-8')
    (out / 'report.html').write_text(html_document(markdown), encoding='utf-8')

    def stat(cell, metric):
        s = cell[metric]
        return (f"{metric}={fmt(s['mean'])}，HAC95%CI={fmt([s['ci95_low'], s['ci95_high']])}，"
                f"原始P={fmt(s['p_raw'])}，n={fmt(s['n'])}；{assessment(s)}")

    def execution_row(period, strategy):
        return next(r for r in execution['runs'] if r['period'] == period and r['strategy'] == strategy and r['scenario'] == 'base')

    def net_description(period):
        values = []
        for strategy, label in [('5dr', '−R5'), ('base_combo', 'P3'), ('added_combo', 'P4')]:
            m = execution_row(period, strategy)['metrics']
            values.append(f"{label}净总收益={fmt(m['total_return'])}，Sharpe={fmt(m['sharpe'])}，最大回撤={fmt(m['max_drawdown'])}")
        m = execution_row(period, '5dr')['metrics']
        return '；'.join(values) + f"；−R5年双边换手={fmt(m['annual_two_sided_turnover'])}"

    def increment_description(period):
        r = next(r for r in execution['paired'] if r['period'] == period and r['scenario'] == 'base')
        a = r['relative_three_factor_alpha']
        ci = r['HAC_mean_difference_CI95']
        aci = a['HAC_daily_alpha_CI95']
        evidence = ('组合增量证据不足：日均差和相对alpha区间均跨0' if ci[0] <= 0 <= ci[1] and aci[0] <= 0 <= aci[1]
                    else '区间方向需分别判断；不能据单项结果宣称稳定增量')
        return (f"IR(P4−P3)={fmt(r['paired_active_return_annualized_IR_P4_minus_P3'])}；"
                f"日均净收益差95%CI={fmt(ci)}，原始P={fmt(r['HAC_raw_P'])}；"
                f"相对日alpha={fmt(a['daily_alpha'])}，HAC95%CI={fmt(aci)}，原始P={fmt(a['HAC_alpha_raw_P'])}；"
                f"净Sharpe差={fmt(r['sharpe_difference'])}，回撤差={fmt(r['paired_drawdown_change_added_minus_base'])}；{evidence}")

    preview_rows = []
    for dimension in ('预测信息', '经济幅度', '统计可信度', '交易可实现性', '组合增量'):
        cells = []
        for period in PERIODS[1:]:
            cell = info[period][primary_pool]['5']
            if dimension == '预测信息':
                text = stat(cell, 'ic') + '；' + stat(cell, 'rank_ic') + f"；原R5 RankIC={fmt(-cell['rank_ic']['mean'])}"
            elif dimension == '经济幅度':
                text = stat(cell, 'spread_q10_q1') + '；' + stat(cell, 'beta') + '；' + stat(cell, 'p25_p75_difference')
            elif dimension == '统计可信度':
                c = cell['coverage']
                text = (f"RankIC HAC t={fmt(cell['rank_ic']['t'])}、L={cell['rank_ic']['lag']}；"
                        f"有效日={cell['rank_ic']['n']}；期内配对/信号={c['in_period_paired_total']}/{c['in_period_signal_total']}；"
                        f"边界截断={c['boundary_censored_signal_total']}；回顾性分期，非真正样本外")
            elif dimension == '交易可实现性':
                text = net_description(period)
            else:
                text = stat(cell, 'conditional_beta') + '；' + increment_description(period)
            cells.append(text)
        preview_rows.append([dimension] + cells)
    preview_sections = [
        '# 5DR五维评价：本地预览（先审后传）',
        f"运行：{result['run_id']}。本预览仅存本机，未上传；请先审阅，再决定是否及上传哪些材料。",
        *pool_preview,
        '主评分−R5检验短期反转；正IC/RankIC表示反转方向，直接使用原始R5时符号相反。2025为主评价，2026年1—9月为固定回顾性对照，2024仅统计参照。所有期间均已观察。主池为原包A股的all_liquid动态合格池，20日均额≥2000万元；另两池仅为敏感性。',
        '以下相关系数/收益/斜率均用原单位（0.01收益=1%）；CI为HAC95%区间，P均为原始双侧P，未经多重搜索校正。区间跨0表示未发现明确差异，不代表等效。',
        table(['五个维度', '2025主评价：all_liquid h5 / 基础费用', '2026年1—9月对照：同口径'], preview_rows),
        shape_note + ' 全部分层形状见下图；不以顶底差代替单调性，也不据此再挑阈值。十分层为每天等权的gross重叠5日简单收益诊断，不能作为可空组合净利润或复利累加。',
        '交易固定10亿元、目标top100、每周调仓、仅多头。压力情景包括全部模型费用/滑点翻倍、日金额容量1%降至0.1%、−R5额外延迟1日；详见完整报告。日线约束和价格代理不能证明真实ST剔除或开盘可成交，缺少竞价深度、官方限价与实物交收。提前20源交易日退出继承旧回顾性修复，不保证残仓为0，减记不是卖出。',
        '条件beta只控制momentum20/lowvol20/liquidity20百分位，不含行业和市值。P3三项等权；P4加−R5后四项等权，原三项权重也改变。IR(P4−P3)是配对主动收益IR，不能称ΔIR；相对alpha为净rP4对净rP3的回归截距，不是官方风险模型alpha。',
        AUDIT_SCOPE,
        '## 图形预览（先看分层和跨期）',
    ]
    for index in (1, 2, 0, 3):
        name, caption = images[index]
        preview_sections.append(f'![{caption}](figures/{name})\n\n{caption}')
    preview_sections += ['## 原文件',
        '[完整HTML报告](report.html) · [完整Markdown报告](REPORT.md) · [冻结协议](protocol.json) · [已验收结果](result.json) · [每日信息](information/information_daily.csv) · [分层明细](information/information_deciles.csv) · [月度与半年度](information/information_monthly.csv)']
    preview = '\n\n'.join(preview_sections) + '\n'
    (out / 'PREVIEW.md').write_text(preview, encoding='utf-8')
    (out / 'preview.html').write_text(html_document(preview), encoding='utf-8')
    return out / 'REPORT.md'


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--pool-sensitivity', type=Path, help='独立验收通过的补充股票池目录')
    args = parser.parse_args()
    print(render(args.output, args.pool_sensitivity))
