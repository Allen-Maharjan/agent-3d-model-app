---
name: Sculpt Workspace
description: A supervisor's console for agent-driven sculpture edits, marked like numbered blocks cut from one protected original.
colors:
  ground: "#e1e4e6"
  surface: "#eceef0"
  surface-sunk: "#d6d9dc"
  plate: "#3a3d40"
  ink: "#1f2427"
  ink-2: "#454c52"
  ink-3: "#565d63"
  rule: "#b4b9bd"
  rule-strong: "#81878c"
  mark: "#a3361d"
  mark-hover: "#8c2d17"
  mark-ink: "#962f18"
  on-mark: "#fbf9f7"
  blue: "#2c5375"
  blue-soft: "#d3dce3"
  fault: "#8a1c2c"
  fault-soft: "#f0dcdc"
typography:
  display:
    fontFamily: "Saira Stencil One, Barlow, sans-serif"
    fontSize: "2.1rem"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "0.02em"
  numeral:
    fontFamily: "Saira Stencil One, Barlow, sans-serif"
    fontSize: "1.25rem"
    fontWeight: 400
    lineHeight: 1
    letterSpacing: "0.02em"
  headline:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.75rem"
    fontWeight: 600
    lineHeight: 1.2
    letterSpacing: "-0.015em"
  title:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1.125rem"
    fontWeight: 600
    lineHeight: 1.2
  body:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "1rem"
    fontWeight: 400
    lineHeight: 1.5
    fontFeature: "\"tnum\""
  label:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.875rem"
    fontWeight: 500
    lineHeight: 1.5
  caption:
    fontFamily: "Barlow, ui-sans-serif, system-ui, sans-serif"
    fontSize: "0.75rem"
    fontWeight: 500
    lineHeight: 1.5
  mono:
    fontFamily: "ui-monospace, SF Mono, Cascadia Mono, Roboto Mono, Menlo, monospace"
    fontSize: "0.875rem"
    fontWeight: 400
    letterSpacing: "0"
rounded:
  none: "0px"
spacing:
  s-1: "4px"
  s-2: "8px"
  s-3: "12px"
  s-4: "16px"
  s-5: "24px"
  s-6: "32px"
  s-7: "48px"
components:
  button-primary:
    backgroundColor: "{colors.mark}"
    textColor: "{colors.on-mark}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 16px"
    height: "40px"
  button-primary-hover:
    backgroundColor: "{colors.mark-hover}"
    textColor: "{colors.on-mark}"
  button-secondary:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 16px"
    height: "40px"
  button-ghost:
    backgroundColor: "transparent"
    textColor: "{colors.ink-2}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 8px"
    height: "32px"
  button-ghost-hover:
    backgroundColor: "{colors.surface-sunk}"
    textColor: "{colors.ink}"
  select:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.label}"
    rounded: "{rounded.none}"
    padding: "0 12px"
    height: "40px"
  revision-block:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    typography: "{typography.numeral}"
    rounded: "{rounded.none}"
    padding: "0 8px"
    height: "36px"
    width: "44px"
  revision-block-current:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
  revision-block-lg:
    typography: "{typography.display}"
    height: "56px"
    width: "72px"
  panel:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "12px"
  render-plate:
    backgroundColor: "{colors.plate}"
    rounded: "{rounded.none}"
  badge-current:
    backgroundColor: "{colors.blue-soft}"
    textColor: "{colors.blue}"
    typography: "{typography.caption}"
    padding: "1px 6px"
  problem-callout:
    backgroundColor: "{colors.fault-soft}"
    textColor: "{colors.ink}"
    rounded: "{rounded.none}"
    padding: "12px 16px"
  toast:
    backgroundColor: "{colors.ink}"
    textColor: "{colors.ground}"
    typography: "{typography.label}"
    padding: "8px 16px"
---

# Design System: Sculpt Workspace

## Overview

**Creative North Star: "The Quarry Ledger"**

Every revision is a numbered block cut from one protected original, marked the way a Carrara quarry stencils lot numbers onto stone. The ground is marble dust: a cool, pale grey. Ink is slate. A single iron-oxide red is the quarry mark, kept for the original, protection status and the one primary action. A muted quarry blue is the second mark and means "moving or selected". Everything else is square plates, 1px slate hairlines and plain working type.

This is a supervisor's console, not an editor, so it runs dense and literal. Machine state is shown verbatim: UUIDs and reports in monospace, job states drawn as lines, failures set in their own crimson callout with the reason and how to recover. The one ornamental move is registration: render plates sit at one fixed scale with crosshairs at their corners, like proofs on a press sheet. The stencil face appears only on revision numerals, which is why the numerals read as marks rather than labels.

It rejects the category default of a dark developer dashboard with a blue accent and a job table. The drop zone is flat: a dashed hairline on a plain surface, with no texture.

**Key Characteristics:**
- Cool marble-dust ground, slate ink, one iron-oxide mark, one quarry-blue state colour.
- Square corners everywhere (0px); structure comes from 1px hairlines, not fills or shadows.
- Stencilled revision blocks (R1, R2...) are the signature; the original carries a red corner tag.
- Job state is drawn as a line: dashed for queued, solid and pulsing for running, struck for failed, dotted for cancelled.
- Render plates are the only lifted surface, framed by registration crosshairs.
- Tabular numerals everywhere; identifiers are monospaced and select-all.

## Colors

A cool, low-chroma stone palette with two scarce marks: iron-oxide red for the original and the primary action, and quarry blue for motion and selection.

### Primary
- **Iron-Oxide Quarry Mark** (mark): the primary button fill, the wordmark's notched block, the corner tag on the original revision block, the drop zone's active border and tint, and text selection. **Burnt Oxide** (mark-hover) is its hover state. **Oxide Ink** (mark-ink) is the darker text-safe variant for icons on light surfaces: the upload glyph and the lock or shield beside each revision's protection verdict.

### Secondary
- **Quarry Blue** (blue): running job state, the upload progress fill, the "Current" badge text, and the focus ring. **Blue Wash** (blue-soft) is the badge ground.

### Tertiary
- **Fault Crimson** (fault): failed states, the engine-down status, and the problem-callout title. **Fault Wash** (fault-soft) is the ground for problem callouts and the engine notice bar; their borders are fault mixed to 40-45% over transparent.

### Neutral
- **Marble Dust** (ground): the page.
- **Polished Face** (surface): the top bar, panels, tables, drop zone, secondary buttons and resting revision blocks.
- **Sunk Face** (surface-sunk): ghost-button hover, progress tracks, report `pre` blocks and skeleton lines.
- **Slate Plate** (plate): the backing behind render images.
- **Slate Ink** (ink), **Slate 2** (ink-2), **Slate 3** (ink-3): primary text, secondary text and metadata or table headers, in that order.
- **Hairline** (rule) and **Strong Hairline** (rule-strong): panel and table borders, then emphasised edges (secondary buttons, selects, dashed drop zone, revision blocks, crosshairs, the revision cut line).

Dark mode follows `prefers-color-scheme` and moves to warm charcoal stone (ground #201f1d, surface #2a2927, ink #e7e6e2). The mark brightens to #c4553a with #e98a6f as its text variant, blue lightens to #8db4d8, and fault to #f08b98. The full dark set lives in the sidecar.

### Named Rules
**The Quarry Mark Rule.** Iron-oxide red marks the wordmark, the original and its protection, the drop target, and the single primary action on a view. It is never used for errors, decoration or headings.

**The Blue Means Moving Rule.** Quarry blue appears only for running work, progress, the current-revision badge and focus. It is never a generic link or brand accent.

**The Fault Is Not The Mark Rule.** Failure uses Fault Crimson (fault) on Fault Wash, a cooler, darker red than the quarry mark, so a rejection is never confused with protected status.

## Typography

**Display Font:** Saira Stencil One (with Barlow fallback), revision numerals only
**Body Font:** Barlow 400/500/600 (with ui-sans-serif, system-ui)
**Label/Mono Font:** the platform monospace stack (ui-monospace, SF Mono, Cascadia Mono, Roboto Mono, Menlo)

**Character:** Barlow is a narrow, slightly industrial grotesque that keeps a dense console readable. The stencil face is the quarry's marking stamp, used so rarely that every appearance reads as a lot number.

### Hierarchy
- **Display** (Saira Stencil One 400, 2.1rem, line-height 1): the current revision block in the workspace header.
- **Numeral** (Saira Stencil One 400, 1.25rem, or 0.95rem in the small block): revision blocks in the rail and tables.
- **Headline** (Barlow 600, 1.75rem, 1.2, -0.015em): page titles and the project name.
- **Title** (Barlow 600, 1.125rem, 1.2): section titles (Upload, Recent projects, Views, Jobs, Revisions), the wordmark and empty-state leads. The drop zone lead steps up to 1.375rem.
- **Body** (Barlow 400, 1rem, 1.5, tabular numerals): default text. Hints are capped at 38ch and empty-state copy at 52ch.
- **Label** (Barlow 500, 0.875rem): buttons, table cells, captions, job rows and metadata lines.
- **Caption** (Barlow 500, 0.75rem): job states, revision meta, protection verdicts, the "Current" badge.
- **Mono** (0.875rem or 0.92em inline): UUIDs, file names, report output.

### Named Rules
**The Stencil Is For Numbers Rule.** Saira Stencil One sets revision numerals (R1, R2...) and nothing else. Never use it for headings, buttons or the wordmark.

**The Verbatim Rule.** Identifiers and machine output (project UUIDs, revision IDs, reports) are set in monospace with `user-select: all`, and wrap anywhere instead of truncating.

## Layout

There is one centred column, max 1480px, padded 32px top, 24px sides and 48px bottom (24/16/32 on phones), under a 56px sticky top bar. Spacing follows a 4px-based scale: 4, 8, 12, 16, 24, 32, 48.

- **Projects:** a two-column grid (5fr upload, 7fr recent projects, with the upload column at least 300px) and a 32px gap, collapsing to one column at 860px.
- **Workspace:** a header row (large revision block, name and UUID, actions on the right) over a hairline, then a 2fr / 1fr body (minimum 300px rail) with a 32px gap. The left side holds a 2x2 grid of square render plates with 24px gaps. The rail holds Jobs, then Revisions.
- **Responsive:** at 1080px the body stacks and the rail becomes two columns. At 860px header actions wrap below the title. At 640px the rail dissolves so Jobs comes before the plates and Revisions after them (running and failed state before images), optional table columns hide, and engine detail text becomes screen-reader-only.

## Elevation & Depth

The system is flat and drawn with hairlines. Surfaces separate by tone (ground, surface, sunk) and 1px borders. The render plate is the one exception: it is lifted like a mounted proof. Interaction never adds shadow; buttons press down 1px on `:active`.

### Shadow Vocabulary
- **Plate lift** (`box-shadow: 0 1px 2px rgb(32 36 39 / 0.08), 0 6px 18px rgb(32 36 39 / 0.07)`; dark: `0 1px 2px rgb(0 0 0 / 0.3), 0 8px 24px rgb(0 0 0 / 0.25)`): render plates only. Pending plates drop it in favour of a dashed hairline.

### Named Rules
**The One Lifted Plate Rule.** Only render plates cast a shadow. Panels, tables, callouts, toasts and buttons stay flat.

## Shapes

Every corner is square (0px): buttons, inputs, panels, blocks, badges, toasts. Form comes from the cut: the wordmark is a 14px block with a stencil notch clipped from its base, and the original revision block carries a 9px triangular red tag clipped into its top-right corner. Borders are 1px. A dashed border means empty or waiting (drop zone, pending plate, empty block). A 1px vertical strong hairline runs down the revision list as the cut line joining blocks.

**The Square Cut Rule.** No border-radius anywhere. Shape is made with clip-path notches and hairlines, never with rounding.

## Components

### Buttons
Quiet and solid, with a 40px tall square plate.
- **Shape:** square (0px), 1px border, 40px minimum height, 16px side padding, Barlow 500 at 0.875rem, optional 18px icon with an 8px gap.
- **Primary:** Iron-Oxide fill with on-mark (near-white) text. Used once per view (Download .blend, Open workspace).
- **Hover / Focus:** primary darkens to Burnt Oxide. Secondary keeps its fill and its border strengthens to ink. All buttons transition colour over 160ms on `cubic-bezier(0.16, 1, 0.3, 1)` and press 1px on `:active`. Focus is a 2px quarry-blue outline offset by 2px.
- **Secondary:** Polished Face fill, Strong Hairline border, ink text (Choose file, Export STL, Retry).
- **Ghost:** transparent, 32px tall, 8px padding, ink-2 text, with a sunk-face fill on hover. Used for icon actions such as copy UUID and checkout.
- **Disabled / Busy:** 55% opacity with a not-allowed cursor; busy buttons spin their icon.

### Revision Block (signature)
A stencilled numeral in a hairline box: 44x36 by default, 34x26 small, 72x56 large. At rest it has a Polished Face fill and a Strong Hairline border. The current revision inverts to an ink fill with ground-coloured text. The block cut from the original (R1) carries the red corner tag. An empty block uses a dashed border and ink-3 text ("R–").

### Line-Form Job State (signature)
A 22px line before a caption-sized label, with the state carried by line style plus hue: dashed ink-2 for queued or blocked, solid 3px quarry blue with a 1.4s opacity pulse for running, solid ink for succeeded, a crimson line with a diagonal strike for failed, and dotted ink-3 for cancelled.

### Render Plate (signature)
A square plate with a Slate Plate backing, the plate-lift shadow, and 7px registration crosshairs drawn 9px outside each corner in Strong Hairline. Images are object-fit contain. Below each plate a caption shows the view name (600, capitalised) on the left and the camera note (ink-3) on the right. While loading, the plate shows a sunk-face sheen sweep. A plate that was never rendered becomes a dashed Polished Face box with "Not rendered".

### Cards / Containers
- **Corner Style:** square (0px).
- **Background:** Polished Face on the Marble Dust ground.
- **Shadow Strategy:** none (see Elevation).
- **Border:** 1px Hairline. Rows inside a panel are divided by the hairline at 60% strength.
- **Internal Padding:** 12px for job rows and table cells, 16px for intake status, 32px/24px for empty states.

### Inputs / Fields
- **Style:** the select is 40px tall, square, with a Polished Face fill, Strong Hairline border and 12px padding. It butts against its button with no gap in the export group (shared edge, no double border).
- **Drop zone:** a flat Polished Face panel with a 1px dashed Strong Hairline and 48px/24px padding, holding a 28px Oxide Ink upload icon, a 1.375rem lead and a 38ch hint. On drag-over the border turns solid quarry mark and the fill tints with 7% mark.
- **Focus:** a 2px quarry-blue outline; file-input labels show it through `:focus-within`.

### Navigation
A 56px sticky Polished Face top bar with a bottom hairline, holding the wordmark (notched red block, Barlow 600 at 1.125rem), a Projects link (500, ink-2, turning ink with a 2px ink underline when current) and the right-aligned engine status at 0.875rem (fault-coloured when Blender is unavailable).

### Problem Callout
A Fault Wash panel with a 1px fault border at 45% mix, a crimson title with an alert icon, then the reason and recovery steps verbatim in ink. Used inside jobs, intake and import failures.

### Toast
A square ink chip with ground-coloured text, fixed bottom-centre. It rises 12px and fades in over 180ms.

### Icons
Tabler outline icons at stroke 1.75, from a local SVG sprite, at 18px (28px large, 15px in revision verdicts), coloured with currentColor.

## Do's and Don'ts

### Do:
- **Do** keep every corner at 0px and build structure from 1px Hairline and Strong Hairline borders.
- **Do** reserve the iron-oxide mark for the wordmark, the original revision's corner tag, protection verdict icons, the drop-target state and one primary button per view.
- **Do** set revision numbers as stencilled blocks, with the current revision inverted to ink and the original tagged red.
- **Do** show job state as line form plus hue (dashed queued, solid running, struck failed, dotted cancelled), never hue alone.
- **Do** present render views as a fixed-scale 2x2 grid of registered plates with crosshairs and a view/camera caption.
- **Do** surface failures verbatim in a Fault Wash callout that names the problem and the recovery.
- **Do** use tabular numerals and monospace, select-all identifiers.
- **Do** honour `prefers-reduced-motion`; motion is limited to 80-200ms state transitions, the running-line pulse, the loading sheen, and the code carve.
- **Do** keep the code carve to moments when a script loads (a new revision, switching revisions): a quarry-blue scan line down the editor and a staggered sweep across each changed line, about 1.6s in total. Never play it while the user types; unpublished edits only get static oxide markers.

### Don't:
- **Don't** round corners or use pill shapes.
- **Don't** use the quarry red for errors; failures are Fault Crimson.
- **Don't** use quarry blue as a brand or link accent, or build a dark dashboard with a blue accent as the default look.
- **Don't** add shadows to panels, tables, buttons or toasts; only render plates are lifted.
- **Don't** use Saira Stencil One for anything except revision numerals.
- **Don't** add texture, gradients or imagery to the drop zone or page ground; the drop zone is a flat dashed panel.
- **Don't** add uppercase tracked eyebrow labels above titles; section titles stand alone.
