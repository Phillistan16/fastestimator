import unittest

import numpy as np

from fastestimator.test.unittest_util import is_equal
from fastestimator.trace.metric import F1Score
from fastestimator.util import Data


class TestF1Score(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        x = np.array([1, 2, 3])
        x_pred = np.array([[1, 1, 3], [2, 3, 4], [1, 1, 0]])
        cls.data = Data({'x': x, 'x_pred': x_pred})
        cls.f1score = F1Score(true_key='x', pred_key='x_pred')
        cls.f1score_output = np.array([0, 0, 0.67, 0])

    def test_on_epoch_begin(self):
        self.f1score.on_epoch_begin(data=self.data)
        with self.subTest('Check initial value of y_true'):
            self.assertEqual(self.f1score.y_true, [])
        with self.subTest('Check initial value of y_pred'):
            self.assertEqual(self.f1score.y_pred, [])

    def test_on_batch_end(self):
        self.f1score.y_true = []
        self.f1score.y_pred = []
        self.f1score.on_batch_end(data=self.data)
        with self.subTest('Check correct values'):
            self.assertEqual(self.f1score.y_true, [1, 2, 3])
        with self.subTest('Check total values'):
            self.assertEqual(self.f1score.y_pred, [2, 2, 0])

    def test_on_epoch_end(self):
        self.f1score.y_true = [1, 2, 3]
        self.f1score.y_pred = [2, 2, 0]
        self.f1score.on_epoch_end(data=self.data)
        with self.subTest('Check if f1_score exists'):
            self.assertIn('f1_score', self.data)
        with self.subTest('Check the value of f1 score'):
            self.assertTrue(is_equal(np.round(self.data['f1_score'], 2), self.f1score_output))