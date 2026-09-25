---
name: translate
description: Use when the user asks to translate text between Chinese and English, or mentions 翻译 / translate / 译成 / 中译英 / 英译中.
---

# 中英翻译

把用户给出的文本翻译成目标语言，遵守以下规则：

1. 保持原意，不增删信息，不擅自解释。
2. 保持原文的语气与风格（正式/口语/俏皮）。
3. 输出格式固定为两行：
   - 原文：……
   - 译文：……
4. 若用户没有说明目标语言，默认中文→英文。
5. 除上述两行外，不要输出任何多余内容。
