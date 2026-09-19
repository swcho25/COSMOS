"""Diagnostics must preserve unclassified articles and actual/predicted axes."""
import sys
import unittest
from pathlib import Path
import numpy as np

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analyze_news_failures import confusion, topic_masks


class FailureAnalysisTests(unittest.TestCase):
    def test_overlapping_topics_do_not_drop_other_articles(self):
        masks=topic_masks(["공급 계약으로 매출 확대", "Annual EARNINGS report", "지역 행사 소식", ""])
        self.assertTrue(masks["contracts"][0] and masks["earnings"][0])
        np.testing.assert_array_equal(masks["other"],[False,False,True,True])
        np.testing.assert_array_equal(masks["all"],[True]*4)

    def test_confusion_axes_actual_then_predicted_and_absent_class_kept(self):
        matrix=confusion(np.array([0,0,1,1]),np.array([0,2,0,0]))
        np.testing.assert_array_equal(matrix,[[1,0,1],[2,0,0],[0,0,0]])
        self.assertEqual(matrix.sum(),4)


if __name__=="__main__":unittest.main()
