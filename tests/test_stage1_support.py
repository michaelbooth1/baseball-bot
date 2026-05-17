import unittest

from scripts.trading.stage1_support import (
    stage1_support_diagnostics,
    stage1_support_diagnostics_from_values,
)


class Stage1SupportDiagnosticsTests(unittest.TestCase):
    def test_exact_lookup_uses_effective_n_and_full_trust_penalties(self):
        diag = stage1_support_diagnostics(
            cell={"n": 500, "weighted_n": 300, "effective_n": 120, "n_samples": 240},
            state_fallback_level=0,
            poisson_line_fallback_mode="exact",
            empirical_line_fallback_mode="exact",
        )

        self.assertEqual(diag["effective_n_proxy"], 120)
        self.assertGreater(diag["stage1_trust_weight"], 0.75)
        self.assertEqual(diag["stage1_support_bucket"], "100-250")
        self.assertTrue(diag["exact_cell_support"])
        self.assertTrue(diag["poisson_line_exact"])
        self.assertTrue(diag["empirical_line_exact"])
        self.assertEqual(diag["empirical_sample_support"], 240)

    def test_fallback_lookup_penalizes_proxy_support(self):
        diag = stage1_support_diagnostics_from_values(
            support_mass=100,
            empirical_sample_support=30,
            state_fallback_level=3,
            poisson_line_fallback_mode="interpolate",
            empirical_line_fallback_mode="clamp_high",
        )

        self.assertAlmostEqual(diag["effective_n_proxy"], 30.25)
        self.assertLess(diag["stage1_trust_weight"], 0.35)
        self.assertEqual(diag["stage1_support_bucket"], "20-50")
        self.assertFalse(diag["exact_cell_support"])
        self.assertFalse(diag["empirical_line_exact"])
        self.assertEqual(diag["empirical_sample_bucket"], "20-50")


if __name__ == "__main__":
    unittest.main()
