/*
 * 校验 Live2D 播放器页面的「点击 / 拖动」判定。
 *
 * 背景（2026-10-03 修的一个真实缺陷）：
 *   原先页面用 DOM 的 `click` 事件触发表情轮换。但 click 只要「按下与抬起落在
 *   同一元素」就派发，**与位移无关**；而拖动桌宠时 Qt 会同步 move() 窗口，指针
 *   相对窗口的位置几乎不变，抬起时照样派发 click —— 表现就是「一拖就换表情」。
 *
 * 修法：不再用 click，改为自己按**屏幕坐标**的位移判定，且用「手势过程中的
 * 最大位移」而不是「起止两点」（拖出去再拖回原点时起止位移是 0，会被误判）。
 *
 * 本脚本从 index.html 里**抽取真实源码**求值，不复制逻辑 —— 复制就等于没测。
 *
 * 跑法：
 *   node tools/probe_live2d_gesture.js
 */
"use strict";

const fs = require("fs");
const path = require("path");

const PAGE = path.join(__dirname, "..", "assets", "live2d", "viewer", "index.html");
const html = fs.readFileSync(PAGE, "utf8");

let failed = 0;
function check(label, actual, expected) {
  const ok = actual === expected;
  if (!ok) { failed += 1; }
  console.log(`${ok ? "  ok  " : " FAIL "} ${label}  (得到 ${actual}，期望 ${expected})`);
}

function extract(pattern, what) {
  const m = html.match(pattern);
  if (!m) {
    console.error(`找不到${what} —— 页面结构变了，请同步更新本脚本`);
    process.exit(1);
  }
  return m[0];
}

/* ── 1. 取出页面里真实的常量与函数 ── */
const slop = Number(extract(/var\s+CLICK_SLOP\s*=\s*(\d+)\s*;/, " CLICK_SLOP").match(/\d+/)[0]);
const fnSrc = extract(/function\s+isClickGesture\s*\([\s\S]*?\n\s*\}/, " isClickGesture");
const manhattanSrc = extract(/function\s+manhattan\s*\([\s\S]*?\n\s*\}/, " manhattan");
const mouseUpSrc = extract(
  /addEventListener\(\s*"mouseup"\s*,\s*function\s*\(e\)\s*\{[\s\S]*?\n\s*\}\);/,
  " mouseup 处理函数");

const { isClickGesture, manhattan } = new Function(
  `var CLICK_SLOP = ${slop};\n${fnSrc}\n${manhattanSrc}\n
   return { isClickGesture: isClickGesture, manhattan: manhattan };`)();

console.log(`页面 CLICK_SLOP = ${slop}`);

/* ── 2. 阈值语义 ── */
console.log("── 阈值语义 ──");
check("位移 0 → 算点击", isClickGesture(0), true);
check("位移 1 → 算点击", isClickGesture(1), true);
check(`位移 ${slop}（恰好到阈值）→ 算点击`, isClickGesture(slop), true);
check(`位移 ${slop + 1}（超阈值）→ 不算点击`, isClickGesture(slop + 1), false);
check("位移 100 → 不算点击", isClickGesture(100), false);

console.log("── 曼哈顿度量 ──");
check("横向 3px", manhattan(0, 0, 3, 0), 3);
check("斜向各 2px → 4（不是欧氏距离 2.83）", manhattan(0, 0, 2, 2), 4);

/* ── 3. 跑页面里真实的 mouseup 处理函数 ──
   模拟时把「手势过程中的最大位移」直接算好塞进 pressAt.max，
   等价于页面在 mousemove 里累计的结果。 */
console.log("── mouseup 处理函数（真实源码）──");

const modelStub = { getBounds: () => ({ x: 0, y: 0, width: 1000, height: 1000 }) };
const expressions = ["a", "b"];

function dispatch(pressAt, ev) {
  let poked = 0;
  const run = new Function(
    "pressAt", "model", "clickExpressions", "isClickGesture", "manhattan", "poke", "e",
    mouseUpSrc.replace(/^[\s\S]*?function\s*\(e\)\s*\{/, "").replace(/\n\s*\}\);\s*$/, ""));
  run(pressAt, modelStub, expressions, isClickGesture, manhattan,
      () => { poked += 1; }, ev);
  return poked;
}

const inside = { clientX: 100, clientY: 100 };

// 原地点击：0 位移 → 应轮换
check("原地按下抬起 → 触发轮换",
      dispatch({ x: 500, y: 400, max: 0 },
               Object.assign({ button: 0, screenX: 500, screenY: 400 }, inside)), 1);

// 拖动：过程中位移很大 → 不该轮换
check("拖动 200px → 不触发轮换",
      dispatch({ x: 500, y: 400, max: 200 },
               Object.assign({ button: 0, screenX: 700, screenY: 400 }, inside)), 0);

// 关键回归：拖出去再拖回原点。起止位移是 0，只有靠 max 才能判出来
check("拖出去再拖回原点 → 不触发轮换",
      dispatch({ x: 500, y: 400, max: 300 },
               Object.assign({ button: 0, screenX: 500, screenY: 400 }, inside)), 0);

// 轻微抖动仍在容差内 → 应轮换
check("抖动 2px → 触发轮换",
      dispatch({ x: 500, y: 400, max: 2 },
               Object.assign({ button: 0, screenX: 502, screenY: 400 }, inside)), 1);

// 右键不参与
check("右键抬起 → 不触发轮换",
      dispatch({ x: 500, y: 400, max: 0 },
               Object.assign({ button: 2, screenX: 500, screenY: 400 }, inside)), 0);

// 没有按下记录（例如在窗口外按下、移进来才松开）
check("没有按下记录 → 不触发轮换",
      dispatch(null, Object.assign({ button: 0, screenX: 500, screenY: 400 }, inside)), 0);

// 点在模型包围盒外
check("点在模型外 → 不触发轮换",
      dispatch({ x: 500, y: 400, max: 0 },
               { button: 0, screenX: 500, screenY: 400, clientX: 5000, clientY: 5000 }), 0);

/* ── 4. 回归守卫：不许退回 click 事件 ── */
console.log("── 回归守卫 ──");
const clickListeners = (html.match(/addEventListener\(\s*["']click["']/g) || []).length;
check("页面里没有 click 监听（它无法区分拖动与点击）", clickListeners, 0);
check("按下时记录 screenX", /addEventListener\(\s*"mousedown"[\s\S]{0,400}screenX/.test(html), true);
check("mouseup 里用 max 判定（而非只看起止两点）",
      /Math\.max\(\s*start\.max\s*,/.test(mouseUpSrc), true);

console.log(failed === 0 ? "\n全部通过。" : `\n${failed} 项失败。`);
process.exit(failed === 0 ? 0 : 1);
