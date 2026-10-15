# -*- coding: utf-8 -*-
"""SCUT 社团成员录入：仅操作浏览器，需人工登录/选社团/最终申请。"""
import argparse
import json
import re
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
URL = 'https://ehall.scut.edu.cn/fp/view?m=fp#act=fp/formHome'
POLITICS = {
    '群众': '群众',
    '共青团员': '共青团员',
    '共青团团员': '共青团员',
    '党员': '中共党员（含预备党员）',
    '中共党员': '中共党员（含预备党员）',
    '中共预备党员': '中共党员（含预备党员）',
    '中共党员（含预备党员）': '中共党员（含预备党员）',
    '其他民主党派': '其他民主党派',
    '其他民主党派人士': '其他民主党派',
}

def load_people():
    people = json.loads((ROOT / 'members.json').read_text(encoding='utf-8'))
    ids = [str(p['student_id']).strip() for p in people]
    if len(set(ids)) != len(ids):
        raise ValueError('成员名单存在重复学号，请检查')
    for p in people:
        for key in ('student_id', 'name', 'phone', 'politics'):
            if not str(p.get(key, '')).strip():
                raise ValueError(f'数据不完整：{p!r} 缺少 {key}')
        if p['politics'].strip() not in POLITICS:
            raise ValueError(f"政治面貌未定义映射：{p['politics']} ({p['name']})")
    return people

def find_add(context, seconds=40):
    """页面里的新增按钮在 iframe 中；弹窗字段却在顶层 document。"""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for tab in reversed(context.pages):
            if tab.is_closed():
                continue
            for frame in tab.frames:
                try:
                    loc = frame.locator('a.link_btn').filter(has_text=re.compile(r'^\s*新增\s*$'))
                    for n in range(min(loc.count(), 12)):
                        if loc.nth(n).is_visible():
                            return tab, frame, loc.nth(n)
                except Exception:
                    pass
        time.sleep(.5)
    for i, tab in enumerate(context.pages, 1):
        print('页面', i, tab.url)
        try:
            tab.screenshot(path=str(ROOT / f'debug_page_{i}.png'))
        except Exception:
            pass
    raise RuntimeError('没有发现 iframe 内可见的 a.link_btn“新增”按钮。确认在自动打开的浏览器中完成登录并进入表单。')

def popup_inputs(page):
    # 按已验证的 HTML 标记定位；检查必须恰好三个，防止误填申请人信息。
    loc = page.locator('input[ispreset="false"][required="true"]:visible')
    if loc.count() != 3:
        # 某些构建版本只设置 required/notnull；优先不执行不确定的填写。
        raise RuntimeError(f'主页面预期可见 3 个成员输入框，实际找到 {loc.count()} 个；已停止以防误填')
    return loc

def popup_dropdown(page):
    loc = page.locator('div.bootstrap-select:visible').filter(
        has=page.locator('button.dropdown-toggle'))
    if loc.count() != 1:
        raise RuntimeError(f'应有 1 个可见的 bootstrap-select 政治面貌组件，实际找到 {loc.count()} 个')
    return loc.first

def select_politics(page, political):
    label = POLITICS[political.strip()]
    widget = popup_dropdown(page)
    widget.locator('button.dropdown-toggle').click()
    # selectpicker 使用 li > a；严格匹配整段可见文本，避免包含关系误选。
    opts = widget.locator('ul.dropdown-menu.inner li a:visible').filter(
        has_text=re.compile(r'^\s*' + re.escape(label) + r'\s*$'))
    if opts.count() != 1:
        raise RuntimeError(f'政治面貌“{label}”匹配项数量 {opts.count()}（应为1）；请手动检查')
    opts.first.click()
    shown = widget.locator('button.dropdown-toggle').inner_text().strip()
    if label not in shown:
        raise RuntimeError(f'政治面貌未成功显示为“{label}”，当前为“{shown}”')

def click_confirm(page):
    # 弹窗位于主页面；确定按钮可能是普通 button / a。
    candidates = page.locator('button:visible, a:visible').filter(
        has_text=re.compile(r'^\s*确定\s*$'))
    if candidates.count() != 1:
        raise RuntimeError(f'弹窗“确定”按钮数量为 {candidates.count()}，不自动点击')
    candidates.first.click()

def add_one(page, frame, person, commit):
    # 每次都重新定位，处理 DOM 重绘。
    add = frame.locator('a.link_btn').filter(has_text=re.compile(r'^\s*新增\s*$'))
    visible = [add.nth(i) for i in range(min(add.count(), 12)) if add.nth(i).is_visible()]
    if len(visible) != 1:
        raise RuntimeError('“新增”按钮不是唯一可见项，终止以防误操作')
    visible[0].click()
    page.get_by_text('新增数据', exact=True).first.wait_for(state='visible', timeout=10000)
    fields = popup_inputs(page)
    for i, val in enumerate((person['student_id'], person['name'], person['phone'])):
        fields.nth(i).fill(str(val).strip())
    select_politics(page, person['politics'])
    if not commit:
        return
    click_confirm(page)
    page.get_by_text('新增数据', exact=True).first.wait_for(state='hidden', timeout=15000)
    # 读取临时成员表：可能位于 iframe，也可能在顶层 document。
    sid = str(person['student_id']).strip()
    found = False
    for _ in range(20):
        try:
            if frame.get_by_text(sid, exact=True).count() or page.get_by_text(sid, exact=True).count():
                found = True
                break
        except Exception:
            pass
        time.sleep(.3)
    if not found:
        raise RuntimeError(f'弹窗关闭后没在页面找到学号 {sid}；请人工确认是否已添加，避免重复')

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--commit', action='store_true', help='逐人点新增弹窗“确定”，不点整份表单的“申请”')
    ap.add_argument('--start', type=int, default=1, help='从第几位开始（从1计数）')
    ap.add_argument('--limit', type=int, default=None, help='本次最多处理几位；建议先 --limit 1')
    args = ap.parse_args()
    people = load_people()
    if args.start < 1 or args.start > len(people):
        ap.error('--start 超出成员人数')
    if args.limit is not None and args.limit < 1:
        ap.error('--limit 必须大于0')
    targets = list(enumerate(people, 1))[args.start - 1:]
    if args.limit:
        targets = targets[:args.limit]
    print(f'名单 {len(people)} 人；本次目标 {len(targets)} 人；模式：{"添加到申请表" if args.commit else "仅填第一人供检查"}')
    if not args.commit:
        targets = targets[:1]
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp('http://127.0.0.1:9222', timeout=15000)
        except Exception as exc:
            raise SystemExit(
                '连接 Chrome 失败。请先用以下 PowerShell 命令启动调试浏览器：\n'
                '& "C:/Program Files/Google/Chrome/Application/chrome.exe" '
                '--remote-debugging-port=9222 '
                '--user-data-dir="$env:LOCALAPPDATA\\ChromeAutomation"\n'
                '然后在该 Chrome 中手动登录学校系统并进入批量新增社团成员页面。\n'
                f'原始错误：{exc}'
            )
        if not browser.contexts:
            raise SystemExit('已连接 Chrome，但没有可用的浏览器上下文。')
        context = browser.contexts[0]
        print('已连接现有 Chrome。不会自动跳转或重新登录。')
        print('请确认该 Chrome 中已进入“批量新增社团成员”、选好社团、可看到“新增”按钮。')
        input('确认后按 Enter：')
        page, frame, _ = find_add(context)
        print('找到新增按钮：', page.url, '| frame:', frame.url)
        if args.commit:
            if input(f'将添加 {len(targets)} 人（不会点最终“申请”），输入 YES 确认：') != 'YES':
                print('已取消'); return
        added = []
        for idx, person in targets:
            print(f'[{idx}/{len(people)}] {person["name"]} {person["student_id"]}')
            try:
                add_one(page, frame, person, args.commit)
                if args.commit:
                    added.append(str(person['student_id']))
                    (ROOT / 'progress.json').write_text(json.dumps({
                        'note':'仅代表本轮检测到加入网页临时表格，并不等于已提交学校申请',
                        'added_ids_this_run':added,
                        'last_list_index':idx
                    }, ensure_ascii=False, indent=2), encoding='utf-8')
                else:
                    print('测试填好：请检查四个字段；未点“确定”。')
            except Exception as exc:
                print(f'第 {idx} 人出错：{exc}。已停止；请人工判断是否已添加，避免重复。')
                try:
                    page.screenshot(path=str(ROOT / 'debug_error.png'))
                except Exception:
                    pass
                break
        if args.commit:
            print(f'本轮已检测成功加入临时表格：{len(added)}/{len(targets)}。请在网页核对，再人工点底部“申请”。')
        input('检查结束后按 Enter 退出脚本（Chrome 保持打开）：')
        # 不调用 browser.close()，避免关闭用户手动启动的 Chrome。

if __name__ == '__main__':
    main()
