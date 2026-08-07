# -*- coding: utf-8 -*-
"""pytest 配置:把 src 加入 sys.path"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
