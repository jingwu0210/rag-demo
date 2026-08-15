"""Evaluation — 测试集加载

load_test_set(path=None)：
- path 默认取 config eval.test_set_path
- 文件存在 → 加载 JSON（[{question, ground_truth, relevant_chunks, language, is_out_of_scope}]）
- 文件不存在 → 返回内置 5 条 sample 测试集（含 1 条 out_of_scope）并记 warning 日志
"""
from __future__ import annotations

import json
from typing import List

from core.config import ConfigRegistry
from core.logging_config import get_logger

logger = get_logger(module="eval.test_set")

# 内置 sample 测试集（5 条，含 1 条 out_of_scope）— 公司场景问题
SAMPLE_TEST_SET: List[dict] = [
    {"question": "员工年假天数是多少？",
     "ground_truth": "根据员工手册，员工每年享有年假，具体天数与职级和司龄相关。",
     "relevant_chunks": [], "language": "zh", "is_out_of_scope": False},
    {"question": "年假申请需要什么条件？",
     "ground_truth": "申请年假需满足入职满一年等条件。",
     "relevant_chunks": [], "language": "zh", "is_out_of_scope": False},
    {"question": "What is the API rate limit policy?",
     "ground_truth": "The technical specification defines the API rate limit.",
     "relevant_chunks": [], "language": "en", "is_out_of_scope": False},
    {"question": "合规审计的频率要求是什么？",
     "ground_truth": "根据合规指南，审计需按年度执行。",
     "relevant_chunks": [], "language": "zh", "is_out_of_scope": False},
    {"question": "今天天气怎么样？",
     "ground_truth": "", "relevant_chunks": [], "language": "zh", "is_out_of_scope": True},
]


def load_test_set(path: str = None) -> List[dict]:
    """加载测试集；文件不存在时回退内置 sample（记 warning 日志）。

    返回元素形如 {question, ground_truth, relevant_chunks, language, is_out_of_scope}。
    """
    test_set_path = path or ConfigRegistry.get("eval.test_set_path", "assets/testsets/test_set.json")
    try:
        with open(test_set_path, "r", encoding="utf-8") as f:
            test_set = json.load(f)
        logger.info("test_set_loaded", path=test_set_path, count=len(test_set))
        return test_set
    except FileNotFoundError:
        logger.warning("test_set_not_found_use_sample",
                       path=test_set_path, sample_count=len(SAMPLE_TEST_SET))
        return [dict(item) for item in SAMPLE_TEST_SET]
