"""古籍残页转写拼接 API。

- ``POST /api/align``：提交两段含 "?"（未辨认字）的转写文本，将左段后缀与
  右段前缀做最优对齐，返回最高分对齐的原文区间、逐列对齐和拼接文本。
- ``POST /api/assemble``：提交按阅读顺序排列的 2～5 段转写，逐段复用上述
  后缀-前缀对齐拼接，并返回最终文本与逐字来源列表。
- ``POST /api/collate``：沿用 ``/api/assemble`` 的请求限制与续接门槛复用其
  拼接文本和逐字来源，逐位置回查所有来源原字做段级投票，供校勘人员复核异文。
- ``POST /api/order``：提交 2～4 段**未排序**转写（单段不超过 100 字、总长不
  超过 240 字），穷举全部排列，逐段复用上述续接门槛与累计对齐得分选出阅读
  顺序，返回原输入下标顺序、拼接文本与按原输入下标标记的逐字来源。
- ``POST /api/revise``：提交与 ``/api/assemble`` 相同的有序 segments 及人工
  校订 edits（段序号、原文下标、替换字），沿逐字来源把校订定位到拼接文本并
  落实改字，返回修订后文本、沿用的逐字来源与逐位置修订记录。
- ``POST /api/locate-quote``：提交与 ``/api/assemble`` 相同的有序 segments 及
  1～80 字引文 quote，在拼接文本中以编辑代价（相同字或任一侧 ``?`` 代价 0，
  替换/插入/删除代价 1）查找引文位置；唯一命中返回左闭右开区间、原文片段、
  代价与该区间逐字 sources，多区间并列返回 409，无合格区间返回 422。
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from .aligner import WILDCARD, align
from .assembler import AssemblyError, assemble
from .collator import collate
from .locator import (
    MAX_QUOTE_LEN as LOCATE_MAX_QUOTE_LEN,
)
from .locator import QuoteNotFoundError, QuoteTieError, locate_quote
from .orderer import (
    MAX_SEGMENT_LEN as ORDER_MAX_SEGMENT_LEN,
)
from .orderer import (
    MAX_SEGMENTS as ORDER_MAX_SEGMENTS,
)
from .orderer import (
    MAX_TOTAL_LEN as ORDER_MAX_TOTAL_LEN,
)
from .orderer import (
    MIN_SEGMENTS as ORDER_MIN_SEGMENTS,
)
from .orderer import NoCompleteOrderError, OrderTieError, determine_order
from .reviser import EditConflictError, EditLocationError, revise

MAX_TEXT_LEN = 500   # 单段文本最大长度（残页转写通常远小于此）
MAX_SEGMENTS = 5     # /api/assemble 最大段数
MIN_SEGMENTS = 2     # /api/assemble 最小段数
MAX_TOTAL_LEN = 800  # /api/assemble 各段总长上限

app = FastAPI(
    title="古籍残页转写拼接 API",
    description=(
        "对两段含 `?`（未辨认字）的古籍残页转写文本做重叠拼接："
        "只将左段后缀与右段前缀对齐，左段未参与对齐的前缀、"
        "右段未参与对齐的后缀不计分。"
    ),
    version="1.0.0",
)


class AlignRequest(BaseModel):
    """拼接请求。"""

    left: str = Field(
        ..., min_length=1, max_length=MAX_TEXT_LEN,
        description="左段转写文本，未辨认字以 ? 表示",
        examples=["学而?习之"],
    )
    right: str = Field(
        ..., min_length=1, max_length=MAX_TEXT_LEN,
        description="右段转写文本，未辨认字以 ? 表示",
        examples=["时习之不亦"],
    )


class Interval(BaseModel):
    """原文区间（下标从 0 开始，左闭右开）。"""

    start: int = Field(..., description="起点下标（含）")
    end: int = Field(..., description="终点下标（不含）")
    text: str = Field(..., description="区间原文")


class Column(BaseModel):
    """逐列对齐中的一列。"""

    left_pos: int | None = Field(..., description="左段下标；空缺为 null")
    left_char: str | None = Field(..., description="左段字形；空缺为 null")
    right_pos: int | None = Field(..., description="右段下标；空缺为 null")
    right_char: str | None = Field(..., description="右段字形；空缺为 null")
    type: str = Field(
        ...,
        description="match=相同字 / wildcard=含? / mismatch=异字 / "
                    "gap_left=左段空缺 / gap_right=右段空缺",
    )
    score: int = Field(..., description="本列得分")


class Conflict(BaseModel):
    """异字冲突：拼接时保留左段字。"""

    left_pos: int = Field(..., description="左段原文位置")
    left_char: str = Field(..., description="左段字形（拼接保留）")
    right_pos: int = Field(..., description="右段原文位置")
    right_char: str = Field(..., description="右段字形")


class ResolvedWildcard(BaseModel):
    """"?" 补字记录：以对方已知字替换。"""

    segment: str = Field(..., description="left / right")
    pos: int = Field(..., description="该段原文位置")
    resolved_char: str = Field(..., description="补入的字形")


class AlignResponse(BaseModel):
    """拼接结果。"""

    score: int = Field(..., description="最高对齐得分")
    aligned_pairs: int = Field(..., description="对齐字符对数")
    left_interval: Interval = Field(..., description="左段参与对齐的后缀区间")
    right_interval: Interval = Field(..., description="右段参与对齐的前缀区间")
    left_free_prefix: str = Field(..., description="左段未参与对齐的前缀（不计分）")
    right_free_suffix: str = Field(..., description="右段未参与对齐的后缀（不计分）")
    aligned_left: str = Field(..., description="左段对齐串（空缺以 - 表示）")
    aligned_right: str = Field(..., description="右段对齐串（空缺以 - 表示）")
    columns: list[Column] = Field(..., description="逐列对齐")
    merged: str = Field(..., description="拼接文本")
    conflicts: list[Conflict] = Field(..., description="异字冲突列表")
    resolved_wildcards: list[ResolvedWildcard] = Field(..., description="? 补字列表")


class AssembleRequest(BaseModel):
    """多段顺序拼接请求。"""

    segments: list[str] = Field(
        ...,
        min_length=MIN_SEGMENTS,
        max_length=MAX_SEGMENTS,
        description="按阅读顺序排列的转写文本（2～5 段），未辨认字以 ? 表示",
        examples=[["学而?习之", "时习之不", "之不亦乐乎"]],
    )

    @model_validator(mode="after")
    def _validate_segments(self):
        for idx, seg in enumerate(self.segments):
            if len(seg) < 1:
                raise ValueError(f"第 {idx + 1} 段文本不能为空")
            if len(seg) > MAX_TEXT_LEN:
                raise ValueError(f"第 {idx + 1} 段文本超过 {MAX_TEXT_LEN} 字")
        total = sum(len(seg) for seg in self.segments)
        if total > MAX_TOTAL_LEN:
            raise ValueError(f"各段文本总长 {total} 字，超过 {MAX_TOTAL_LEN} 字上限")
        return self


class CharSource(BaseModel):
    """单个原始段对某输出字的来源标记（段序号、下标均从 0 开始）。"""

    segment: int = Field(..., description="原始段序号（从 0 开始）")
    pos: int = Field(..., description="该字在原始段中的下标（从 0 开始）")


class AssembleResponse(BaseModel):
    """多段顺序拼接结果。"""

    text: str = Field(..., description="最终拼接文本")
    sources: list[list[CharSource]] = Field(
        ...,
        description="逐字来源列表，与 text 等长；每项保留贡献该字的所有原始段"
                    "序号及原文下标（相同字、? 补字双方、异字冲突双方均保留）",
    )


class VariantCandidate(BaseModel):
    """异文位置上的一个候选字及其支持段。"""

    char: str = Field(..., description="候选原字（已知字）")
    segments: list[int] = Field(
        ...,
        description="支持该候选字的原始段序号（从 0 开始，升序、去重）；"
                    "以原始段为投票单位，同段重复来源只计一次，? 不投票",
    )


class TextVariant(BaseModel):
    """一个异文位置的复核信息。"""

    pos: int = Field(..., description="在拼接文本中的位置（下标从 0 开始）")
    current: str = Field(..., description="当前拼接文本在该位置的字（不自动改写）")
    candidates: list[VariantCandidate] = Field(
        ...,
        description="各候选字及支持的段序号，按得票数降序、"
                    "并列时按最小支持段序号升序排列",
    )
    suggestion: str | None = Field(
        ...,
        description="建议字：唯一最高票候选；最高票并列时为 null",
    )


class CollateResponse(BaseModel):
    """异文校勘结果：仅列出至少两种不同已知字的位置。"""

    text: str = Field(..., description="拼接文本（与 /api/assemble 的结果一致，不被改写）")
    variants: list[TextVariant] = Field(
        ...,
        description="异文位置列表，按拼接文本位置升序；"
                    "纯补字（只有一种已知字）的位置不列入",
    )


class OrderRequest(BaseModel):
    """未排序多段转写的排序请求。"""

    segments: list[str] = Field(
        ...,
        min_length=ORDER_MIN_SEGMENTS,
        max_length=ORDER_MAX_SEGMENTS,
        description="未排序的转写文本（2～4 段），未辨认字以 ? 表示",
        examples=[["之不亦乐乎", "学而?习之", "时习之不"]],
    )

    @model_validator(mode="after")
    def _validate_segments(self):
        for idx, seg in enumerate(self.segments):
            if len(seg) < 1:
                raise ValueError(f"第 {idx + 1} 段文本不能为空")
            if len(seg) > ORDER_MAX_SEGMENT_LEN:
                raise ValueError(f"第 {idx + 1} 段文本超过 {ORDER_MAX_SEGMENT_LEN} 字")
        total = sum(len(seg) for seg in self.segments)
        if total > ORDER_MAX_TOTAL_LEN:
            raise ValueError(f"各段文本总长 {total} 字，超过 {ORDER_MAX_TOTAL_LEN} 字上限")
        return self


class OrderResult(BaseModel):
    """一个完整阅读顺序及其拼接结果。"""

    order: list[int] = Field(
        ...,
        description="阅读顺序，以原输入下标表示（从 0 开始）",
    )
    score: int = Field(..., description="该顺序各步对齐得分的累计总分")
    text: str = Field(..., description="按该顺序拼接得到的文本")
    sources: list[list[CharSource]] = Field(
        ...,
        description="逐字来源列表，与 text 等长；段序号按原输入下标标记，"
                    "规则与 /api/assemble 一致",
    )


class OrderResponse(OrderResult):
    """唯一最高分时的排序结果。"""


class EditItem(BaseModel):
    """一项人工校订：把某原始段某下标的字校改为指定字。"""

    segment: int = Field(..., ge=0, description="原始段序号（从 0 开始）", examples=[2])
    pos: int = Field(..., ge=0, description="该段原文下标（从 0 开始）", examples=[0])
    char: str = Field(
        ..., min_length=1, max_length=1,
        description="替换字（单个非 ? 字符）",
        examples=["知"],
    )

    @field_validator("char")
    @classmethod
    def _char_not_wildcard(cls, v: str) -> str:
        if v == WILDCARD:
            raise ValueError("替换字不能为 ?")
        return v


class ReviseRequest(AssembleRequest):
    """人工校订请求：与 /api/assemble 相同的有序 segments，外加校订列表。"""

    edits: list[EditItem] = Field(
        ...,
        description="人工校订列表，每项含原始段序号 segment、原文下标 pos、"
                    "替换字 char（单个非 ? 字符）",
        examples=[[{"segment": 2, "pos": 0, "char": "知"}]],
    )


class EditRef(BaseModel):
    """关联校订项：原样回显校订内容及其在请求 edits 列表中的下标。"""

    index: int = Field(..., description="该校订在请求 edits 列表中的下标（从 0 开始）")
    segment: int = Field(..., description="原始段序号（从 0 开始）")
    pos: int = Field(..., description="该段原文下标（从 0 开始）")
    char: str = Field(..., description="替换字")


class TextChange(BaseModel):
    """一个输出位置上落实的修订。"""

    pos: int = Field(..., description="在拼接文本中的位置（下标从 0 开始）")
    from_char: str = Field(..., description="原字（修订前拼接文本在该位置的字）")
    to_char: str = Field(..., description="新字（校订落实后的字）")
    edits: list[EditRef] = Field(
        ...,
        description="关联校订项：经重叠段指向同一位置且改字相同的多项校订合并于此",
    )


class ReviseResponse(BaseModel):
    """人工校订结果。"""

    text: str = Field(..., description="修订后的文本（未涉及位置保持原字）")
    sources: list[list[CharSource]] = Field(
        ...,
        description="逐字来源列表，沿用 /api/assemble 的拼接结果，不重新对齐",
    )
    changes: list[TextChange] = Field(
        ...,
        description="逐位置修订列表，按位置升序；未涉及位置不列入",
    )


class LocateQuoteRequest(AssembleRequest):
    """引文定位请求：与 /api/assemble 相同的有序 segments，外加引文 quote。"""

    quote: str = Field(
        ...,
        min_length=1,
        max_length=LOCATE_MAX_QUOTE_LEN,
        description="待定位引文（1～80 字），未辨认字以 ? 表示，可与拼接文本中的 ? 免代价配对",
        examples=["时习之"],
    )


class QuoteHit(BaseModel):
    """引文在拼接文本中的一个命中区间。"""

    start: int = Field(..., description="区间起点下标（含，从 0 开始）")
    end: int = Field(..., description="区间终点下标（不含，左闭右开）")
    fragment: str = Field(..., description="区间原文片段（拼接文本 [start, end)）")
    cost: int = Field(..., description="引文与该区间对齐的最小代价")
    sources: list[list[CharSource]] = Field(
        ...,
        description="该区间逐字来源列表，与 fragment 等长，规则与 /api/assemble 一致",
    )


class LocateQuoteResponse(QuoteHit):
    """引文唯一命中时的定位结果。"""

    text: str = Field(..., description="拼接文本（与 /api/assemble 的结果一致，不被改写）")
    quote: str = Field(..., description="原样回显的引文")


@app.get("/", summary="服务信息")
def index():
    return {
        "name": "古籍残页转写拼接 API",
        "align": "POST /api/align",
        "assemble": "POST /api/assemble",
        "collate": "POST /api/collate",
        "order": "POST /api/order",
        "revise": "POST /api/revise",
        "locate_quote": "POST /api/locate-quote",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/health", summary="健康检查")
def health():
    return {"status": "ok"}


@app.post(
    "/api/align",
    response_model=AlignResponse,
    summary="拼接两段残页转写文本",
    description=(
        "计分规则：相同字 +2；`?` 与任意字配对 0；异字 -2；"
        "连续空缺首字 -3、其余每字 -1。至少一对字符对齐。"
        "同分取对齐字符对数最多者，再取左段起点最小者。"
    ),
)
def api_align(req: AlignRequest):
    return align(req.left, req.right)


@app.post(
    "/api/assemble",
    response_model=AssembleResponse,
    status_code=200,
    summary="按阅读顺序逐段拼接 2～5 段残页转写",
    description=(
        "每段最长 500 字、总长最长 800 字。逐段复用后缀-前缀对齐：将当前拼接"
        "文本与下一段合并。每一步仅在最高对齐得分大于 0、且至少有 2 对相同"
        "已知字时继续；任一步不满足即返回 422 并指出失败段序号，不产生部分"
        "结果。返回最终文本及与其等长的逐字来源列表，来源保留贡献该字的所有"
        "原始段序号及原文下标（相同字、`?` 补字双方、异字冲突双方均保留）。"
    ),
)
def api_assemble(req: AssembleRequest):
    try:
        return assemble(req.segments)
    except AssemblyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "failed_segment": exc.failed_segment,
            },
        ) from exc


@app.post(
    "/api/collate",
    response_model=CollateResponse,
    status_code=200,
    summary="校勘复核：段级投票汇总 2～5 段残页转写的异文",
    description=(
        "请求沿用 `/api/assemble` 的 segments 限制（2～5 段、单段最长 500 字、"
        "总长最长 800 字）与续接门槛（每步最高对齐得分 > 0 且至少 2 对相同已知字，"
        "否则返回 422 并给出 failed_segment），复用其拼接文本和逐字来源。"
        "逐个输出位置回查所有来源原字，以原始段为投票单位：同段重复来源只计一次，"
        "`?` 不投票。仅返回至少有两种不同已知字的位置，列出位置、当前拼接字、"
        "各候选字及支持的段序号；唯一最高票给建议字，并列最高票则建议为 null。"
        "补字但无异文的位置不列入，接口不自动改写拼接文本。"
    ),
)
def api_collate(req: AssembleRequest):
    try:
        return collate(req.segments)
    except AssemblyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "failed_segment": exc.failed_segment,
            },
        ) from exc


@app.post(
    "/api/order",
    response_model=OrderResponse,
    status_code=200,
    summary="穷举 2～4 段未排序转写的可行阅读顺序",
    description=(
        "单段最长 100 字、总长最长 240 字。穷举全部排列（至多 4! = 24 种），"
        "每一步把当前拼接文本与下一原段交给现有后缀-前缀对齐逻辑，沿用续接门槛"
        "（最高对齐得分 > 0 且至少 2 对相同已知字，否则废弃该排列），累计各步"
        "对齐得分作为该顺序总分（不按原段两两相似度排序）。取累计得分最高的"
        "完整顺序，返回原输入下标顺序、拼接文本及按原输入下标标记的逐字来源，"
        "拼接/来源规则与 /api/assemble 一致。最高分并列时返回 **409** 并给出全部"
        "并列顺序；不存在任何完整顺序时返回 **422**。"
    ),
    responses={
        409: {
            "description": "最高累计得分存在多个并列的完整顺序",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "message": "最高累计得分 8 的完整顺序有 2 个并列，无法唯一确定阅读顺序",
                            "score": 8,
                            "ties": [
                                {
                                    "order": [0, 1, 2],
                                    "score": 8,
                                    "text": "…",
                                    "sources": [[{"segment": 0, "pos": 0}]],
                                },
                                {
                                    "order": [0, 2, 1],
                                    "score": 8,
                                    "text": "…",
                                    "sources": [[{"segment": 0, "pos": 0}]],
                                },
                            ],
                        }
                    }
                }
            },
        },
        422: {
            "description": "不存在任何完整顺序（或请求参数不合法）",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "message": "没有任何排列能通过续接门槛（每步最高对齐得分 > 0 且至少 2 对相同已知字），无法确定完整阅读顺序"
                        }
                    }
                }
            },
        },
    },
)
def api_order(req: OrderRequest):
    try:
        return determine_order(req.segments)
    except OrderTieError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "score": exc.score,
                "ties": exc.tied,
            },
        ) from exc
    except NoCompleteOrderError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc)},
        ) from exc


@app.post(
    "/api/revise",
    response_model=ReviseResponse,
    status_code=200,
    summary="在拼接文本上落实人工校订",
    description=(
        "请求沿用 `/api/assemble` 的 segments 限制（2～5 段、单段最长 500 字、"
        "总长最长 800 字）与续接门槛（不满足返回 422 并给出 failed_segment），"
        "先复用其拼接得到文本与逐字来源。每项校订给出原始段序号 segment、原文"
        "下标 pos 与替换字 char（单个非 `?` 字符），接口沿逐字来源把校订定位到"
        "拼接文本位置：重叠段指向同一位置且改字相同的多项校订合并为一处修改；"
        "同一位置被指向不同改字时返回 **409** 并列出全部冲突位置与校订项。"
        "段序号或下标越界、校订坐标未映射或映射到多个输出位置均返回 **422**。"
        "任一环节失败即不返回任何结果（不产生部分修订）；不重新对齐，未涉及"
        "位置保持原字，sources 沿用拼接结果。返回修订后文本、沿用的逐字来源及"
        "逐位置 changes（含原字、新字、关联校订项）。"
    ),
    responses={
        409: {
            "description": "同一输出位置被多项校订指向不同改字",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "message": "校订冲突：输出位置 4 被多项校订指向不同改字，无法落实修订",
                            "conflicts": [
                                {
                                    "pos": 4,
                                    "edits": [
                                        {"index": 0, "segment": 0, "pos": 4, "char": "甲"},
                                        {"index": 1, "segment": 1, "pos": 2, "char": "乙"},
                                    ],
                                }
                            ],
                        }
                    }
                }
            },
        },
        422: {
            "description": "续接失败、段序号/下标越界，或校订坐标无法唯一定位",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "message": "第 1 项校订的段序号 5 越界：共 3 段（段序号 0～2）"
                        }
                    }
                }
            },
        },
    },
)
def api_revise(req: ReviseRequest):
    try:
        return revise(req.segments, [e.model_dump() for e in req.edits])
    except AssemblyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "failed_segment": exc.failed_segment,
            },
        ) from exc
    except EditLocationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc)},
        ) from exc
    except EditConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "conflicts": exc.conflicts,
            },
        ) from exc


@app.post(
    "/api/locate-quote",
    response_model=LocateQuoteResponse,
    status_code=200,
    summary="在拼接文本中定位一段引文",
    description=(
        "请求沿用 `/api/assemble` 的 segments 限制（2～5 段、单段最长 500 字、"
        "总长最长 800 字）与续接门槛（不满足返回 422 并给出 failed_segment），"
        "另附 1～80 字的引文 `quote`。接口先复用其拼接得到文本与逐字来源，再把"
        "引文与拼接文本的**任意非空连续区间**比较：相同字、或任一侧为 `?` 的"
        "字符对代价 0；替换、插入、删除各代价 1。区间起点与终点均自由（引文必须"
        "完全消耗），仅接受最小代价不超过 2 的区间。最小代价由多个**不同区间**"
        "并列取得时返回 **409** 并按起点升序列出全部并列区间（各带原文片段、代价"
        "与逐字 sources）；最小代价大于 2、无合格区间时返回 **422**。唯一命中时"
        "返回左闭右开区间 [start, end)、原文片段 fragment、代价 cost 与该区间逐字"
        "sources；接口只做定位，不改写拼接文本。"
    ),
    responses={
        409: {
            "description": "最小代价由多个不同区间并列取得，无法唯一定位",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "message": "引文在拼接文本中有 2 个不同区间以最小代价 0 并列，无法唯一定位",
                            "cost": 0,
                            "ties": [
                                {
                                    "start": 1,
                                    "end": 2,
                                    "fragment": "甲",
                                    "cost": 0,
                                    "sources": [[{"segment": 0, "pos": 1}]],
                                },
                                {
                                    "start": 4,
                                    "end": 5,
                                    "fragment": "甲",
                                    "cost": 0,
                                    "sources": [[{"segment": 1, "pos": 0}]],
                                },
                            ],
                        }
                    }
                }
            },
        },
        422: {
            "description": "续接失败、参数不合法，或无代价不超过 2 的合格区间",
            "content": {
                "application/json": {
                    "example": {
                        "detail": {
                            "message": "引文（2 字）在拼接文本中最接近区间的代价为 3，超过 2，无合格区间"
                        }
                    }
                }
            },
        },
    },
)
def api_locate_quote(req: LocateQuoteRequest):
    try:
        return locate_quote(req.segments, req.quote)
    except AssemblyError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": str(exc),
                "failed_segment": exc.failed_segment,
            },
        ) from exc
    except QuoteNotFoundError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc)},
        ) from exc
    except QuoteTieError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "cost": exc.cost,
                "ties": exc.hits,
            },
        ) from exc
