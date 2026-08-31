# -*- coding: utf-8 -*-
"""RPA 通用层 — 紫鸟客户端、浏览器会话、Excel IO、断点续写。

全项目唯一的紫鸟底层实现，业务 task 与交互式原子工具都从这里复用，
不再各自 copy 一份 send_http / open_store。
"""
