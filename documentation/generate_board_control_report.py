"""Inspect Make targets and preprocess their real configuration; emit an offline HTML report.

Run from the workspace: python bldc/documentation/generate_board_control_report.py
Requires GNU make and the ARM GCC toolchain already shipped in bldc/tools.
This only preprocesses configuration headers; it does not build or flash firmware.
"""

from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from html import escape
from pathlib import Path
import json
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "documentation" / "板卡_BLDC六步与FOC控制分析.html"
GCC = ROOT / "tools/gcc-arm-none-eabi-7-2018-q2-update/bin/arm-none-eabi-gcc.exe"


def run(args, **kwargs):
    return subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kwargs)


def make_value(name, expression, makefile=None):
    args = ["make", "-s"]
    if makefile:
        args += ["-f", makefile]
    result = run(args + ["--eval", f"{name}:;@echo $({expression})", name])
    if result.returncode:
        raise RuntimeError(result.stderr)
    return result.stdout.strip().split()


def line_of(relative, text):
    lines = (ROOT / relative).read_text(encoding="utf-8", errors="replace").splitlines()
    return next(i for i, line in enumerate(lines, 1) if text in line)


def href(path):
    return "../" + Path(path).as_posix()


def source_link(path, label=None, line=None):
    label = label or Path(path).as_posix()
    if line:
        label += f":{line}"
    return f'<a href="{escape(href(path), quote=True)}">{escape(label)}</a>'


def inspect_board(name, headers, includes):
    header = headers[name]
    relative = header.relative_to(ROOT).as_posix()
    # The include order is the real conf_general.h -> hw.h -> HW_HEADER -> mcconf_default.h.
    args = [str(GCC), "-E", "-dM", "-H", "-x", "c", "-mcpu=cortex-m4", "-mthumb",
            "-mfloat-abi=hard", "-mfpu=fpv4-sp-d16", "-DUSE_STDPERIPH_DRIVER",
            "-DBOARD_OTG_NOVBUSSENS", "-DHW_SOURCE=1", f'-DHW_HEADER="{relative}"',
            "-I."] + ["-I" + inc for inc in includes] + ["-"]
    result = run(args, input='#include "conf_general.h"\n', timeout=60)
    if result.returncode:
        raise RuntimeError(f"{name}: {result.stderr}")
    macros = {}
    for line in result.stdout.splitlines():
        match = re.match(r"#define\s+(\w+)(?:\([^)]*\))?\s*(.*)", line)
        if match:
            macros[match[1]] = match[2].strip()
    dual = "HW_HAS_DUAL_MOTORS" in macros
    parallel = "HW_HAS_DUAL_PARALLEL" in macros
    single_hook = "HW_SET_SINGLE_MOTOR" in macros
    kind = "parallel" if parallel else "dual" if dual and not single_hook else "selectable"
    if single_hook:
        raise RuntimeError(f"{name}: requires manual review of HW_SET_SINGLE_MOTOR")
    motor_type = macros.get("MCCONF_DEFAULT_MOTOR_TYPE")
    if motor_type not in ("MOTOR_TYPE_FOC", "2"):
        raise RuntimeError(f"{name}: unexpected default {motor_type}")
    # -H provides the active include chain, so Luna's inactive 48V/60V branch
    # cannot accidentally be used as the reported default-value source.
    chain = []
    for included in re.findall(r'^\.+\s+(.+)$', result.stderr, re.M):
        candidate = (ROOT / included.strip()).resolve()
        if candidate.is_relative_to(ROOT / "hwconf") and candidate.exists():
            chain.append(candidate)
    origins = {}
    for key in ("HW_HAS_DUAL_MOTORS", "HW_HAS_DUAL_PARALLEL", "MCCONF_DEFAULT_MOTOR_TYPE"):
        if key not in macros:
            continue
        for candidate in chain:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            for lineno, line in enumerate(lines, 1):
                if re.match(r"\s*#\s*define\s+" + key + r"\b", line):
                    if key == "MCCONF_DEFAULT_MOTOR_TYPE":
                        value = re.sub(r"^\s*#\s*define\s+" + key + r"\s+", "", line).split()[0]
                        if value != motor_type:
                            continue
                    origins[key] = [candidate.relative_to(ROOT).as_posix(), lineno]
                    break
            if key in origins:
                break
    if "MCCONF_DEFAULT_MOTOR_TYPE" not in origins:
        origins["MCCONF_DEFAULT_MOTOR_TYPE"] = ["motor/mcconf_default.h", 25]
    # Match FIND_TARGET_C_CODE exactly, including the shared *_core.c preference.
    core_sources = sorted(header.parent.glob("hw_*_core.c"))
    sources = core_sources or [header.parent / ("hw_" + name.replace("_no_limits", "") + ".c")]
    missing = [p.relative_to(ROOT).as_posix() for p in sources if not p.exists()]
    return dict(name=name, family=header.relative_to(ROOT / "hwconf").parts[0],
                hw_name=macros.get("HW_NAME", "").strip('"'), kind=kind,
                header=relative, sources=[p.relative_to(ROOT).as_posix() for p in sources], missing_sources=missing,
                origins=origins, default=motor_type,
                no_limits="DISABLE_HW_LIMITS" in macros,
                shunts="3" if "HW_HAS_3_SHUNTS" in macros else "未声明 3 分流",
                phase_shunts="HW_HAS_PHASE_SHUNTS" in macros)


def generate():
    names = make_value("analysis_boards", "ALL_BOARD_NAMES")
    includes = make_value("analysis_includes", "INCDIR", "make/fw.mk")
    paths = [p for p in (ROOT / "hwconf").rglob("hw_*.h") if not p.name.endswith("core.h")]
    headers = {}
    for path in paths:
        name = path.name.replace("hw_", "").removesuffix(".h")
        if name in headers:
            raise RuntimeError(f"Duplicate Make target: {name}")
        headers[name] = path
    if set(names) != set(headers):
        raise RuntimeError(f"Make/Python target mismatch: {set(names) ^ set(headers)}")
    with ThreadPoolExecutor(max_workers=6) as executor:
        boards = list(executor.map(lambda n: inspect_board(n, headers, includes), names))
    counts = Counter(b["kind"] for b in boards)
    base = [b for b in boards if not b["name"].endswith("_no_limits")]
    commit_result = run(["git", "log", "-1", "--format=%h"])
    commit = commit_result.stdout.strip() if commit_result.returncode == 0 else "未读取"
    compiler = run([str(GCC), "--version"]).stdout.splitlines()[0]
    refs = [
        ("Make 自动枚举板卡", "Makefile", "RAW_TARGET_PATHS :=", "递归收集 hwconf 下头文件，排除 *core.h，再由 hw_*.h 文件名生成板卡目标；大小写保留。"),
        ("Make 选择硬件源码", "Makefile", "define FIND_TARGET_C_CODE", "优先使用同目录 hw_*_core.c，否则移除 _no_limits 后寻找同名 .c。"),
        ("板卡宏传入编译器", "Makefile", "$(1)_BUILD_MACROS =", "HW_HEADER/HW_SOURCE 决定本次构建采用的硬件配置。"),
        ("两套算法共同列入源码", "motor/motor.mk", "motor/mcpwm.c", "mcpwm.c 与 mcpwm_foc.c 同时列入 CSRC；不存在按板卡名称二选一的 Make 规则。"),
        ("全局默认电机类型", "motor/mcconf_default.h", "#define MCCONF_DEFAULT_MOTOR_TYPE", "默认 MOTOR_TYPE_FOC；板卡或用户配置可先行覆盖。"),
        ("配置载入默认电机类型", "confgenerator.c", "conf->motor_type = MCCONF_DEFAULT_MOTOR_TYPE", "默认宏写入 mc_configuration.motor_type。"),
        ("启动时读取已存配置", "conf_general.c", "void conf_general_read_mc_configuration", "先读取 EEPROM 配置并校验 CRC，失败时才加载默认配置。"),
        ("双电机启动强制 FOC", "motor/mc_interface.c", "m_motor_1.m_conf.motor_type = MOTOR_TYPE_FOC", "HW_HAS_DUAL_MOTORS 下两个电机的 motor_type 均被设为 FOC。"),
        ("按配置初始化算法", "motor/mc_interface.c", "// Initialize selected implementation", "BLDC/DC 调用 mcpwm_init；FOC 调用 mcpwm_foc_init。"),
        ("写入配置时的模式约束", "motor/mc_interface.c", "#if defined HW_HAS_DUAL_PARALLEL", "在 mc_interface_set_configuration 中：双桥并联强制 FOC；双电机且无单电机切换钩子时强制 FOC。"),
        ("通信配置入口的约束", "comm/commands.c", "#if defined(HW_HAS_DUAL_MOTORS) & !defined(HW_SET_SINGLE_MOTOR)", "VESC Tool 配置下发路径同样将普通双电机目标限制为 FOC。"),
        ("六步换相实现", "motor/mcpwm.c", "static void commutate(int steps) {", "comm_step 在 1…6 循环，并更新换相状态；可按传感器模式使用反电动势或霍尔换相。"),
        ("FOC 电流变换", "motor/mcpwm_foc.c", "// Full Clarke Transform", "三相电流经 Clarke 变换，再经 Park 变换进入 d/q 轴控制。"),
        ("FOC 调制输出", "motor/mcpwm_foc.c", "// Inverse Park transform", "d/q 轴电流调节后，经逆 Park 变换与 foc_svm 输出三相 PWM。"),
    ]
    ref_html = "".join(f'<tr><td>{escape(title)}</td><td>{source_link(path, line=line_of(path, key))}</td><td>{escape(note)}</td></tr>'
                       for title, path, key, note in refs)
    labels = dict(selectable="BLDC 六步 / FOC 可选", dual="FOC 限定 · 双电机", parallel="FOC 限定 · 双桥并联")
    rows = []
    for board in boards:
        kind = board["kind"]
        if kind == "selectable":
            reason = "未定义双电机/双桥并联限制宏；motor_type 可选择 BLDC 或 FOC。"
        else:
            key = "HW_HAS_DUAL_PARALLEL" if kind == "parallel" else "HW_HAS_DUAL_MOTORS"
            origin, lineno = board["origins"][key]
            reason = f'<code>{key}</code><br>{source_link(origin, line=lineno)}'
        if board["name"].startswith("a50s_dual"):
            reason += "<br>名称带 dual，但此目标未定义 HW_HAS_DUAL_MOTORS。"
        if board["name"] == "example":
            reason += "<br>硬件配置模板；不代表已验证的成品板卡。"
        if board["name"] == "axiom":
            reason += "<br>具有专用相电压缩放和 FPGA 硬件；六步实际适配需另行验证。"
        default_path, default_line = board["origins"]["MCCONF_DEFAULT_MOTOR_TYPE"]
        hw_sources = "<br>".join((f'<span class="missing">缺失：<code>{escape(s)}</code></span>'
                                  if s in board["missing_sources"] else source_link(s)) for s in board["sources"])
        if board["missing_sources"]:
            reason += "<br><strong class=\"missing\">Make 硬件源码映射缺失，不能按当前规则直接构建。</strong>"
        rows.append(f'''<tr data-kind="{kind}" data-family="{escape(board['family'])}">
<td><code>fw_{escape(board['name'])}</code><small>{escape(board['hw_name'])}</small></td>
<td>{escape(board['family'])}</td><td><span class="tag {kind}">{labels[kind]}</span></td>
<td>{'可选（代码路径）' if kind == 'selectable' else '受固件约束'}</td><td>支持；默认 FOC</td>
<td>{source_link(default_path, 'FOC 默认值来源', default_line)}<small>{escape(board['default'])}</small></td>
<td>{source_link(board['header'])}<small>硬件实现</small>{hw_sources}</td><td>{reason}</td></tr>''')
    family_rows = []
    for family in sorted({b["family"] for b in boards}):
        group = [b for b in boards if b["family"] == family]
        tally = Counter(b["kind"] for b in group)
        family_rows.append(f'<tr><td>{escape(family)}</td><td>{len(group)}</td><td>{tally["selectable"]}</td><td>{tally["dual"]}</td><td>{tally["parallel"]}</td></tr>')
    restricted = []
    for kind, title in (("dual", "双电机 · FOC 限定"), ("parallel", "双桥并联 · FOC 限定")):
        group = [b for b in boards if b["kind"] == kind]
        chips = "".join(f'<code>fw_{escape(b["name"])}</code>' for b in group)
        restricted.append(f'<article class="panel"><h3>{title} <span class="count">{len(group)}</span></h3><div class="chips">{chips}</div></article>')
    broken = [b for b in boards if b["missing_sources"]]
    broken_rows = "".join(f'<tr><td><code>fw_{escape(b["name"])}</code></td><td>{source_link(b["header"])}</td><td><code>{escape(b["missing_sources"][0])}</code></td></tr>' for b in broken)
    payload = json.dumps(dict(boards=boards, counts=counts, commit=commit), ensure_ascii=False).replace("</", "<\\/")
    html = TEMPLATE
    replacements = dict(TOTAL=str(len(boards)), SELECTABLE=str(counts["selectable"]),
                        DUAL=str(counts["dual"]), PARALLEL=str(counts["parallel"]),
                        RESTRICTED=str(counts["dual"] + counts["parallel"]), BASE=str(len(base)),
                        VARIANTS=str(len(boards) - len(base)), COMMIT=escape(commit),
                        COMPILER=escape(compiler), ROWS="\n".join(rows), REFERENCES=ref_html,
                        FAMILIES="".join(family_rows), RESTRICTED_LIST="".join(restricted),
                        FAMILY_OPTIONS="".join(f'<option>{escape(f)}</option>' for f in sorted({b['family'] for b in boards})),
                        BROKEN=str(len(broken)), BROKEN_ROWS=broken_rows, DATA=payload)
    for key, value in replacements.items():
        html = html.replace("@@" + key + "@@", value)
    if re.search(r"@@[A-Z_]+@@", html):
        raise RuntimeError("Unresolved template placeholder")
    OUTPUT.write_text(html, encoding="utf-8")
    print(json.dumps(dict(output=str(OUTPUT), total=len(boards), counts=counts,
                          base_targets=len(base), no_limits_variants=len(boards)-len(base),
                          default_types=Counter(b["default"] for b in boards),
                          restricted=[b["name"] for b in boards if b["kind"] != "selectable"],
                          missing_source_targets=[b["name"] for b in broken], compiler=compiler), ensure_ascii=False, indent=2))


TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="基于当前 VESC bldc 工程 Make 目标与 ARM GCC 预处理结果的 BLDC 六步/FOC 板卡分类分析。">
<title>Make 板卡 · BLDC 六步与 FOC 控制分析</title>
<style>
:root{--paper:#f5f2eb;--ink:#23312e;--muted:#5e6e66;--line:#d9ded2;--green:#1d6457;--blue:#285f8d;--amber:#996521;--card:#fffdf7}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.75 "Segoe UI","Microsoft YaHei",sans-serif}a{color:var(--blue);text-underline-offset:3px}a:hover{color:var(--green)}
.wrap{max-width:1580px;margin:auto;padding:42px 40px 72px}.eyebrow{color:var(--green);font-size:13px;font-weight:700;letter-spacing:2px}h1,h2,h3{font-family:Georgia,"Noto Serif SC","SimSun",serif;line-height:1.35}h1{font-size:clamp(29px,3.5vw,48px);margin:12px 0 18px;letter-spacing:-1px}h2{font-size:27px;margin:0 0 18px}h3{font-size:20px;margin:0 0 14px}p{margin:10px 0 16px}.lead{max-width:1120px;font-size:18px}.meta{color:var(--muted);font-size:13px}.meta span{margin-right:22px}.nav{display:flex;flex-wrap:wrap;gap:12px 24px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:14px 0;margin:26px 0}.nav a{text-decoration:none;font-size:14px}
.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin:26px 0}.stat,.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:23px}.stat strong{font:700 40px/1.15 Georgia,serif;display:block;color:var(--green);margin:7px 0}.stat small{display:block;color:var(--muted);font-size:12px}.stat .label{font-size:14px}.section{margin-top:42px}.callout{border-left:4px solid var(--green);padding:16px 22px;background:#e9eee4;border-radius:0 8px 8px 0}.callout p:last-child{margin-bottom:0}.grid2{display:grid;grid-template-columns:1fr 1fr;gap:20px}.count{font:600 14px "Segoe UI",sans-serif;background:#e5e9df;border-radius:20px;padding:4px 10px;vertical-align:middle}.chips{display:flex;flex-wrap:wrap;gap:9px}.chips code{background:#eef1e9;border:1px solid var(--line);border-radius:5px;padding:3px 8px;font-size:12px}
code,pre{font-family:Consolas,"Cascadia Code",monospace}code{font-size:.91em;overflow-wrap:anywhere}pre{font-size:13px;line-height:1.65;background:#263c35;color:#f3f3e9;border-radius:9px;padding:19px 22px;white-space:pre-wrap;overflow-wrap:anywhere}small{font-size:12px;color:var(--muted)}.note{color:var(--muted);font-size:14px}.flow{display:flex;align-items:stretch;gap:12px;margin:18px 0}.flow .node{flex:1;background:var(--card);border:1px solid var(--line);border-top:3px solid var(--green);border-radius:8px;padding:17px;font-size:14px}.flow .arrow{display:flex;align-items:center;font-size:25px;color:var(--muted)}.flow strong{display:block;margin-bottom:9px}.flow .node small{display:block}
.tablebox{overflow:auto;border:1px solid var(--line);border-radius:10px;background:var(--card)}table{border-collapse:collapse;width:100%;font-size:14px}th,td{text-align:left;border-bottom:1px solid var(--line);padding:13px 15px;vertical-align:top}th{background:#e9eee4;font-size:13px;white-space:nowrap}tbody tr:last-child td{border-bottom:0}tbody tr:hover{background:#f2f5ed}td small{display:block;margin-top:5px}td a{overflow-wrap:anywhere}.inventory{min-width:1480px;font-size:12px}.inventory td:first-child{min-width:232px}.inventory td:nth-child(7){min-width:250px;max-width:320px}.inventory td:last-child{min-width:245px;max-width:310px}.inventory th{position:sticky;top:0;z-index:1}.inventory td{padding:14px 12px}.tag{display:inline-block;border-radius:5px;padding:3px 7px;font-size:11px;font-weight:600;white-space:nowrap}.tag.selectable{color:var(--green);background:#e3eee7}.tag.dual{color:var(--blue);background:#e7eef5}.tag.parallel{color:var(--amber);background:#f6ecd9}.controls{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end;margin:18px 0}.controls label{font-size:12px;font-weight:600;display:block;color:var(--muted)}input,select,button{font:14px "Segoe UI","Microsoft YaHei",sans-serif;background:var(--card);color:var(--ink);border:1px solid #b5c3b9;border-radius:6px;padding:10px 12px;min-height:42px}input{width:330px;max-width:100%}button{cursor:pointer;background:var(--green);color:white;border-color:var(--green)}button:hover{background:#154d43}button.secondary{color:var(--green);background:transparent}.result{margin-left:auto;align-self:center;font-size:13px;color:var(--muted)}.empty{text-align:center;padding:30px;color:var(--muted)}.footer{margin-top:45px;border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:12px}ol,ul{padding-left:24px}li{margin:8px 0}#all .tablebox{max-height:780px}.legend{display:flex;gap:16px;flex-wrap:wrap;margin:14px 0}
@media(max-width:850px){.wrap{padding:26px 18px 48px}.stats{grid-template-columns:1fr 1fr}.grid2{grid-template-columns:1fr}.flow{flex-direction:column}.flow .arrow{justify-content:center;transform:rotate(90deg);height:26px}.controls>*{width:100%}.controls input,.controls select{width:100%}.result{margin:0}.stat{padding:17px}.stat strong{font-size:34px}}
@media print{body{background:white;font-size:11px}.wrap{padding:0;max-width:none}h1{font-size:28px}h2{font-size:20px}.nav,.controls,button{display:none}.section{margin-top:24px}.stats{gap:8px}.stat,.panel{padding:12px;break-inside:avoid}.stat strong{font-size:28px}.tablebox,#all .tablebox{overflow:visible;max-height:none;border:0}.inventory{min-width:0;font-size:8px;table-layout:fixed}.inventory td,.inventory th{padding:5px 3px;min-width:0!important;max-width:none!important;white-space:normal;position:static}.tag{font-size:8px;white-space:normal}th{display:table-cell}thead{display:table-header-group}tr{break-inside:avoid}a{color:inherit;text-decoration:none}.flow .node{padding:10px}pre{color:#222;background:#eee}small{font-size:8px}@page{size:A3 landscape;margin:14mm}}
.missing{color:#9b432e}tr[hidden]{display:none}
</style>
</head>
<body><main class="wrap">
<header><div class="eyebrow">FIRMWARE / HARDWARE / CONTROL MODES</div>
<h1>Make 板卡的 BLDC 六步与 FOC 控制分析</h1>
<p class="lead">当前工程共枚举 <strong>@@TOTAL@@ 个固件目标</strong>，全部默认使用 <strong>FOC</strong>。其中 <strong>@@SELECTABLE@@ 个目标</strong>保留 BLDC 六步与 FOC 的模式选择路径，<strong>@@RESTRICTED@@ 个目标</strong>因双电机或双桥并联配置被固件限制为 FOC。未发现默认使用六步控制或仅支持六步控制的目标。</p>
<div class="meta"><span>分析日期：2026-11-17 · Asia/Shanghai</span><span>工程：bldc / VESC 7.00</span><span>提交：@@COMMIT@@</span><span>方法：Make 枚举 + ARM GCC 逐目标预处理 + 控制代码追踪</span></div>
</header>
<nav class="nav" aria-label="文档导航"><a href="#conclusion">分类结论</a><a href="#build">Make 与算法关系</a><a href="#restricted">FOC 限定清单</a><a href="#all">全部板卡查询</a><a href="#evidence">源码依据</a><a href="#usage">使用建议与边界</a></nav>
<div class="stats"><div class="stat"><span class="label">Make 固件目标</span><strong>@@TOTAL@@</strong><small>@@BASE@@ 个非 _no_limits 目标 + @@VARIANTS@@ 个变体</small></div><div class="stat"><span class="label">BLDC 六步 / FOC 可选</span><strong>@@SELECTABLE@@</strong><small>代码路径开放，全部默认 FOC</small></div><div class="stat"><span class="label">双电机 · FOC 限定</span><strong>@@DUAL@@</strong><small>HW_HAS_DUAL_MOTORS</small></div><div class="stat"><span class="label">双桥并联 · FOC 限定</span><strong>@@PARALLEL@@</strong><small>HW_HAS_DUAL_PARALLEL</small></div></div>
<section class="section" id="conclusion"><h2>01 / 应如何分类</h2>
<div class="callout"><p><strong>板卡名称不能直接决定控制算法。</strong>同一套三相逆变硬件通常可承载六步控制和 FOC；本工程中，Make 选择的是硬件引脚、采样和驱动配置，最终算法由 <code>mc_configuration.motor_type</code> 与硬件限制宏共同决定。名称带 <code>FOC</code> 不等于只能 FOC，名称带 <code>dual</code> 也不等于固件启用了双电机。</p></div>
<div class="tablebox" style="margin-top:20px"><table><thead><tr><th>分类</th><th>数量</th><th>BLDC 六步</th><th>FOC</th><th>判断依据</th></tr></thead><tbody>
<tr><td>默认六步 / 六步专用</td><td>0</td><td>未发现此类目标</td><td>—</td><td>所有目标的有效默认值均为 <code>MOTOR_TYPE_FOC</code> 或数值 <code>2</code>。</td></tr>
<tr><td>BLDC 六步 / FOC 可选</td><td>@@SELECTABLE@@</td><td>软件入口可选</td><td>默认使用</td><td>没有触发双电机/并联模式强制 FOC 的宏；可由 <code>motor_type</code> 进入 <code>mcpwm</code> 或 <code>mcpwm_foc</code>。</td></tr>
<tr><td>双电机 FOC 限定</td><td>@@DUAL@@</td><td>正常配置入口受限</td><td>强制使用</td><td>定义 <code>HW_HAS_DUAL_MOTORS</code>，且本次扫描未发现任何目标定义 <code>HW_SET_SINGLE_MOTOR</code> 例外钩子。</td></tr>
<tr><td>双桥并联 FOC 限定</td><td>@@PARALLEL@@</td><td>正常配置入口受限</td><td>强制使用</td><td>定义 <code>HW_HAS_DUAL_PARALLEL</code>，配置更新时将 <code>motor_type</code> 改为 FOC。</td></tr>
</tbody></table></div>
<p class="note">本报告中的“可选”描述当前代码路径，不代表每块商品板都已完成六步实机验证。统计单位为 Make 枚举的固件目标，包含电压、硬件版本、分流电阻及 <code>_no_limits</code> 变体，不能直接当作独立 PCB 数量。其中 @@BROKEN@@ 个目标存在硬件源码映射缺失，下面单独列出；控制模式统计仍按其有效头文件配置计算。</p>
</section>
<section class="section" id="build"><h2>02 / Make 构建如何关联到控制算法</h2>
<div class="flow" role="img" aria-label="板卡头文件经 Make 选择后加载配置，再按 motor_type 分派控制算法">
<div class="node"><strong>① 板卡目标</strong><code>make fw_410</code><small>递归扫描 hwconf/hw_*.h<br>排除 *core.h；保留目标大小写</small></div><span class="arrow" aria-hidden="true">→</span>
<div class="node"><strong>② 硬件配置</strong><code>HW_HEADER / HW_SOURCE</code><small>选择板卡头文件与硬件实现<br>包装头文件可能继承 core.h</small></div><span class="arrow" aria-hidden="true">→</span>
<div class="node"><strong>③ 电机配置</strong><code>motor_type</code><small>优先读取 EEPROM 已存配置<br>无效时才采用编译期默认值</small></div><span class="arrow" aria-hidden="true">→</span>
<div class="node"><strong>④ 算法分派</strong><code>BLDC → mcpwm</code><br><code>FOC → mcpwm_foc</code><small>双电机/并联宏对模式额外约束</small></div></div>
<div class="grid2"><article class="panel"><h3>BLDC 六步控制的实现</h3><p><code>motor/mcpwm.c</code> 用 <code>comm_step</code> 表示 1～6 个换相状态，通过 <code>set_next_comm_step()</code> 设置各相输出。无感模式根据相电压/反电动势推进换相，有感模式使用霍尔表。电流环、速度环等外层控制不会将六步换相自动变成 FOC。</p></article>
<article class="panel"><h3>FOC 控制的实现</h3><p><code>motor/mcpwm_foc.c</code> 对采样电流执行 Clarke / Park 变换，在转子 d/q 坐标系调节电流，再经逆 Park 变换和 <code>foc_svm()</code> 输出 PWM。霍尔、编码器和无感观测器属于角度来源选择；使用霍尔并不意味着采用六步控制。</p></article></div>
<pre>motor/motor.mk 同时列入：motor/mcpwm.c + motor/mcpwm_foc.c

MOTOR_TYPE_BLDC = 0 → mcpwm_init(...)     → 六步换相
MOTOR_TYPE_DC   = 1 → mcpwm_init(...)     → 有刷 DC（不计入六步分类）
MOTOR_TYPE_FOC  = 2 → mcpwm_foc_init(...) → 磁场定向控制

全局默认：MCCONF_DEFAULT_MOTOR_TYPE = MOTOR_TYPE_FOC
Luna 部分配置写作数值 2，其含义同样是 FOC。</pre>
<p class="note">顶层 Makefile 直接提供 <code>fw_&lt;board&gt;</code> 和 <code>&lt;board&gt;</code> 目标。其 help 中还提到 <code>fw</code> / <code>PROJECT=... fw</code>，但当前顶层文件没有定义通用 <code>fw</code> 规则；本文示例使用实际生成的明确板卡目标。</p>
<article class="panel"><h3>发现 @@BROKEN@@ 个 Make 硬件源码映射缺失</h3><p>这些变体头文件会被 Make 自动枚举，但所在目录没有 <code>hw_*_core.c</code>，按当前 <code>FIND_TARGET_C_CODE</code> 规则推导的同名 <code>.c</code> 也不存在。头文件能够正常预处理，并不等于该 Make 目标能够构建；下表均属于“六步 / FOC 可选”的头文件配置。本报告仅标注现状，未修改构建规则。</p>
<div class="tablebox"><table><thead><tr><th>目标</th><th>存在的头文件</th><th>Make 推导但不存在的源码</th></tr></thead><tbody>@@BROKEN_ROWS@@</tbody></table></div></article>
</section>
<section class="section" id="restricted"><h2>03 / 哪些目标被限定为 FOC</h2><p>以下是全部 FOC 限定目标，包含 <code>_no_limits</code> 变体。其余目标在下一节标为“BLDC 六步 / FOC 可选”。</p>
<div class="grid2">@@RESTRICTED_LIST@@</div>
<p class="note">双桥并联是两组三相功率桥共同驱动一台电机。当前代码在配置更新时强制 FOC；它与双电机模式的启动强制逻辑并不完全相同，因此本报告不将二者的初始化约束混写为同一条。</p>
<article class="panel" style="margin-top:20px"><h3>几个容易误判的目标</h3><ul>
<li><code>40 / 410 / 45 / 46 / 48 / 49</code> 等 VESC4 系列：仍然全部默认 FOC，同时保留六步模式入口，不能归为“仅六步”。</li>
<li><code>60 / 60_mk* / 75_300 / 100_250</code> 以及 <code>Cheap_FOCer / Little_FOCer / go_foc_*</code>：按本次预处理结果属于模式可选类；名称中的 FOC 不能作为“FOC 专用”证据。</li>
<li><code>a50s_dual_8s / a50s_dual_12s</code>：其 <code>hw_a50s_dual_core.h</code> 没有定义 <code>HW_HAS_DUAL_MOTORS</code>，本工程每个目标按一路控制配置编译，仍归为模式可选类。</li>
<li><code>ubox_v*</code>、<code>go_foc_dv6_pro</code>：本次目标同样没有定义上述双电机限制宏；商品是否含两块 MCU 或两路功率级，需结合硬件原理图判断。</li>
<li><code>_no_limits</code> 变体表示硬件配置限值处理发生变化；实际宏状态可在内嵌审计数据中查看。它不会自动切换六步或 FOC。</li>
</ul></article></section>
<section class="section" id="all"><h2>04 / 全部 Make 板卡目标查询</h2>
<p>可搜索目标名、<code>HW_NAME</code>、厂商目录或硬件文件名。源码链接均为相对路径，文档随工程移动后仍可使用。表格横向滚动可查看判断依据。</p>
<div class="controls"><div><label for="query">关键词</label><input id="query" placeholder="例如：410、stormcore、FOC、ubox" type="search"></div><div><label for="kind">控制分类</label><select id="kind"><option value="all">全部分类</option><option value="selectable">BLDC 六步 / FOC 可选</option><option value="dual">FOC 限定 · 双电机</option><option value="parallel">FOC 限定 · 双桥并联</option></select></div><div><label for="family">板卡目录</label><select id="family"><option value="all">全部目录</option>@@FAMILY_OPTIONS@@</select></div><button id="reset" class="secondary" type="button">重置筛选</button><button id="print" type="button">打印 / 保存 PDF</button><span class="result" id="result" role="status" aria-live="polite">显示 @@TOTAL@@ / @@TOTAL@@ 个目标</span></div>
<div class="tablebox"><table class="inventory"><thead><tr><th>Make 目标 / HW_NAME</th><th>板卡目录</th><th>控制分类</th><th>BLDC 六步</th><th>FOC</th><th>默认模式依据</th><th>硬件配置与实现</th><th>判断依据 / 备注</th></tr></thead><tbody id="board-rows">@@ROWS@@</tbody></table><div id="empty" class="empty" hidden>没有匹配的目标，请调整关键词或筛选条件。</div></div>
<p class="note">关闭 JavaScript 仍可阅读全部表格。筛选与打印只使用浏览器本地功能，无网络依赖。打印遵循当前筛选结果；若需完整清单，请先重置筛选。</p>
<details style="margin-top:20px"><summary>按板卡目录统计</summary><div class="tablebox" style="margin-top:12px"><table><thead><tr><th>目录</th><th>全部目标</th><th>六步 / FOC 可选</th><th>双电机 FOC 限定</th><th>双桥并联 FOC 限定</th></tr></thead><tbody>@@FAMILIES@@</tbody></table></div></details>
</section>
<section class="section" id="evidence"><h2>05 / 源码证据与复核方法</h2><div class="tablebox"><table><thead><tr><th>检查点</th><th>文件与行号</th><th>代码含义</th></tr></thead><tbody>@@REFERENCES@@</tbody></table></div>
<p class="note">显示的行号对应本次工作区快照；普通浏览器点击后打开源码文件，行号供编辑器定位。</p>
<ol><li>实际运行 GNU Make 读取 <code>ALL_BOARD_NAMES</code>，共得到 @@TOTAL@@ 个目标，并与递归头文件扫描结果逐一核对。</li>
<li>从 <code>make/fw.mk</code> 读取真实 <code>INCDIR</code>；对每个目标执行 ARM GCC 的 <code>-E -dM</code>，以 <code>conf_general.h</code> 为入口，传入对应 <code>HW_HEADER</code>，保留真实条件编译与头文件继承关系。</li>
<li>检查预处理后有效的 <code>HW_HAS_DUAL_MOTORS</code>、<code>HW_HAS_DUAL_PARALLEL</code>、<code>HW_SET_SINGLE_MOTOR</code> 以及 <code>MCCONF_DEFAULT_MOTOR_TYPE</code>；用 <code>-H</code> 的真实头文件加载链追踪宏来源，并检查 Make 推导的硬件 .c 文件是否存在，发现 @@BROKEN@@ 个目标映射缺失。</li>
<li>结合 <code>mc_interface.c</code> 的初始化/配置切换与 <code>comm/commands.c</code> 的配置写入流程，判断模式是否受固件约束。全部目标的配置预处理成功；未执行全量固件编译或实机测试。</li></ol>
<p class="note">工具链：@@COMPILER@@。分析按工程默认编译配置进行；未额外传入 USER_MC_CONF、替代 HW_SOURCE 或其他自定义控制宏。可重新运行 <a href="generate_board_control_report.py">报告生成脚本</a> 复核清单；日期为本次分析日期。</p>
</section>
<section class="section" id="usage"><h2>06 / 如何选用与理解这份清单</h2>
<div class="grid2"><article class="panel"><h3>需要 BLDC 六步控制</h3><p>先在全表中筛选“BLDC 六步 / FOC 可选”，再按实际硬件选用对应固件。构建目标只选择板卡，不会把默认 FOC 改成六步；还需要在电机配置中选择 BLDC，并配置对应的无感或霍尔换相参数。</p><pre>cd bldc
make fw_410
# 固件默认仍是 FOC。
# 通过电机配置选择 MOTOR_TYPE_BLDC 后，进入六步路径。</pre></article>
<article class="panel"><h3>需要 FOC 控制</h3><p>本次全部目标默认 FOC。目标选择必须与板卡版本一致，并设置电机参数及所需位置反馈方式。双电机与双桥并联类按当前工程的 FOC 配置使用。</p><pre>cd bldc
make fw_stormcore_60d
make fw_stormcore_100d_parallel
# 目标名称区分硬件；不是两个独立算法工程。</pre></article></div>
<div class="callout" style="margin-top:22px"><p><strong>结论的适用范围：</strong>本报告回答当前源码中“哪些 Make 目标默认什么模式、哪些目标存在模式限制、哪些目标保留六步代码入口”。对可选类的具体商品板，尤其是 <code>axiom</code> 等专用采样/功率硬件，实际六步适配还需检查原理图、相电压量程、PWM 接法和换相测试。<code>example</code> 仅是硬件模板。已经存入 EEPROM 的电机模式也不能从 Make 目标名推断。</p></div>
</section>
<footer class="footer">来源：当前工作区的 bldc/Makefile、make/fw.mk、hwconf、motor 与配置通信代码。报告包含完整静态目标清单及内嵌预处理审计摘要；无需联网。</footer>
</main>
<script id="analysis-data" type="application/json">@@DATA@@</script>
<script>
(() => {
  const query=document.getElementById('query'), kind=document.getElementById('kind'), family=document.getElementById('family');
  const rows=[...document.querySelectorAll('#board-rows tr')];
  const index=rows.map(row=>({row,text:row.textContent.toLocaleLowerCase()}));
  function filter(){
    const terms=query.value.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean);let shown=0;
    for(const {row,text} of index){const visible=terms.every(t=>text.includes(t))&&(kind.value==='all'||row.dataset.kind===kind.value)&&(family.value==='all'||row.dataset.family===family.value);row.hidden=!visible;if(visible)shown++;}
    document.getElementById('result').textContent=`显示 ${shown} / ${rows.length} 个目标`;
    document.getElementById('empty').hidden=shown!==0;
  }
  query.addEventListener('input',filter);kind.addEventListener('change',filter);family.addEventListener('change',filter);
  document.getElementById('reset').addEventListener('click',()=>{query.value='';kind.value='all';family.value='all';filter();});
  document.getElementById('print').addEventListener('click',()=>window.print());
  filter();
})();
</script>
</body></html>
'''


if __name__ == "__main__":
    generate()
