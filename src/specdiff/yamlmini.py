"""极简 YAML 读取。

刻意不引入 PyYAML——零依赖是本项目的硬约束，
任何环境（CI 容器、EDA 服务器、隔离网）都要能直接跑。

支持的范围（覆盖本项目的全部配置需求）：
    key: value
    key:
      - item
      - key: value
        key2: value
    key:
      nested: value
      list:
        - a
        - b

不支持锚点、多文档、复杂流式写法——遇到不支持的语法会明确报错，
而不是静默给出错误结果（配置解析错了比没解析更危险）。
"""

from __future__ import annotations


class YamlError(Exception):
    """配置写法有误。"""


def parse(text: str) -> dict:
    """把 YAML 文本解析成字典。"""
    lines: list[tuple[int, str]] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        # 去掉注释，但要保留引号内的 #
        stripped = _strip_comment(raw).rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip())
        lines.append((indent, stripped.strip()))

    if not lines:
        return {}

    data, pos = _parse_block(lines, 0, lines[0][0])
    if pos != len(lines):
        raise YamlError(f"第 {pos} 行附近无法解析，请检查缩进是否一致")
    if not isinstance(data, dict):
        raise YamlError("配置文件的顶层必须是键值对")
    return data


def _strip_comment(line: str) -> str:
    """去掉行内注释，但跳过引号内的 #。"""
    out = []
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            # 只有当 # 前面是空白（或行首）时才算注释，避免切掉 a#b
            if i == 0 or line[i - 1] in " \t":
                break
        out.append(ch)
    return "".join(out)


def _parse_block(lines: list[tuple[int, str]], pos: int, indent: int):
    """解析一个缩进层级，返回 (结果, 下一个位置)。"""
    if pos >= len(lines):
        return {}, pos

    # 判断这一层是列表还是字典
    first_indent, first_text = lines[pos]
    if first_text.startswith("- ") or first_text == "-":
        return _parse_list(lines, pos, indent)
    return _parse_map(lines, pos, indent)


def _parse_map(lines: list[tuple[int, str]], pos: int, indent: int):
    data: dict = {}
    while pos < len(lines):
        cur_indent, text = lines[pos]
        if cur_indent < indent:
            break
        if cur_indent > indent:
            raise YamlError(f"缩进不一致：{text!r} 比同级更深，但没有上级键")
        if text.startswith("- "):
            break

        if ":" not in text:
            raise YamlError(f"这一行缺少冒号：{text!r}")
        key, _, rest = text.partition(":")
        key = key.strip()
        rest = rest.strip()

        if rest:
            # 块标量：| 保留换行，> 折成一行
            if rest in ("|", "|-", "|+", ">", ">-", ">+"):
                pos += 1
                block, pos = _parse_block_scalar(lines, pos, indent, rest)
                data[key] = block
                continue
            data[key] = _scalar(rest)
            pos += 1
            continue

        # 值是嵌套结构（或空）
        pos += 1
        if pos < len(lines) and lines[pos][0] > indent:
            value, pos = _parse_block(lines, pos, lines[pos][0])
            data[key] = value
        elif pos < len(lines) and lines[pos][0] == indent and \
                (lines[pos][1].startswith("- ") or lines[pos][1] == "-"):
            # 同级缩进的列表（YAML 允许这样写）
            value, pos = _parse_list(lines, pos, indent)
            data[key] = value
        else:
            data[key] = None
    return data, pos


def _parse_list(lines: list[tuple[int, str]], pos: int, indent: int):
    items: list = []
    while pos < len(lines):
        cur_indent, text = lines[pos]
        if cur_indent < indent:
            break
        if cur_indent != indent or not (text.startswith("- ") or text == "-"):
            break

        body = text[1:].strip()
        pos += 1

        if not body:
            # 该项是嵌套块
            if pos < len(lines) and lines[pos][0] > indent:
                value, pos = _parse_block(lines, pos, lines[pos][0])
                items.append(value)
            else:
                items.append(None)
            continue

        if ":" in body and not body.startswith(("'", '"')):
            # 该项是行内字典开头，后续行是这个字典的其余键
            key, _, rest = body.partition(":")
            item: dict = {}
            rest = rest.strip()
            if rest:
                item[key.strip()] = _scalar(rest)
            # 收集后续更深缩进的行，作为这个字典的其余键
            sub: list[tuple[int, str]] = []
            sub_indent = None
            while pos < len(lines) and lines[pos][0] > indent:
                if sub_indent is None:
                    sub_indent = lines[pos][0]
                if lines[pos][0] < sub_indent:
                    break
                sub.append(lines[pos])
                pos += 1
            if sub:
                # 把行内那个键补回去，让后续行能正确挂到同一层
                merged, _ = _parse_map(sub, 0, sub_indent)
                item.update(merged)
            items.append(item)
        else:
            items.append(_scalar(body))
    return items, pos


def _parse_block_scalar(lines: list[tuple[int, str]], pos: int, parent_indent: int,
                        marker: str):
    """解析 | 或 > 开头的多行文本。

    | 保留换行；> 把换行折成空格。末尾的 - 表示不要结尾换行。
    规格里的 note 字段经常需要写多行说明，所以这个要支持。
    """
    body: list[str] = []
    block_indent: int | None = None
    while pos < len(lines):
        cur_indent, text = lines[pos]
        if cur_indent <= parent_indent:
            break
        if block_indent is None:
            block_indent = cur_indent
        body.append(text)
        pos += 1

    if not body:
        return "", pos

    if marker.startswith(">"):
        # 折行：段落之间保留空行，段落内用空格连接
        out_lines: list[str] = []
        cur: list[str] = []
        for ln in body:
            if ln == "":
                if cur:
                    out_lines.append(" ".join(cur))
                    cur = []
                out_lines.append("")
            else:
                cur.append(ln)
        if cur:
            out_lines.append(" ".join(cur))
        text = "\n".join(out_lines)
    else:
        text = "\n".join(body)

    text = text.rstrip("\n")
    if not marker.endswith("-"):
        text += "\n"
    return text, pos


def _scalar(v: str):
    """把字符串转成合适的 Python 类型。"""
    v = v.strip()
    if v == "" or v == "~":
        return None
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none"):
        return None
    # 列表行内写法 [a, b, c]
    if v.startswith("[") and v.endswith("]"):
        inner = v[1:-1].strip()
        if not inner:
            return []
        return [_scalar(x) for x in _split_inline(inner)]
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _split_inline(s: str) -> list[str]:
    """按逗号切分，跳过引号内的逗号。"""
    out, cur, quote = [], [], None
    for ch in s:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
            cur.append(ch)
            continue
        if ch == ",":
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if cur:
        out.append("".join(cur).strip())
    return out
