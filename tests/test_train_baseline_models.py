import unittest

from scripts.analysis import train_baseline_models as tbm


class TrainBaselineModelsTests(unittest.TestCase):
    def test_fit_preprocessor_numeric_and_categorical(self) -> None:
        rows = [
            {"a": 1.0, "b": "top", "c": True},
            {"a": 2.0, "b": "bottom", "c": False},
            {"a": 3.0, "b": "top", "c": True},
            {"a": 4.0, "b": "bottom", "c": False},
        ]
        prep = tbm.fit_preprocessor(rows, feature_cols=["a", "b", "c"])
        self.assertIn("a", prep.numeric_cols)
        self.assertIn("c", prep.numeric_cols)
        self.assertIn("b", prep.categorical_cols)
        self.assertIn("cat::b==bottom", prep.feature_names)
        self.assertIn("cat::b==top", prep.feature_names)

        X = tbm.transform_rows(rows, prep)
        self.assertEqual(len(X), 4)
        self.assertEqual(len(X[0]), len(prep.feature_names))

    def test_logistic_regression_simple_signal(self) -> None:
        X = [[0.0], [0.5], [1.0], [2.0], [3.0], [3.5]]
        y = [0, 0, 0, 1, 1, 1]
        model = tbm.fit_logistic_regression(X, y, l2=0.1, lr=0.1, max_iter=1000)
        p = tbm.predict_probabilities(model, X)
        self.assertLess(p[0], 0.5)
        self.assertGreater(p[-1], 0.5)
        self.assertIsNotNone(tbm._auc_pairwise(y, p))
        self.assertGreaterEqual(tbm._auc_pairwise(y, p) or 0.0, 0.9)

    def test_metric_summary(self) -> None:
        y = [0, 1, 1, 0]
        p = [0.2, 0.8, 0.9, 0.1]
        m = tbm.metric_summary(y, p)
        self.assertEqual(m["rows"], 4)
        self.assertGreater((m["accuracy_0p5"] or 0.0), 0.9)
        self.assertGreater((m["auc"] or 0.0), 0.9)
        self.assertLess((m["logloss"] or 1.0), 0.5)


if __name__ == "__main__":
    unittest.main()
