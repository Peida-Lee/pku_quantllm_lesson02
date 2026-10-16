"""Validate V2 over its real loopback API, then restore the preferred main run.

Starts one bounded macro-strategy experiment. Does not issue real orders or call
GitHub. All restoration uses /api/runs/activate, never direct pointer edits.
"""
import argparse
import copy
import csv
from datetime import datetime
import io
import json
from pathlib import Path
import time
import traceback
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
OPENER = build_opener(ProxyHandler({}))


def node(op, *args, **params):
    return {"op": op, "args": list(args), "params": params}


class Verification:
    def __init__(self, base, restore_run):
        self.base, self.restore_run = base, restore_run
        self.checks, self.qa_run, self.saved_strategy = [], None, None
        self.job_started, self.restored = False, False

    def request(self, path, payload=None, origin=None, content_type="application/json", raw=None):
        data = raw if raw is not None else (json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None)
        headers = {"Content-Type": content_type}
        if origin is not None:
            headers["Origin"] = origin
        req = Request(self.base + path, data=data, headers=headers)
        try:
            response = OPENER.open(req, timeout=30)
        except HTTPError as e:
            response = e
        with response:
            body = response.read()
            return response.status, body, response.headers

    def json(self, path, payload=None, **kwargs):
        code, body, _ = self.request(path, payload, **kwargs)
        return code, json.loads(body)

    def check(self, name, condition, **detail):
        self.checks.append({"check": name, "passed": bool(condition), **detail})
        print(("PASS " if condition else "FAIL ") + name, flush=True)
        return bool(condition)

    def require(self, name, condition, **detail):
        if not self.check(name, condition, **detail):
            raise AssertionError(name)

    def idle(self, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, state = self.json("/api/status")
            if status != 200:
                raise RuntimeError("Status API failed")
            if not state["busy"]:
                return state
            time.sleep(1)
        raise TimeoutError("Server job did not finish before the bounded deadline")

    def snapshot(self, result, label):
        source = json.loads((ROOT / "dashboard/src/data.json").read_text(encoding="utf-8"))
        manifest = json.loads((ROOT / "dashboard/dist/data-app-build.json").read_text(encoding="utf-8"))
        status, sidecar = self.json("/" + quote(manifest["snapshot"]["path"]))
        expected_names = {row["name"] for row in result["summary"]}
        self.check(label, status == 200 and source["run_id"] == result["run_id"] == sidecar["run_id"]
                   and source["config"] == result["config"] == sidecar["config"]
                   and {r["name"] for r in sidecar["queries"]["summary"]["rows"]} == expected_names
                   and sidecar["buildStatus"] == "complete")

    def run(self):
        status, initial = self.json("/api/status")
        self.require("initial_server_idle_on_expected_main_run", status == 200 and not initial["busy"] and initial["latest_run"] == self.restore_run)
        status, before = self.json("/api/result")
        self.require("initial_main_result_available", status == 200 and before["run_id"] == self.restore_run and before["status"] == "complete")
        self.before = before
        status, catalog = self.json("/api/blocks")
        ids = [b["id"] for b in catalog["blocks"]]
        self.require("catalog_has_50_plus_unique_typed_blocks_and_examples", status == 200 and len(ids) >= 50 and len(set(ids)) == len(ids)
                     and catalog["catalog"]["blocks"] == catalog["blocks"] and len(catalog["examples"]) >= 3,
                     observed_blocks=len(ids), observed_strategy_examples=len(catalog["examples"]))

        doc = json.loads((ROOT / "examples/reversal5_custom.json").read_text(encoding="utf-8"))
        doc["name"] = "API验收：自定义5日反转（10只月调）"
        doc["selection"].update({"top_n": 10, "rebalance": "monthly"})
        status, validation = self.json("/api/strategy/validate", {"strategy": doc})
        self.require("macro_document_normalized_and_expanded_with_causal_metadata", status == 200 and validation.get("valid") is True
                     and validation["document"] == validation["strategy"] and validation["meta"]["causal"] is True
                     and validation["expanded"]["score"]["op"] == "negate"
                     and validation["expanded"]["score"]["args"][0]["params"]["period"] == 5)
        normalized = validation["strategy"]
        wire = json.loads(json.dumps(normalized, ensure_ascii=False, allow_nan=False))
        status, again = self.json("/api/strategy/validate", {"strategy": wire})
        self.check("strategy_json_export_import_roundtrip_is_lossless", status == 200 and again["strategy"] == normalized and again["expanded"] == validation["expanded"])
        status, saved = self.json("/api/strategies", {"strategy": normalized})
        self.require("strategy_saved_with_server_generated_identifier", status == 201 and saved["strategy"] == normalized and saved["id"].startswith("strategy_"))
        self.saved_strategy = saved["id"]
        status, loaded = self.json("/api/strategies/" + saved["id"])
        _, listing = self.json("/api/strategies")
        self.check("saved_strategy_get_and_list_preserve_dynamic_name_and_document", status == 200 and loaded["strategy"] == normalized
                   and any(r["id"] == saved["id"] and r["name"] == doc["name"] and r["strategy"] == normalized for r in listing["strategies"]))

        pack = normalized["packs"][0]
        status, imported = self.json("/api/block-packs", {"pack": pack})
        self.require("block_pack_json_import_adds_executable_catalog_entry", status == 201 and imported["pack"] == pack
                     and any(b["id"] == "custom.short_reversal5" and b["default"]["op"] == "custom.short_reversal5" for b in imported["blocks"]))
        status, replaced = self.json("/api/block-packs", {"pack": json.loads(json.dumps(imported["pack"]))})
        _, new_catalog = self.json("/api/blocks")
        self.check("block_pack_roundtrip_reimport_replaces_same_id_without_duplicate", status == 201 and replaced["id"] == imported["id"]
                   and replaced["pack"] == pack and sum(b["id"] == "custom.short_reversal5" for b in new_catalog["blocks"]) == 1
                   and sum(p["id"] == pack["id"] for p in new_catalog["packs"]) == 1)

        bad = []
        value = copy.deepcopy(doc); value["score"] = node("future_close"); bad.append(("unknown_operation_rejected", value))
        value = copy.deepcopy(doc); value["score"] = node("lag", node("close"), period=-1); bad.append(("negative_lag_rejected", value))
        value = copy.deepcopy(doc); value["score"]["python"] = "__import__('os')"; bad.append(("executable_ast_key_rejected", value))
        value = copy.deepcopy(doc); value["score"] = node("add", node("is_finite", node("close")), node("close")); bad.append(("boolean_numeric_type_mismatch_rejected", value))
        value = copy.deepcopy(doc); value["filter"] = node("close"); bad.append(("numeric_filter_type_rejected", value))
        value = copy.deepcopy(doc); score = node("close")
        for _ in range(20): score = node("negate", score)
        value["score"] = score; bad.append(("excessive_expression_depth_rejected", value))
        value = copy.deepcopy(doc); value["packs"][0]["blocks"][0]["template"] = node("custom.short_reversal5", node("input", name="x")); bad.append(("recursive_imported_macro_rejected", value))
        value = copy.deepcopy(doc); value["score"] = node("constant", value=float("nan")); bad.append(("nonfinite_json_number_rejected", value))
        value = copy.deepcopy(doc); value["selection"]["top_n"] = True; bad.append(("boolean_selection_count_rejected", value))
        for name, value in bad:
            status, response = self.json("/api/strategy/validate", {"strategy": value})
            self.check(name, status == 400 and bool(response.get("error")))
        cycle_pack = {"format": "daily-factor-block-pack", "version": 1, "id": "qa-cycle", "name": "QA cycle", "blocks": [
            {"id": "custom.qa_a", "name": "A", "inputs": [], "template": node("custom.qa_b")},
            {"id": "custom.qa_b", "name": "B", "inputs": [], "template": node("custom.qa_a")}]}
        status, _ = self.json("/api/block-packs", {"pack": cycle_pack})
        self.check("mutually_recursive_pack_import_rejected", status == 400)
        malicious_pack = copy.deepcopy(pack); malicious_pack["blocks"][0]["id"] = "close"
        status, _ = self.json("/api/block-packs", {"pack": malicious_pack})
        self.check("imported_pack_cannot_replace_builtin_operation", status == 400)
        origin_statuses = [self.json(path, payload, origin="https://untrusted.example")[0] for path, payload in [
            ("/api/strategies", {"strategy": normalized}), ("/api/block-packs", {"pack": pack}), ("/api/run", {"strategy": normalized})]]
        self.check("cross_origin_mutating_routes_rejected_before_side_effects", origin_statuses == [403, 403, 403])
        status, _, _ = self.request("/api/strategy/validate", {"strategy": normalized}, content_type="text/plain")
        self.check("non_json_import_request_rejected", status == 415)
        status, _, _ = self.request("/api/strategy/validate", raw=b'{"padding":"' + b'x' * 270000 + b'"}')
        self.check("oversize_request_body_rejected", status == 400)
        traversal = [self.json("/api/strategies/..%2Fconfig")[0], self.json("/api/runs/activate", {"run_id": "../config"})[0], self.request("/api/download?path=../config.json")[0]]
        self.check("strategy_run_and_download_directory_escape_rejected", traversal == [400, 400, 404])

        cfg = {**before["config"], "start": "2021-01-01", "end": "2021-03-31", "top_n": 31, "rebalance": "weekly"}
        status, accepted = self.json("/api/run", {"config": cfg, "strategy": normalized}, origin=self.base)
        self.require("real_macro_run_accepted_with_selection_overriding_config", status == 202 and accepted["config"]["top_n"] == 10 and accepted["config"]["rebalance"] == "monthly")
        self.job_started = True
        status, _ = self.json("/api/runs/activate", {"run_id": self.restore_run})
        self.check("historical_activation_shares_busy_lock_with_backtest", status == 409)
        state = self.idle()
        self.require("real_macro_run_and_dashboard_build_finish_without_error", state["error"] is None)
        status, result = self.json("/api/result")
        self.qa_run = result["run_id"]
        self.require("real_custom_result_has_new_identity_three_scenarios_one_ic", status == 200 and self.qa_run != self.restore_run
                     and result["status"] == "complete" and not result["failures"] and len(result["summary"]) == 3 and len(result["ic"]) == 1
                     and {r["scenario"] for r in result["summary"]} == {"base", "gross", "stress"}
                     and {r["factor"] for r in result["summary"]} == {"custom"})
        self.check("custom_strategy_name_is_dynamic_in_result_and_ic", result["custom_strategy"]["name"] == doc["name"]
                   and all(r["name"] == doc["name"] for r in result["summary"] + result["ic"]) and result["factor_definitions"]["custom"]["name"] == doc["name"])
        self.check("short_interval_and_document_selection_applied_to_real_result", result["config"]["start"] == cfg["start"] and result["config"]["end"] == cfg["end"]
                   and result["config"]["top_n"] == 10 and result["config"]["rebalance"] == "monthly"
                   and all(cfg["start"] <= r["date"] <= cfg["end"] for r in result["curves"]))
        status, strategy = self.json("/api/download?path=" + quote(result["files"]["strategy"], safe=""))
        manifest_status, manifest = self.json("/api/download?path=" + quote(result["files"]["strategy_manifest"], safe=""))
        self.check("strategy_document_and_causal_manifest_download_roundtrip", status == manifest_status == 200 and strategy == normalized
                   and manifest["causal"] is True and manifest["selection"]["top_n"] == 10 and manifest["selection"]["rebalance"] == "monthly"
                   and set(manifest["operations"]) == {"close", "pct_change", "negate"})
        status, trade_bytes, _ = self.request("/api/download?path=" + quote(result["strategies"]["custom"]["trades"], safe=""))
        trades = list(csv.DictReader(io.StringIO(trade_bytes.decode("utf-8-sig"))))
        executed = [row for row in trades if row["side"] in {"BUY", "SELL"}]
        self.check("actual_custom_trade_ledger_preserves_strict_signal_lag", status == 200 and bool(executed)
                   and all(datetime.fromisoformat(r["signal_date"]) < datetime.fromisoformat(r["date"]) for r in executed), observed_executed_trades=len(executed))
        self.snapshot(result, "source_and_served_compiled_snapshot_match_custom_run_and_name")
        status, history = self.json("/api/runs")
        self.check("history_lists_custom_qa_and_original_main_names", status == 200 and any(r["run_id"] == self.qa_run and r["name"] == doc["name"] for r in history["runs"])
                   and any(r["run_id"] == self.restore_run for r in history["runs"]))

    def restore(self):
        state = self.idle()
        status, accepted = self.json("/api/runs/activate", {"run_id": self.restore_run}, origin=self.base)
        self.require("restore_main_via_activation_api_accepted", status == 202 and accepted["run_id"] == self.restore_run)
        status, _ = self.json("/api/runs/activate", {"run_id": self.restore_run})
        self.check("activation_rebuild_uses_same_busy_lock", status == 409)
        state = self.idle()
        status, restored = self.json("/api/result")
        self.restored = status == 200 and state["error"] is None and not state["busy"] and state["latest_run"] == self.restore_run == restored["run_id"]
        self.require("preferred_main_result_restored_without_direct_pointer_edit", self.restored)
        self.snapshot(restored, "restored_source_and_compiled_snapshot_match_preferred_main")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--base", default="http://127.0.0.1:8776")
    p.add_argument("--restore-run", default="run_20261016_201056_106039")
    args = p.parse_args()
    v = Verification(args.base, args.restore_run)
    error = None
    try:
        v.run()
    except Exception:
        error = traceback.format_exc()
        print(error, flush=True)
    finally:
        try:
            if v.job_started:
                v.restore()
        except Exception:
            error = (error or "") + "\nRESTORATION FAILURE\n" + traceback.format_exc()
        passed = sum(c["passed"] for c in v.checks)
        report = {"checks": v.checks, "passed": passed, "total": len(v.checks), "all_passed": passed == len(v.checks) and error is None and v.restored,
                  "before_run": args.restore_run, "qa_run": v.qa_run, "restored_run": args.restore_run if v.restored else None,
                  "restored_via_api": v.restored, "saved_strategy_id": v.saved_strategy, "error": error,
                  "scope": "Real loopback V2 API and one real imported-macro historical run; no broker or real orders"}
        (ROOT / "api_validation_v2.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({k: report[k] for k in ["passed", "total", "all_passed", "qa_run", "restored_run", "error"]}, ensure_ascii=False, indent=2), flush=True)
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
