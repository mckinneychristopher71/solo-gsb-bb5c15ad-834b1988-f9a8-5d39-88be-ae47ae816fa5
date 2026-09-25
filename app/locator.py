"""在拼接文本中定位一段引文（quote）。

复用 :func:`app.assembler.assemble` 的拼接文本与逐字来源（沿用其段数长度限制
与续接门槛，失败时原样抛出 :class:`~app.assembler.AssemblyError`），把 quote
与拼接文本的**任意非空连续区间**做半全局编辑距离比较：

- quote 必须全部消耗；拼接文本在区间之前的前缀、之后的后缀不参与比较；
- 相同字代价 0；任一侧为 ``?``（未辨认字）代价 0；
- 替换、插入、删除各代价 1（标准 Levenshtein 代价）。

仅接受**最小代价不超过 2** 的区间：

- 最小代价唯一对应一个区间：返回该左闭右开区间、原文片段、代价与逐字 sources；
- 多个不同区间同为最小代价：抛出 :class:`QuoteTieError`（携带全部并列区间）；
- 最小代价超过 2（无合格区间）：抛出 :class:`QuoteNotFoundError`。

本模块只做定位查询，绝不改写拼接文本。
"""

from __future__ import annotations

from .aligner import WILDCARD
from .assembler import assemble

MIN_QUOTE_LEN = 1
MAX_QUOTE_LEN = 80
MAX_COST = 2  # 仅接受最小代价不超过 2 的区间


class QuoteNotFoundError(ValueError):
    """拼接文本中不存在代价不超过上限的合格区间。"""

    def __init__(self, min_cost: int):
        self.min_cost = min_cost
        super().__init__(
            f"拼接文本中没有任何非空连续区间与引文匹配（最小代价 {min_cost}，"
            f"超过 {MAX_COST} 的上限）"
        )


class QuoteTieError(ValueError):
    """最小代价由多个不同区间并列取得。"""

    def __init__(self, cost: int, tied: list[dict]):
        self.cost = cost
        self.tied = tied
        super().__init__(
            f"最小代价 {cost} 的区间有 {len(tied)} 个并列，无法唯一确定引文位置"
        )


def _sub_cost(a: str, b: str) -> int:
    """一对字符的替换/匹配代价：相同字或任一侧为 ``?`` 代价 0，否则 1。"""
    if a == WILDCARD or b == WILDCARD:
        return 0
    return 0 if a == b else 1


def _minimum_interval_cost(quote: str, text: str) -> int:
    """quote 与 text 任意非空连续区间之间的最小编辑代价（不设上限）。

    半全局编辑距离 DP：quote（竖边，i）必须全部消耗；text（横边，j）允许
    不计代价的自由前缀（区间可从任意位置起步）与自由后缀（在任意列收尾）。
    """
    m = len(quote)
    # 首行（quote 已消耗 0 字）：区间尚未起步，代价恒为 0
    prev = [0] * (len(text) + 1)
    for i in range(1, m + 1):
        cur = [i]  # 区间从 text 首字起步、quote 前 i 字全部删除
        for j in range(1, len(text) + 1):
            cur.append(min(
                prev[j] + 1,                              # 删除 quote[i-1]
                cur[j - 1] + 1,                           # 插入 text[j-1]
                prev[j - 1] + _sub_cost(quote[i - 1], text[j - 1]),
            ))
        prev = cur
    # 区间非空：quote 长度在 1～80，收尾时必已消耗至少一个 text 字符，
    # prev[0]（纯删除、对应空区间）不可能取得最小代价之外的语义，
    # 但显式排除空区间以符合“非空连续区间”的定义。
    return min(prev[1:])


def _enumerate_intervals_within_cost(
    quote: str, text: str, cap: int
) -> list[tuple[int, int, int]]:
    """枚举代价不超过 ``cap`` 的全部 (起点, 终点, 代价)（区间左闭右开、非空）。

    半全局编辑距离 DP，每个单元格按代价（0～cap）分桶记录可取得该代价的
    全部区间起点：区间从 text[j] 起步（自由前缀不耗 quote）时起点为 j。
    同一单元格内每个起点只保留其最小代价。
    """
    m, n = len(quote), len(text)
    # prev[j][c]：已消耗 quote 前 i 字、text 前 j 字、代价恰为 c 的全部起点
    prev = [[set() for _ in range(cap + 1)] for _ in range(n + 1)]
    for j in range(n + 1):
        prev[j][0].add(j)  # 区间尚未起步：仅代价 0、起点记为 j

    found: list[tuple[int, int, int]] = []
    for i in range(1, m + 1):
        cur = [[set() for _ in range(cap + 1)] for _ in range(n + 1)]
        # 区间从 text 首字起步：quote 前 i 字全部删除（i > cap 时无可行状态）
        if i <= cap:
            cur[0][i].add(0)
        for j in range(1, n + 1):
            cell = cur[j]

            def add(cost: int, starts) -> None:
                if cost <= cap:
                    cell[cost].update(starts)

            # 删除 quote[i-1]
            for c in range(cap):
                add(c + 1, prev[j][c])
            # 插入 text[j-1]
            for c in range(cap):
                add(c + 1, cur[j - 1][c])
            # 匹配 / 替换（任一侧为 ? 或相同字代价 0，否则 1）
            s = _sub_cost(quote[i - 1], text[j - 1])
            for c in range(cap - s + 1):
                add(c + s, prev[j - 1][c])

            # 区间从此处起步（text 前 j 字为不计代价的自由前缀）
            if i == 1:
                add(s, {j})

            # 每个起点只保留其最小代价（其余为不可扩展为更优对齐的冗余状态）
            seen: set[int] = set()
            for c in range(cap + 1):
                cell[c] = {st for st in cell[c] if st not in seen}
                seen.update(cell[c])

        if i == m:
            for j in range(1, n + 1):  # j >= 1：排除空区间
                for c in range(cap + 1):
                    for start in cur[j][c]:
                        if start < j:
                            found.append((start, j, c))
        prev = cur
    return found


def locate_quote(segments: list[str], quote: str) -> dict:
    """在 2～5 段转写的拼接文本中定位 quote，返回命中信息。

    - 唯一命中：返回 ``{"text", "start", "end", "fragment", "cost", "sources"}``；
    - 并列：抛出 :class:`QuoteTieError`（``tied`` 按 (起点, 终点) 升序）；
    - 无合格区间：抛出 :class:`QuoteNotFoundError`。
    """
    if not (MIN_QUOTE_LEN <= len(quote) <= MAX_QUOTE_LEN):
        raise ValueError(
            f"引文长度需为 {MIN_QUOTE_LEN}～{MAX_QUOTE_LEN} 字"
        )

    result = assemble(segments)
    text, sources = result["text"], result["sources"]

    min_cost = _minimum_interval_cost(quote, text)
    if min_cost > MAX_COST:
        raise QuoteNotFoundError(min_cost)

    candidates = [
        (start, end, cost)
        for start, end, cost in _enumerate_intervals_within_cost(
            quote, text, min_cost
        )
        if cost == min_cost
    ]
    candidates.sort(key=lambda x: (x[0], x[1]))

    if len(candidates) > 1:
        tied = [
            {"start": start, "end": end, "text": text[start:end], "cost": cost}
            for start, end, cost in candidates
        ]
        raise QuoteTieError(min_cost, tied)

    start, end, cost = candidates[0]
    return {
        "text": text,
        "start": start,
        "end": end,
        "fragment": text[start:end],
        "cost": cost,
        "sources": [list(srcs) for srcs in sources[start:end]],
    }
