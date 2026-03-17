# Painted UX Migration Retrospective — 2026-03-17

## Scope of this slice

This retrospective covers the first real implementation slice of the painted UX migration:

- Stage 0: add `painted` as a real siftd dependency and create the bridge/seam
- Stage 1: migrate `query <id>` detail rendering onto painted
- upstream follow-on: fix and release a real `painted` ANSI rendering bug as `0.1.2`

## What shipped

### In siftd

- added `painted` as a package dependency
- introduced an internal zoom abstraction (`MINIMAL` / `SUMMARY` / `DETAILED` / `FULL`)
- added `src/siftd/output/painted_bridge.py`
- migrated `query <id>` detail rendering onto painted blocks
- preserved the separation between:
  - narrative/data normalization
  - human-readable terminal rendering
  - JSON output
- switched siftd to `painted>=0.1.2`
- removed the temporary local rendering workaround once the upstream fix was released

### In painted

- diagnosed an ANSI rendering bug in `print_block()`
- fixed trailing rectangular row padding being written in ANSI mode
- released and published `painted 0.1.2`

## What went well

1. **The staged migration plan held up.**
   Starting with `query <id>` was the right call. It gave us the richest static data with the fewest moving parts.

2. **The bridge layer was a good seam.**
   We were able to move rendering onto painted without pushing painted concerns into storage or API layers.

3. **The zoom model was immediately useful.**
   Mapping current siftd flags onto internal semantic zoom levels made the rendering intent clearer without requiring a public CLI redesign yet.

4. **We upstreamed the right bug instead of carrying it forever.**
   The spacing issue turned out to be a real painted bug, not a siftd-specific quirk. Fixing it upstream reduced long-term maintenance and kept the siftd bridge simpler.

## What was harder than expected

1. **Terminal behavior differed from captured/plain output.**
   The worst bug in this slice did not show up as a normal text diff.

2. **The bug sat exactly at the terminal boundary.**
   Painted blocks are rectangular. In ANSI mode, trailing right-padding spaces were being printed. When those spaces hit the terminal's last column, the terminal auto-wrapped before the explicit newline. The result looked like spurious blank lines.

3. **Upstreaming added release overhead.**
   Once we decided to fix the problem properly, the work became:
   - diagnose in siftd
   - patch painted
   - run painted checks
   - bump version
   - tag + release + publish
   - return to siftd and remove the workaround

4. **Dependency-source discipline matters.**
   We lost some time by inspecting a non-authoritative local checkout before switching back to the canonical painted repo and the actual PyPI-installed package.

## Root cause of the spacing bug

The issue was not extra blank lines in siftd's data.

It was this sequence:

1. painted composes rectangular blocks
2. shorter rows are padded with trailing spaces
3. ANSI `print_block()` wrote those padding spaces to the terminal
4. if a padded row reached the last terminal column, the terminal auto-wrapped
5. the following newline then appeared as an extra blank line

This is exactly the kind of bug that belongs in the rendering library, not in every application that uses it.

## Decisions validated by this slice

- `painted` should be a **real hard dependency** for siftd's human-readable UX
- JSON output should remain a completely separate product path
- semantic zoom should stay internal for now and map from existing siftd flags
- static/detail views should migrate before live/in-place or TUI work
- every migration stage needs at least one **real TTY review**, not just captured output tests

## Changes to carry into the next slice

### 1. Require real TTY validation early

For any future rendering stage, manual review should explicitly include:

- normal TTY output
- piped/plain output
- narrow terminal
- wide terminal
- default and full/detail modes

Captured output alone is not sufficient.

### 2. Keep upstream fixes upstream

If the bridge starts to accumulate terminal-behavior hacks, stop and check whether the bug actually belongs in painted.

### 3. Continue command-by-command migration

The next slice should still be:

1. `peek <id>`
2. `peek <id> --full`
3. only then `peek --follow`

## Recommended next implementation target

**Stage 2 — Peek detail migration**

Goals for the next session:

- project `peek <id>` through the same painted-backed rendering family as `query <id>`
- preserve current peek-specific data differences while aligning visual hierarchy
- reuse the semantic role mapping and zoom decisions already proven in query detail

## Short version

This was more work than a straight CLI refactor because it crossed an application/library boundary.

That said, it was high-value work:

- siftd now has a real painted seam
- `query <id>` is painted-backed
- the nastiest rendering bug was fixed at the right layer
- painted has a released fix (`0.1.2`)
- the next migration slice should be simpler because the delivery path is no longer suspect
