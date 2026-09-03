# DoTasks Logo

## Primary direction: Done Loop

The selected mark combines three ideas in one compact shape:

- three stacked task cards form the vertical stem of the letter `D`;
- the white outer curve represents a workflow moving through the board;
- the mint check signals a verified completion rather than activity alone.

The design keeps the product's existing blue accent while reducing detail at favicon and sidebar sizes. It is geometric, calm, and technical without resembling a generic chat bubble or checklist app.

## Files

- `dotasks-mark.svg`: primary icon-only mark.
- `dotasks-logo-horizontal.svg`: primary lockup for light backgrounds.
- `dotasks-logo-horizontal-dark.svg`: primary lockup for dark backgrounds.
- `dotasks-logo-vertical.svg`: centered lockup for square or stacked placements.
- `dotasks-mark-mono-dark.svg`: single-color mark for light backgrounds.
- `dotasks-mark-mono-light.svg`: single-color mark for dark backgrounds.
- `concepts/`: three explored directions; `concept-1-done-loop.svg` is the selected production direction.
- `../../../web/public/dotasks-mark.svg`: web source used by the sidebar and favicon.
- `../../../static/dotasks-mark.svg`: built/runtime copy of the production mark.

## Palette

| Role | Color | Usage |
| --- | --- | --- |
| DoTasks blue | `#317CFF` | Primary mark and product accent |
| Completion mint | `#8AF0B6` | Successful workflow completion |
| Ink | `#18181B` | Wordmark on light backgrounds |
| Paper | `#F7F7F8` | Wordmark on dark backgrounds |

## Usage

- Use the icon-only mark below 120 px of available horizontal space.
- Use the horizontal lockup in headers, documentation, and release material.
- Keep clear space equal to one task-card width around every side of the mark.
- Minimum recommended size is 16 px for the mark and 120 px wide for the horizontal lockup.
- The production icon has a transparent canvas; do not add another container or corner radius around it.

## Do not

- stretch, skew, rotate, outline, or add shadows to the mark;
- recolor individual task cards;
- place the full-color mark on a saturated blue background;
- typeset a replacement wordmark with a decorative font;
- remove the completion check or separate it from the workflow loop.

## Export

SVG is the source of truth. When a PNG is required, export from the SVG at the exact target size:

```bash
inkscape dotasks-logo-horizontal.svg --export-type=png --export-width=1040
inkscape dotasks-mark-mono-dark.svg --export-type=png --export-width=512
```
