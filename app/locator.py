"""引文定位：在 ``/api/assemble`` 的拼接文本中查找引文的位置。

提交与 :func:`app.assembler.assemble` 相同的有序 segments 与一段引文 quote，
先复用其拼接得到拼接文本与逐字来源（沿用段数/长度限制与续接失败规则），再把
引文与拼接文本的**任意非空连续区间**比较，按逐字编辑代价取最优区间：

- 相同字代价 0；
- 任一侧为 ``?``（未辨认字）的字符对代价 0；
- 替换（双方均为已知且不同）、插入、删除各代价 1。

区间起点、终点均自由（区间前的拼接文本前缀、引文消耗完后的区间后缀不产生
删除/插入代价），但引文必须完全消耗、区间至少包含一个拼接字符。

- 最小代价不超过 :data:`MAX_COST`（2）且区间唯一：返回该左闭右开区间、
  原文片段、代价与该区间的逐字来源；
- 最小代价由多个**不同区间**并列取得：抛出 :class:`QuoteTieError`，
  携带全部并列区间；
- 最小代价大于 2：抛出 :class:`QuoteNotFoundError`。

本模块只做定位，绝不改写拼接文本。
"""

from __future__ import annotations

from .aligner import WILDCARD
from .assembler import assemble

MAX_QUOTE_LEN = 80   # 引文最大长度
MAX_COST = 2         # 仅接受最小代价不超过 2 的区间
_INF = 10 ** 9


class QuoteNotFoundError(ValueError):
    """拼接文本中不存在代价不超过 :data:`MAX_COST` 的区间。"""


class QuoteTieError(ValueError):
    """最小代价由多个不同区间并列取得，无法唯一定位。"""

    def __init__(self, cost: int, hits: list[dict]):
        self.cost = cost
        self.hits = hits
        super().__init__(
            f"引文在拼接文本中有 {len(hits)} 个不同区间以最小代价 {cost} 并列，"
            "无法唯一定位"
        )


def _pair_cost(a: str, b: str) -> int:
    """单对字符的代价：相同字或任一侧为 ``?`` 代价 0，否则替换代价 1。"""
    if a == WILDCARD or b == WILDCARD:
        return 0
    return 0 if a == b else 1


def _forward_costs(text: str, quote: str) -> list[list[int]]:
    """半全局编辑代价 DP（区间起点自由、引文必须完全消耗、区间非空）。

    求引文与任意非空拼接区间的最小逐字编辑代价。第 0 行 ``f[0][j] = 0``
    表示“尚未开始消耗引文、区间起点待定”，故字符对转移
    ``f[i-1][j-1] + pc`` 在 ``i = 1`` 时自然覆盖“区间自任意 ``j`` 起步、
    前面的引文字全部删除”；终点取 ``f[m][j]``（``j >= 1``）表示引文已
    完全消耗，区间在 ``j`` 结束（其后的拼接字不参与、不产生插入代价）。

    注意：此处不能使用 ``|j - i|`` 带状裁剪——最优路径可能因前导删除临时
    越带后再回到带内（带状只适用于固定长度片段的 :func:`_edit_cost`）。
    """
    m, n = len(quote), len(text)
    f = [[_INF] * (n + 1) for _ in range(m + 1)]
    for j in range(n + 1):
        f[0][j] = 0
    for i in range(1, m + 1):
        f[i][0] = i
        for j in range(1, n + 1):
            pc = _pair_cost(quote[i - 1], text[j - 1])
            f[i][j] = min(
                f[i - 1][j - 1] + pc,   # 字符对（i=1 时即区间自此起步）
                f[i][j - 1] + 1,        # 区间再纳入一个拼接字（插入）
                f[i - 1][j] + 1,        # 删除一个引文字，区间不变
            )
    return f


def _edit_cost(quote: str, fragment: str, cap: int) -> int:
    """引文与固定区间片段之间的逐字编辑代价（全填满 DP，无自由端）。

    只计算 ``|j - i| <= cap`` 的带状区域；实际代价超过 ``cap`` 时返回
    某个大于 ``cap`` 的值即可（调用方只区分“是否等于最优代价”）。
    """
    n = len(fragment)
    row = [_INF] * (n + 1)
    for j in range(min(n, cap) + 1):
        row[j] = j
    for i, qc in enumerate(quote, start=1):
        nxt = [_INF] * (n + 1)
        if i <= cap:
            nxt[0] = i
        for j in range(max(1, i - cap), min(n, i + cap) + 1):
            nxt[j] = min(
                row[j] + 1,                  # 删除一个引文字
                nxt[j - 1] + 1,              # 插入一个区间字
                row[j - 1] + _pair_cost(qc, fragment[j - 1]),  # 字符对
            )
        row = nxt
    return row[n]


def _enumerate_hits(text: str, quote: str, cap: int) -> tuple[int, list[tuple[int, int]]]:
    """枚举全部最优区间，返回 ``(最小代价, [(start, end), ...])``。

    先用正向半全局 DP 求出最小代价；超过 ``cap`` 时直接返回（无合格
    区间）。否则枚举取得该代价的全部不同区间：最优对齐若含前导/后导空缺列，
    删去这些空缺列不增加代价，故必存在一个不含前后导空缺列的等价最优对齐——
    其中插入列数与删除列数之差即区间长度与引文长度之差，而插入数、删除数均
    不超过总代价，故最优区间长度必落在 ``len(quote) ± best`` 之内。对每个
    这样的窗口用带状编辑代价复核即可（``best <= cap`` 时每窗口 O(len(quote)·
    cap)，窗口总数 O(n·cap)）。
    """
    m, n = len(quote), len(text)
    f = _forward_costs(text, quote)
    best = min(f[m][j] for j in range(1, n + 1))
    if best > cap:
        return best, []

    intervals: list[tuple[int, int]] = []
    for length in range(max(1, m - best), min(n, m + best) + 1):
        for start in range(0, n - length + 1):
            end = start + length
            if _edit_cost(quote, text[start:end], best) == best:
                intervals.append((start, end))
    intervals.sort()
    return best, intervals


def locate_quote(segments: list[str], quote: str) -> dict:
    """在 2～5 段转写的拼接文本中定位引文。

    返回唯一命中时的 ``{"text", "quote", "start", "end", "fragment",
    "cost", "sources"}``；拼接阶段失败原样抛出
    :class:`~app.assembler.AssemblyError`，无合格区间抛出
    :class:`QuoteNotFoundError`，多区间并列抛出 :class:`QuoteTieError`。
    """
    if not 1 <= len(quote) <= MAX_QUOTE_LEN:
        raise ValueError(f"引文长度需为 1～{MAX_QUOTE_LEN} 字")

    result = assemble(segments)
    text, sources = result["text"], result["sources"]

    best, intervals = _enumerate_hits(text, quote, MAX_COST)
    if best > MAX_COST:
        raise QuoteNotFoundError(
            f"引文（{len(quote)} 字）在拼接文本中最接近区间的代价为 {best}，"
            f"超过 {MAX_COST}，无合格区间"
        )

    def make_hit(start: int, end: int) -> dict:
        return {
            "start": start,
            "end": end,
            "fragment": text[start:end],
            "cost": best,
            "sources": [list(src) for src in sources[start:end]],
        }

    hits = [make_hit(s, e) for s, e in intervals]
    if len(hits) > 1:
        raise QuoteTieError(best, hits)

    return {"text": text, "quote": quote, **hits[0]}
