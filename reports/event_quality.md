# Improvement event quality report

Generated 2026-09-03T21:24:27.508755+00:00 by `scripts/10_analyze_event_quality.py` over 523908 events (stage 08) for the 39902 repos found by `.kicad_pro`. Flags per event are in `data/cache/event_quality/state.sqlite`, table `event_quality`.

## Filters

| filter | rule |
|---|---|
| scope | event inside a qualified project dir; PR events must be merged; at least one `.kicad_pcb` changed |
| text | title + body with checklists, HTML comments, quotes, sign-offs removed is >= 100 chars; not a PR template (checklist markers); < 50% TODO lines; not just issue numbers / URLs |
| size | added + deleted lines over the event's KiCad files <= 8000; total changed files <= 40 |
| modified only | every KiCad file has status `modified`, so before and after states pair up |
| semantics | from the patch text, changed lines whose leading token is not save-time churn (`uuid`, `tstamp`, `version`, `generator`, ...) >= 5; this is a proxy for a netlist comparison, which needs the full files |
| quota | at most 20 events per repo, best tier then longest text first |

Tiers: **A** all filters pass and an issue is linked; **B** all filters pass; **C** text / size / modified pass but GitHub omitted the patch (large file), semantics unknown; **X** excluded.

## Funnel: PR events

| step | events | repos |
|---|---|---|
| all events | 33844 | 4418 |
| inside a qualified project dir | 16372 | 3520 |
| PR merged (commits always pass) | 14200 | 3197 |
| .kicad_pcb changed | 12152 | 3022 |
| text: cleaned body >= 100 chars, no template / TODO / refs-only | 3534 | 1125 |
| size: KiCad lines <= 8000 and changed files <= 40 | 1066 | 435 |
| all KiCad files modified (no add / rename / remove) | 855 | 325 |
| patch text available | 511 | 190 |
| semantic changed lines >= 5 (not save-only churn) | 328 | 169 |
| within per-repo quota of 20 | 283 | 168 |

## Funnel: commit events

| step | events | repos |
|---|---|---|
| all events | 490064 | 32026 |
| inside a qualified project dir | 490064 | 32026 |
| PR merged (commits always pass) | 490064 | 32026 |
| .kicad_pcb changed | 275885 | 31972 |
| text: cleaned body >= 100 chars, no template / TODO / refs-only | 23086 | 5138 |
| size: KiCad lines <= 8000 and changed files <= 40 | 10456 | 2899 |
| all KiCad files modified (no add / rename / remove) | 8444 | 2359 |
| patch text available | 4398 | 1429 |
| semantic changed lines >= 5 (not save-only churn) | 3957 | 1372 |
| within per-repo quota of 20 | 3416 | 1372 |

## Tiers

| tier | PR events | commit events | repos |
|---|---|---|---|
| A | 70 | 130 | 57 |
| B | 258 | 3827 | 1411 |
| C | 344 | 4046 | 1722 |
| X | 33172 | 482061 | 32544 |

Final pool (tiers A + B within quota): **3699** events from **1441** repos (283 PR, 3416 commit); quota removed 586 events.

## Why events are excluded

Counted over eligible events (qualified dir, merged or commit, pcb changed); an event can fail several filters.

| reason | PR events | commit events |
|---|---|---|
| text | 8618 | 252799 |
| size | 8653 | 136734 |
| not_modified_only | 6062 | 88818 |
| save_only_churn | 183 | 441 |

Text failures broken down:

| sub-reason | PR events | commit events |
|---|---|---|
| cleaned body shorter than threshold | 8016 | 252743 |
| PR template | 585 | 14 |
| TODO-dominated | 2 | 80 |
| issue numbers / URLs only | 149 | 2417 |

## Save-only churn

4909 events had patch text for every KiCad file. Semantic vs churn changed lines:

| | events | median semantic lines | median churn lines | churn-only events |
|---|---|---|---|---|
| pull_request | 511 | 24 | 3 | 183 |
| commit | 4398 | 165 | 4 | 441 |

## Repo concentration

Top repos by tier A + B events before the quota:

| repo | A + B events | kept by quota |
|---|---|---|
| sabas0ba/kicad_skills | 158 | 20 |
| LastZactionHero/defcon-silent-disco | 110 | 20 |
| rjwalters/kicad-tools | 97 | 20 |
| Tinkerbug-Robotics/TinkerRocket | 67 | 20 |
| dektronics/printalyzer-timer | 63 | 20 |
| nielsverhoeven/PoE-FanController | 52 | 20 |
| Swyter/psdaptwor | 46 | 20 |
| worlickwerx/pi-cluster-two | 45 | 20 |
| nanographs/Scan-Gen-Glasgow-Testing | 36 | 20 |
| ideocentric/caryatid | 36 | 20 |
| EwoudVV/ducktop2 | 34 | 20 |
| v3l0c1r4pt0r/ucm4 | 30 | 20 |

## 3D models in qualified repos

Of 32029 qualified repos, 16977 ship at least one 3D model file (.step, .stp, .wrl, .stl, .iges, .igs, .3mf, .obj, .f3d, .fcstd, .scad); 10492 have one inside a project dir. Files by suffix: .obj 153016, .step 134340, .stl 67954, .stp 37827, .wrl 32117, .scad 19855, .fcstd 11431, .3mf 10295, .f3d 3674, .igs 565, .iges 317. Table `repo_3d_models`.

## Samples

### Tier A

- **rjwalters/kicad-tools** commit a133e6a270, dir `boards/03-usb-joystick/output`, pcb 1 sch 0 pro 0, semantic 272 churn 34 lines, text 4496 chars, issues [3532, 3535, 3545]. *fix(router): 45° quantization in post-route mutation passes (closes #3532) (#3537)* [link](https://github.com/rjwalters/kicad-tools/commit/a133e6a2708e4ab3a9e641091493320c79f0f119)
- **rjwalters/kicad-tools** PR #4045, dir `boards/00-simple-led/output`, pcb 2 sch 0 pro 0, semantic 8 churn 0 lines, text 4210 chars, issues [4034]. *fix(3d): offset canonical STEP models onto origin-centered pads by pad-centroid delta* [link](https://github.com/rjwalters/kicad-tools/pull/4045)
- **broncoracing/bcm** PR #14, dir `pcb`, pcb 1 sch 2 pro 0, semantic 21 churn 3 lines, text 112 chars, issues [13]. *Correct PCB to fix firmware flashing* [link](https://github.com/broncoracing/bcm/pull/14)
- **amachronic/echoplayer** commit e14b09d176, dir `r1-rev2`, pcb 1 sch 0 pro 0, semantic 12 churn 60 lines, text 198 chars, issues [12]. *Fix incorrect 3.5mm jack pinout* [link](https://github.com/amachronic/echoplayer/commit/e14b09d176ed4ae50ebee3c8a1b28d569ed9379b)

### Tier B

- **CamelCaseName/Nano33IOTShield** commit 5ea903aa1c, dir `/`, pcb 1 sch 1 pro 0, semantic 80 churn 10 lines, text 131 chars, issues []. *Forgot to set OE high when power is connected* [link](https://github.com/CamelCaseName/Nano33IOTShield/commit/5ea903aa1c8c67400001ea037d9dd4bb3b684a42)
- **hdlguy/kicad** commit 3254496826, dir `zmod_m2_ssd`, pcb 1 sch 1 pro 0, semantic 622 churn 15 lines, text 108 chars, issues []. *Rev B of zmod M.2, changed clock buffer to give full edge rate, reduced M.2 mounting hole from 2.7 t* [link](https://github.com/hdlguy/kicad/commit/32544968268f2c206f2fd40ef891add6155ef4e9)
- **Stab-Rabbit-coding/Open-Secure-ESC** commit c0bafe31af, dir `builds/6s/50A/CAN_485_faraday/kicad`, pcb 1 sch 0 pro 0, semantic 453 churn 90 lines, text 2778 chars, issues []. *Assembly prep for professional reflow; correct the routing state* [link](https://github.com/Stab-Rabbit-coding/Open-Secure-ESC/commit/c0bafe31af89ca2e3deae91d0501033ee18a90d0)
- **lhr-solar/PS-PowerBoard** commit 248603deca, dir `/`, pcb 1 sch 0 pro 0, semantic 86 churn 0 lines, text 104 chars, issues []. *Merge pull request #36 from corbosiny/garrettW* [link](https://github.com/lhr-solar/PS-PowerBoard/commit/248603deca7ad97c6a18e58687cf5d5d31e33062)

## Next steps

- Netlist comparison: parse `.kicad_sch` / `.kicad_pcb` S-expressions at before and after sha (needs the full files, i.e. a checkout) and compare nets, components, footprints; the churn heuristic above only reads patches.
- DRC / ERC with `kicad-cli` (found at /Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli) on both states; keep events whose error counts do not grow.
- Manual review of the tier A seed set: `reports/seed_issue_driven_prs.csv`.
- Rerun after the 29279 added repos are fetched (Release C).
