#!/usr/bin/env python3
"""Render Peco display snapshots. No task storage, network or execution side effects."""

import argparse
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path


MAX_INPUT_BYTES = 1024 * 1024
MAX_TEXT_LENGTH = 10_000
ASSETS = Path(__file__).resolve().parent.parent / "assets"
KINDS = {
    "core": "核心",
    "maintenance": "维护",
    "fuel": "燃料",
    "fixed": "固定安排",
    "buffer": "缓冲",
}
COMMON = {"date", "generated_at"}
FIELDS = {
    "day-plan": COMMON | {"note", "focus", "schedule", "first_action"},
    "start-card": COMMON | {"task", "support", "action", "stop", "return_point"},
    "review": COMMON | {"retained", "adjustment", "next_action"},
}


class RenderError(ValueError):
    pass


def object_only(value, where, allowed):
    if not isinstance(value, dict):
        raise RenderError(f"{where} 必须是 JSON 对象")
    if set(value) - allowed:
        raise RenderError(f"{where} 含未知字段；允许字段：{'、'.join(sorted(allowed))}")
    return value


def text_field(obj, key, where, required=False):
    if key not in obj:
        if required:
            raise RenderError(f"{where}.{key} 为必填字段")
        return ""
    value = obj[key]
    if not isinstance(value, str) or not value.strip():
        raise RenderError(f"{where}.{key} 必须是非空字符串")
    if len(value) > MAX_TEXT_LENGTH:
        raise RenderError(f"{where}.{key} 不得超过 {MAX_TEXT_LENGTH:,} 字符")
    return html.escape(value.strip(), quote=True)


def list_field(obj, key, maximum=None):
    value = obj.get(key, [])
    if not isinstance(value, list):
        raise RenderError(f"输入.{key} 必须是数组")
    if maximum is not None and len(value) > maximum:
        raise RenderError(f"输入.{key} 最多展示 {maximum} 项")
    return value


def action_fields(value, where):
    obj = object_only(value, where, {"task", "action", "stop"})
    return {key: text_field(obj, key, where, required=True) for key in ("task", "action", "stop")}


def empty_note(message):
    return f'<p class="empty">{message}</p>'


def day_content(data):
    cards = []
    for index, item in enumerate(list_field(data, "focus", 3), 1):
        where = f"输入.focus[{index - 1}]"
        item = object_only(item, where, {"title", "when", "result", "priority", "due"})
        title = text_field(item, "title", where, required=True)
        details = []
        for key, label in (("priority", "优先级"), ("due", "截止信息"), ("when", "安排时段"), ("result", "本轮成果")):
            value = text_field(item, key, where)
            if value:
                details.append(f'<div><dt>{label}</dt><dd>{value}</dd></div>')
        cards.append(f'<article class="focus-card"><span class="number">0{index}</span><h3>{title}</h3><dl>{"".join(details)}</dl></article>')
    rows = []
    for index, item in enumerate(list_field(data, "schedule")):
        where = f"输入.schedule[{index}]"
        item = object_only(item, where, {"time", "title", "result", "kind"})
        time = text_field(item, "time", where, required=True)
        title = text_field(item, "title", where, required=True)
        result = text_field(item, "result", where)
        kind = item.get("kind", "")
        if "kind" in item and (not isinstance(kind, str) or kind not in KINDS):
            raise RenderError(f"{where}.kind 只能是 core、maintenance、fuel、fixed 或 buffer")
        badge = f'<span class="tag {kind}">{KINDS[kind]}</span>' if kind else ""
        detail = f'<p>{result}</p>' if result else ""
        rows.append(f'<li><span class="time">{time}</span><div class="slot"><h3>{title}</h3>{detail}</div>{badge}</li>')
    if "first_action" in data:
        action = action_fields(data["first_action"], "输入.first_action")
        current = f'<p class="task-name">{action["task"]}</p><p class="action-text">{action["action"]}</p><p class="stop"><span>停止点</span>{action["stop"]}</p>'
    else:
        current = empty_note("当前入口尚未确定；确认条件后再选择。")
    note = text_field(data, "note", "输入")
    return {
        "NOTE": f'<p class="intro">{note}</p>' if note else "",
        "FOCUS_CARDS": "".join(cards) or empty_note("还没有确认今天的重点；不需要为了凑满三件事添加任务。"),
        "SCHEDULE_ROWS": "".join(rows) or '<li class="empty">尚无已确认时段；不据此推断当前空闲。</li>',
        "FIRST_ACTION": current,
    }


def start_content(data):
    fields = {key.upper(): text_field(data, key, "输入", required=True) for key in ("task", "action", "stop")}
    support = text_field(data, "support", "输入")
    return_point = text_field(data, "return_point", "输入")
    fields["SUPPORT"] = f'<p class="support">{support}</p>' if support else ""
    fields["RETURN_POINT"] = f'<div class="return-note"><span>返回位置</span><p>{return_point}</p></div>' if return_point else ""
    return fields


def review_content(data):
    retained = []
    for index, value in enumerate(list_field(data, "retained", 3)):
        text = text_field({"item": value}, "item", f"输入.retained[{index}]", required=True)
        retained.append(f'<li>{text}</li>')
    if data.get("next_action") is None:
        next_block = empty_note("下一步尚未确定；确认后再安排，不为了填满卡片添加任务。")
    else:
        action = action_fields(data["next_action"], "输入.next_action")
        next_block = f'<p class="task-name">{action["task"]}</p><p>{action["action"]}</p><p class="stop"><span>停止点</span>{action["stop"]}</p>'
    return {
        "RETAINED_ROWS": f'<ul class="retained">{"".join(retained)}</ul>' if retained else empty_note("暂无已确认成果；没有反馈的部分保留未知。"),
        "ADJUSTMENT": text_field(data, "adjustment", "输入", required=True),
        "NEXT_BLOCK": next_block,
    }


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RenderError("JSON 存在重复字段；请为每个字段保留一个值")
        result[key] = value
    return result


def load_payload(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise RenderError("输入 JSON 不得超过 1 MiB")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RenderError("输入应为合法的 UTF-8 JSON；请检查编码、引号和逗号") from exc


def render(template_name, payload):
    data = object_only(payload, "输入", FIELDS[template_name])
    values = {"DATE": text_field(data, "date", "输入", required=True), "GENERATED_AT": text_field(data, "generated_at", "输入", required=True)}
    if template_name == "day-plan":
        values.update(day_content(data))
    elif template_name == "start-card":
        values.update(start_content(data))
    else:
        values.update(review_content(data))
    template = (ASSETS / f"{template_name}.html").read_text(encoding="utf-8")
    names = set(re.findall(r"\{\{([A-Z_]+)\}\}", template))
    if names != set(values):
        raise RenderError("HTML 模板占位字段与渲染器不匹配；请使用同一版本的模板与脚本")
    return re.sub(r"\{\{([A-Z_]+)\}\}", lambda match: values[match.group(1)], template)


def write_output(path, content, force):
    if not force:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        return
    if path.is_symlink():
        raise RenderError("拒绝覆盖符号链接；请提供实际的私有 HTML 输出路径")
    # Atomic replacement preserves the prior file on a failed render/write.
    # A path switched to a symlink in the meantime is replaced, never followed.
    fd, temporary = tempfile.mkstemp(prefix=".peco-html-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description="将 Peco 展示数据渲染为离线 HTML；不读取或更新任务数据库。")
    parser.add_argument("--template", choices=sorted(FIELDS), required=True)
    parser.add_argument("--input", type=Path, required=True, help="UTF-8 JSON 展示数据")
    parser.add_argument("--output", type=Path, required=True, help="目标 HTML 路径；父目录须已存在")
    parser.add_argument("--force", action="store_true", help="明确允许覆盖已存在输出")
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.output.resolve():
            raise RenderError("输入与输出不能使用同一路径")
        output = render(args.template, load_payload(args.input))
        write_output(args.output, output, args.force)
    except FileExistsError:
        print("错误：输出已存在；换一个文件名，或明确使用 --force 覆盖。", file=sys.stderr)
        return 2
    except (RenderError, OSError, RecursionError) as exc:
        message = str(exc) if not isinstance(exc, RecursionError) else "JSON 嵌套过深；请使用规定的简洁展示结构"
        print(f"错误：{message}", file=sys.stderr)
        return 2
    print(json.dumps({"ok": True, "template": args.template, "output": str(args.output.resolve()), "snapshot_only": True}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
