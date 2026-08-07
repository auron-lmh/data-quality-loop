# -*- coding: utf-8 -*-
"""DeepSeek 封装:Maker 用强模型(pro),Checker 用弱模型(flash)降成本"""
from langchain_deepseek import ChatDeepSeek
from data_quality_loop.env_utils import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

deepseek_llm = ChatDeepSeek(
    api_key=DEEPSEEK_API_KEY,
    api_base=DEEPSEEK_BASE_URL,
    model="deepseek-v4-pro",
    extra_body={"thinking": {"type": "disabled"}},   # 关闭思考,省成本
)

deepseek_llm_flash = ChatDeepSeek(
    api_key=DEEPSEEK_API_KEY,
    api_base=DEEPSEEK_BASE_URL,
    model="deepseek-v4-flash",
    extra_body={"thinking": {"type": "disabled"}},
)
