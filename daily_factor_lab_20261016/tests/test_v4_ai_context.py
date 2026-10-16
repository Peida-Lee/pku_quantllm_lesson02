"""Verify V4 advisory context, never call a real provider."""
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from ai_strategy import confirm_strategy
from research_v4 import grid,strategy_document,config_for
from strategy_blocks import StrategyError


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.c=next(c for c in grid() if c["factor_id"]=="reversal_w005")
        self.doc=strategy_document(self.c)
        self.env=patch.dict(os.environ,{"FACTOR_AI_BASE_URL":"","FACTOR_AI_MODEL":"","FACTOR_AI_API_KEY":""})
        self.env.start();self.addCleanup(self.env.stop)

    def test_execution_constraints_and_only_used_formula_survive(self):
        cfg={**config_for(self.c,"evaluation_2025").__dict__,"api_key":"PRIVATE_SENTINEL","market_matrix":[[1,2]]}
        with patch("urllib.request.build_opener",side_effect=AssertionError("No network")):
            result=confirm_strategy(self.doc,config=cfg)
        context=result["ai_context"]
        for key in ("execution_model","st_policy","universe","liquidity_top_n","risk_proxy_window","max_daily_participation","liquidate_end","wind_down_days"):
            self.assertEqual(context["execution_config"][key],cfg[key])
        self.assertEqual(len(context["factor_definitions"]),1)
        self.assertEqual(context["factor_definitions"][0]["id"],"reversal_w005")
        self.assertIn("C[t]/C[t-w]-1",context["factor_definitions"][0]["formula"])
        self.assertNotIn("PRIVATE_SENTINEL",json.dumps(context))
        self.assertNotIn("market_matrix",context["execution_config"])
        self.assertFalse(result["used_ai"])
        self.assertFalse(context["source_limits"]["historical_ST_verified"])

    def test_selection_inherits_config_and_explicit_override_is_preserved(self):
        self.doc.pop("selection")
        result=confirm_strategy(self.doc,config={"top_n":37,"rebalance":"monthly","wind_down_days":20})
        self.assertEqual(result["ai_context"]["effective_selection"]["top_n"],37)
        self.doc["selection"]={"top_n":20,"rebalance":"weekly"}
        result=confirm_strategy(self.doc,config={"top_n":37,"rebalance":"monthly"})
        self.assertEqual(result["ai_context"]["effective_selection"]["top_n"],20)
        self.assertIn("目标入选",result["local_confirmation"]["summary"])

    def test_unknown_library_operation_rejected_before_io(self):
        self.doc["score"]["op"]="v4_missing"
        with patch("urllib.request.build_opener",side_effect=AssertionError("No network")):
            with self.assertRaises(StrategyError):confirm_strategy(self.doc)


if __name__=="__main__":unittest.main()
