# -*- coding: utf-8 -*-
"""环境变量加载:从项目根 .env 读取(override=True 确保 .env 优先)"""
import os
from dotenv import load_dotenv

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv(os.path.join(ROOT, ".env"), override=True)

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL")
API_TOKEN = os.getenv("API_TOKEN", "")   # FastAPI Bearer 鉴权(留空则开发模式放行)
