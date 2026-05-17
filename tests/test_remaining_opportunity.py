import unittest

from scripts.trading.remaining_opportunity import compute_remaining_opportunity_fields


class RemainingOpportunityTests(unittest.TestCase):
    def test_top_ninth_home_lead_marks_shortened_bottom_ninth_risk(self) -> None:
        fields = compute_remaining_opportunity_fields(
            away_score=3,
            home_score=5,
            inning=9,
            inning_state="Top",
        )

        self.assertTrue(fields["home_leading_late"])
        self.assertFalse(fields["batting_team_is_home"])
        self.assertTrue(fields["bottom9_available_if_needed"])
        self.assertEqual(fields["expected_remaining_half_innings"], 1.0)
        self.assertEqual(fields["expected_remaining_pa_bucket"], "<=5")
        self.assertEqual(fields["home_skip_bottom9_risk"], 1.0)

    def test_top_ninth_home_not_leading_keeps_bottom_ninth_opportunity(self) -> None:
        fields = compute_remaining_opportunity_fields(
            away_score=5,
            home_score=3,
            inning=9,
            inning_state="Top",
        )

        self.assertFalse(fields["home_leading_late"])
        self.assertEqual(fields["expected_remaining_half_innings"], 2.0)
        self.assertEqual(fields["expected_remaining_pa_bucket"], "6-10")
        self.assertEqual(fields["home_skip_bottom9_risk"], 0.0)

    def test_top_eighth_home_lead_reduces_regulation_half_innings(self) -> None:
        fields = compute_remaining_opportunity_fields(
            away_score=2,
            home_score=4,
            inning=8,
            inning_state="Top",
        )

        self.assertTrue(fields["home_leading_late"])
        self.assertEqual(fields["expected_remaining_half_innings"], 3.0)
        self.assertEqual(fields["expected_remaining_pa_bucket"], "11-18")
        self.assertEqual(fields["home_skip_bottom9_risk"], 1.0)


if __name__ == "__main__":
    unittest.main()
