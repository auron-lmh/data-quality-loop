# -*- coding: utf-8 -*-
"""DeepSeek 封装:Maker 用强模型(pro),Checker 用弱模型(flash)降成本"""
from langchain_deepseek import ChatDeepSeek
from data_quality_loop.env_utils import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

# 修复(CI): 裸环境(无 .env)时 env_utils 返回 None → ChatDeepSeek pydantic 校验失败。
# 给 api_base 官方默认值，key 空串(仅在运行时真实调用才需)。
_API_KEY = DEEPSEEK_API_KEY or ""
_API_BASE = DEEPSEEK_BASE_URL or "https://api.deepseek.com"

deepseek_llm = ChatDeepSeek(
    api_key=_API_KEY,
    api_base=_API_BASE,
    model="deepseek-v4-pro",
    extra_body={"thinking": {"type": "disabled"}},   # 关闭思考,省成本
)

deepseek_llm_flash = ChatDeepSeek(
    api_key=_API_KEY,
    api_base=_API_BASE,
    model="deepseek-v4-flash",
    extra_body={"thinking": {"type": "disabled"}},
)
