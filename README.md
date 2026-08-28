# FLARE

**F**lexible **L**ighting **A**utomation & **R**econciliation **E**ngine.

Your lights, matched to the shape of your day — bright and cool to help you wake up, gradually warming through
the afternoon, dimming to a relaxed glow as evening sets in, and low and warm once the house is asleep. Rooms
turn their lights on and off as people come and go, manual changes are left alone until you're done with them,
and anything a scene already has covered is left to the scene.

## 📖 [Read the documentation](https://danrspencer.github.io/adaptive_lighting/)

The quickest way to see what this actually does is the
**[interactive curve playground](https://danrspencer.github.io/adaptive_lighting/playground.html)** — it runs the
real dashboard card, and you can drag the schedule and curve settings around and watch it redraw.

- **[Quickstart](https://danrspencer.github.io/adaptive_lighting/installation/)** — HACS, the blueprint, and the dashboard card
- **[Power users](https://danrspencer.github.io/adaptive_lighting/advanced/)** — every service and entity, scene handoff, and building without the blueprint
- **[Blueprint reference](https://danrspencer.github.io/adaptive_lighting/blueprint/)** — every input, feature by feature
- **[Contributing](https://danrspencer.github.io/adaptive_lighting/contributing/)** — repository layout and the test suite

## Why four phases, not a continuous curve

Most adaptive-lighting tools compute one continuous curve straight from the sun's position — brightness and
colour temperature interpolated smoothly between sunrise and sunset, nothing else to it. That's a reasonable
default, but it treats every part of your day the same way: just "more or less light," rather than light with a
*purpose*. FLARE instead uses four named phases, each justified on its own terms:

- **Morning** exists to help you wake up, not to track sunrise. It starts at a fixed time before you'd normally
  be up, independent of the season — a sun-tracking curve would have it arrive at 5am in June and 8am in
  December, which isn't what a wake-up light is for. Bright, cool-white light in the morning has been linked to
  better alertness later in the day: [one study](https://pubmed.ncbi.nlm.nih.gov/36058557/) found office workers
  given 1.5 hours of bright morning light for a week had higher sleep efficiency and less morning sleepiness than
  under regular office lighting.
- **Day** is the long middle stretch, gradually warming as it runs toward evening so the eventual transition
  doesn't feel abrupt.
- **Evening** is when relaxed, warm lighting takes over — the one phase that *does* track the sun (sunset), so
  your indoor lighting shifts in step with what's actually happening outside. It's clamped between an earliest
  and latest bound, though, so a 4pm winter sunset doesn't dump you into "relaxed evening" mode the moment you
  walk in from work, and a 10pm midsummer sunset doesn't mean evening never really arrives.
- **Night** isn't tied to any solar event at all — it's just what the house should look like once everyone's
  asleep: dim and warm, the lighting you want on at 3am without waking yourself up further.

## The two pieces

Installed separately, and useful separately.

**FLARE** is a Home Assistant integration. It exposes the phase schedule above, plus
per-light grouping (reachability, tolerance, override protection, two-step transitions, optional RGB colour) and
scene-coverage gap filling, as plain Home Assistant services — `compute_lighting_groups`, `compute_curve` and
`compute_scene_coverage` are pure planners that hand back data; `apply_lighting` wraps the same grouping logic
and actually turns lights on and off. All usable from your own automations with no blueprint required.

**The blueprint** is a ready-made room automation built on those services. It depends on them entirely and
does nothing without them — it's a worked example rather than a separate product, wiring the services up the
way most rooms want them so you can get going without writing anything. Brightness and colour temperature
follow the phase schedule, motion controls on/off, scenes can take over partially or entirely, manual changes
are respected, and lights that don't reach their target get corrected automatically. And because it's a
blueprint it isn't a black box: take it, change it, or rip it apart to build something different on the same
services.

## License

[MIT](LICENSE)
