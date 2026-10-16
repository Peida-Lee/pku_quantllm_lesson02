"""Attach pure aggregate neighbor diagnostics to one verified study snapshot."""
import hashlib
import json
from pathlib import Path
from lab import ROOT


def attach_diagnostics(snapshot):
    study=snapshot.get("research_v4")
    if not study or not study.get("snapshot_ready") or study.get("validation",{}).get("passed") is not True:
        return
    from v4_diagnostics import diagnose
    base=(ROOT/"research_outputs").resolve()
    directory=(base/study["study_id"]).resolve()
    directory.relative_to(base)
    diagnostic_directory=directory/"neighbor_diagnostics"
    path=diagnostic_directory/"diagnostics.json"
    digest=hashlib.sha256((ROOT/"v4_diagnostics.py").read_bytes()).hexdigest()
    diagnostic=None
    if path.is_file():
        try:
            previous=json.loads(path.read_text(encoding="utf-8"))
            if previous.get("study_id")==study["study_id"] and previous.get("source_snapshot_revision")==study["snapshot_revision"] and previous.get("script_sha256")==digest:
                diagnostic=previous
        except (ValueError,OSError):pass
    if diagnostic is None:diagnostic=diagnose(study,diagnostic_directory)
    if diagnostic.get("study_id")!=study["study_id"] or diagnostic.get("source_snapshot_revision")!=study["snapshot_revision"] or diagnostic.get("script_sha256")!=digest:
        raise ValueError("V4邻域诊断身份不匹配，拒绝拼接快照")
    source={"label":"同族单轴变化的参数邻域诊断（事后描述）","executedAt":diagnostic["generated_at"],
            "timezone":"Asia/Shanghai","study_id":study["study_id"],"source_snapshot_revision":study["snapshot_revision"],"script_sha256":digest,
            "files":["v4_diagnostics.py",f"research_outputs/{study['study_id']}/result.json",f"research_outputs/{study['study_id']}/neighbor_diagnostics/diagnostics.json"],
            "filters":["同idea_id，window/universe/top_n/rebalance恰好一个轴变化","每个情景仅以成功且有限的邻居作统计分母，failed/unknown另列"],
            "caveats":diagnostic.get("limitations",[]),
            "metricDefinitions":[{"label":"邻居正收益比例","definition":"该情景邻居成功且有限收益中的正收益项数/observed；expected、failed、unknown分别单列，不当成独立证据"}]}
    for name,rows in (("neighbors",diagnostic["neighbors"]),("families",diagnostic["family_summary"])):
        snapshot["queries"]["research_v4_"+name]={"rows":rows,"source":source,
            "methods":[{"language":"python","code":"python v4_diagnostics.py research_outputs/"+study["study_id"]+"; purely aggregate post-hoc diagnostic, no reselection or simulation"}]}
    study["files"].update({key:f"{study['study_id']}/neighbor_diagnostics/{file}" for key,file in {
        "neighbor_diagnostics":"neighbor_diagnostics.csv","family_summary":"family_summary.csv","neighbor_report":"report.md"}.items()})
