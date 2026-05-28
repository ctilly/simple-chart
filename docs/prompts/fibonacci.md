# Task: Build the "Fibonacci Retracement" drawing tool for SimpleChart

## Start here (do not skip)
You are adding a new drawing tool to the SimpleChart repo. Before doing anything else:
1. Read `AGENTS.md` in full.
2. Read `docs/skills/drawing-tool.md` in full.
This tool is INTERACTIVE (click-to-anchor, live mouse tracking, commit
click, drag, Configure dialog, persistent drawing). The drawing tool skill file
governs the entire process — its Phase Gates, Spec template, Non-Negotiable
Boundaries, and the Acceptance Bar are binding.

Follow the Phase Gates exactly and in order. Stop at every gate, explain what
was decided or written, and wait for my explicit approval before continuing.
Do not write any tool code before the boundary/file plan gate is approved.
Do not skip the research gate.

## Assume nothing — research first, then ask
I am not a Fibonacci Retracement expert and I want a shared understanding before
any code exists. Per the skill file's Research section: research the accepted
definition, the standard level set, labeling conventions, and the drawing-tool
interaction model from authoritative technical references, and cite your
sources. Then deliver the research summary the skill requires AND an explicit
list of every ambiguity or decision you need me to resolve. Assume nothing
about: which price/level the start anchors to, retracement direction, the exact
level values shown, how levels are labeled, what defines the "area of
possibility," or coordinate/timeframe handling. Surface each as a question.
Wait for my approval of the research summary before drafting the spec.

## What the tool must do (requirements)
- Add mode: a drawing tool the user invokes to start drawing on the chart.
- First click sets the START candle — the origin in BOTH price and time. A
Fibonacci scale appears to the right of that candle.
- As the mouse moves to the right, the scale expands in price and time and
updates live. The level percentages are drawn on their respective horizontal
lines and update dynamically as the mouse moves.
- Area of possibility: live drawing is only valid in the region to the right of
the start candle and within the relevant min/max price and time bounds. If the
mouse leaves this region, the in-progress drawing terminates. (Define these
bounds precisely during research/spec — flag the definition for my approval.)
- Second click on the END candle commits the drawing; the scale then persists
on the chart.
- Appearance is editable from the "Configure…" dialog (which levels are shown,
colors, line styles, label format, etc. — propose the configurable set in the
spec).

## Non-negotiable constraints
- QUALITY: extremely high quality. Fully typed (mypyc-compatible),
standard-library-first, no speculative abstractions, no dead code. Honor the
project rules.
- TOTAL ISOLATION: every part of this tool lives ONLY inside its own
package under `tools/` (e.g. `tools/fib_retracement/`). No knowledge
of Fibonacci may leak into `app/`, `chart/`, the runtime, the public API, or
any shared file. No `if extension_name == ...`, no series-key sniffing outside
the package, no Fibonacci-specific chart branch. Adding the SMALLEST GENERIC
chart primitive is permitted only under the skill file's stated rule. If you
believe any change outside the package is unavoidable, STOP and follow the
skill's boundary side-task process (name the boundary, justify why an
tool-only change is insufficient, list files, state behavioral risk,
define focused tests) and wait for approval.
- Register the tool and its store handler from inside the package. Import
extension-facing API only from `simplechart.api`.

## Deliverables, in order — pause for my approval at each
1. Clarifying questions on the requirements above.
2. Research summary + citations + list of ambiguities.
3. Tool spec using the skill file's spec template.
4. Boundary/file plan.
Then implement following the skill's Implementation Order and remaining gates.

## Done means
- Passes the skill file's "Fibonacci Retracement Acceptance Bar."
- Behavior works end to end: start click → live expansion with dynamic % labels
→ termination when the mouse leaves the area of possibility → commit click →
persistent scale → appearance configurable via "Configure…".
- Tests added per the skill's Verify step; `python -m pytest` and
`python -m compileall app chart data indicators simplechart tools tests` run clean.
