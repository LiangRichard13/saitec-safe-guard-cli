"""recorder — Layer 2

归一化记录收集器（内存队列 + JSONL 落盘）。
"""
from .recorder import Recorder

__all__ = ["Recorder"]