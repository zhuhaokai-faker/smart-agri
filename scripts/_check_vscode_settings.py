# -*- coding: utf-8 -*-
"""
校验 .vscode/settings.json 是不是合法 JSON（含 JSONC 注释）。

【为什么需要这个小工具】
VS Code 的 settings.json 解析失败时，报的错**不是**"JSON 语法错误"，而是：

    命令 "Python: 选择解释器" 导致错误
    无法写入文件夹设置。请打开 "..." 文件夹设置并清除错误或警告，然后重试。

这个提示把"文件解析失败"说成了"写入失败"，方向完全指错 ——
会让人去查文件权限，而真正的问题是**反斜杠写成了非法转义**。

JSON 里合法的转义只有： \" \\ / b f n r t uXXXX
写成 "C:\\path" 是错的（应该是 "C:\\\\path"），
或者干脆用正斜杠 "C:/path" —— Windows 上一样有效且不用转义。

用这个脚本可以在提交前快速自检。
"""

import json
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SETTINGS = BASE_DIR / '.vscode' / 'settings.json'

# JSON 标准允许的转义字符
VALID_ESCAPES = set('"\\/bfnrtu')


def strip_jsonc_comments(text: str) -> str:
    """去掉 // 行注释。只处理行注释 —— settings.json 里基本用不到块注释。"""
    return re.sub(r'^[ \t]*//.*$', '', text, flags=re.M)


def find_bad_escapes(obj, path=''):
    """递归找出字符串值里的非法转义序列。"""
    bad = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            bad += find_bad_escapes(v, f'{path}.{k}' if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            bad += find_bad_escapes(v, f'{path}[{i}]')
    elif isinstance(obj, str):
        for m in re.finditer(r'\\(.)', obj):
            if m.group(1) not in VALID_ESCAPES:
                bad.append((path, obj))
    return bad


def main():
    if not SETTINGS.exists():
        print(f'  [SKIP] {SETTINGS} 不存在')
        return 0

    raw = SETTINGS.read_text(encoding='utf-8')

    # ---- 1. 严格 JSON 校验（去注释后）----
    try:
        data = json.loads(strip_jsonc_comments(raw))
    except json.JSONDecodeError as e:
        print(f'  [FAIL] 不是合法 JSON：{e}')
        print(f'         出错位置附近：{raw[max(0, e.pos - 60):e.pos + 60]!r}')
        return 1
    print('  [OK] 去掉注释后是合法 JSON')

    # ---- 2. 非法转义检查 ----
    # 注意：json.loads 通过之后，其实已经不存在非法转义了
    # （因为 json 模块自己就会对 \. 报错）。这一步是给"想直接看哪个值有问题"
    # 的场景留的，顺便把规则显式写出来。
    bad = find_bad_escapes(data)
    if bad:
        print('  [FAIL] 存在非法转义：')
        for path, val in bad:
            print(f'         {path} = {val}')
        return 1
    print('  [OK] 字符串值里没有非法转义')

    # ---- 3. 逐项打印，人眼过一遍 ----
    print('\n  解析出的配置：')
    for k, v in data.items():
        print(f'    {k} = {v}')

    # ---- 4. 解释器路径是否真实存在 ----
    interp = data.get('python.defaultInterpreterPath')
    if interp:
        real = interp.replace('${workspaceFolder}', str(BASE_DIR))
        real = real.replace('/', chr(92))          # 正斜杠转 Windows 分隔符
        exists = Path(real).exists()
        print(f'\n  解释器路径：{real}')
        print('  [OK] 该文件真实存在' if exists else '  [FAIL] 该文件不存在')
        if not exists:
            return 1
    return 0


if __name__ == '__main__':
    print('=' * 60)
    print('  校验 .vscode/settings.json')
    print('=' * 60)
    sys.exit(main())
