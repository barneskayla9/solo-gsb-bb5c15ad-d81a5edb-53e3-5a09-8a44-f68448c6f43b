"""成片切片规划：把修订稿字符区间映射到逐字时间轴，生成无声音重复的剪辑方案。

处理流程：
1. 校验选中区间：均为修订稿上的非空半开区间，按修订稿顺序排列且互不重叠
   （端点相接合法）；
2. 每个选中区间映射到逐字时间轴，取首字 start、末字 end 得到源录音区间，
   映射后为零时长的选中区间视为非法（422）；
3. 各源录音区间向两侧扩展 padding_ms 并裁剪到录音边界 [0, total_duration]；
4. 把重叠或相接（半开区间端点相等）的源录音区间合并为成片片段，
   避免同一段录音声音重复；
5. 合并段按源录音时间顺序拼接为成片（前段结束即后段开始，无间隙），
   并标注每个选中区间所属合并段及其在成片中的起止时间。
"""
from __future__ import annotations

from bisect import bisect_right

from .errors import InvalidTimelineError
from .schemas import (
    ClipPlanRequest,
    ClipPlanResponse,
    ClipSelectionPlan,
    ClipSegment,
    RevisedCharTimeline,
)


def validate_selections(req: ClipPlanRequest) -> None:
    """校验选中区间：非空、不越出修订稿、按修订稿顺序排列且互不重叠。

    前置条件：请求已通过 Pydantic 校验（选中区间 1～20 个、下标为整数）。
    """
    text_len = len(req.revised_text)
    prev_end = 0
    for idx, sel in enumerate(req.selections):
        if sel.start_index < 0 or sel.end_index > text_len:
            raise InvalidTimelineError(
                f"第 {idx} 个选中区间 [{sel.start_index}, {sel.end_index}) "
                f"超出修订稿范围（长度 {text_len}）")
        if sel.start_index >= sel.end_index:
            raise InvalidTimelineError(
                f"第 {idx} 个选中区间必须为非空半开区间：start_index="
                f"{sel.start_index} 必须小于 end_index={sel.end_index}")
        if idx > 0 and sel.start_index < prev_end:
            raise InvalidTimelineError(
                f"选中区间必须按修订稿顺序排列且互不重叠：第 {idx - 1} 个区间结束于"
                f" {prev_end}，第 {idx} 个区间起始为 {sel.start_index}（重叠）")
        prev_end = sel.end_index


def build_clip_plan(
        req: ClipPlanRequest,
        chars: list[RevisedCharTimeline]) -> ClipPlanResponse:
    """把选中字符区间映射到逐字时间轴并生成成片切片方案。"""
    total = req.total_duration
    padding = req.padding_ms

    # 各选中区间映射到源录音毫秒区间 [core_start, core_end)（留白前），
    # 再扩展留白并裁剪到录音边界。字符区间有序且时间轴单调不回退，结果同样有序。
    cores: list[tuple[int, int]] = []
    ranges: list[tuple[int, int]] = []
    for idx, sel in enumerate(req.selections):
        core_start = chars[sel.start_index].start
        core_end = chars[sel.end_index - 1].end
        if core_end <= core_start:
            raise InvalidTimelineError(
                f"第 {idx} 个选中区间 [{sel.start_index}, {sel.end_index})"
                f"（{req.revised_text[sel.start_index:sel.end_index]!r}）"
                f"映射到时间轴后为零时长 [{core_start}, {core_end})，无法切片")
        cores.append((core_start, core_end))
        ranges.append((
            max(0, core_start - padding),
            min(total, core_end + padding),
        ))

    # 合并重叠或相接（半开区间端点相等）的源录音区间。
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        elif end > merged[-1][1]:
            merged[-1] = (merged[-1][0], end)

    # 合并段按源顺序无间隙拼接成片；每个选中区间（留白前核心）整体位于
    # 某个合并段内，其成片时间为所在段成片起点加源内偏移。
    segments: list[ClipSegment] = []
    offsets: list[int] = []  # 各合并段的成片起点
    cursor = 0
    for start, end in merged:
        segments.append(ClipSegment(
            source_start=start,
            source_end=end,
            cut_start=cursor,
            cut_end=cursor + (end - start),
        ))
        offsets.append(cursor)
        cursor += end - start

    merge_starts = [seg.source_start for seg in segments]
    selection_plans: list[ClipSelectionPlan] = []
    for sel, (core_start, core_end) in zip(req.selections, cores):
        # 最后一个起点 <= core_start 的合并段即核心区间所属段。
        seg_idx = bisect_right(merge_starts, core_start) - 1
        base = offsets[seg_idx]
        source_base = segments[seg_idx].source_start
        selection_plans.append(ClipSelectionPlan(
            start_index=sel.start_index,
            end_index=sel.end_index,
            text=req.revised_text[sel.start_index:sel.end_index],
            segment_index=seg_idx,
            start=base + (core_start - source_base),
            end=base + (core_end - source_base),
        ))

    return ClipPlanResponse(
        segments=segments,
        total_duration=cursor,
        selections=selection_plans,
    )
