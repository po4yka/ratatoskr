---
name: ponytail-help
description: >
  Quick-reference card for all ponytail modes, skills, and commands.
  One-shot display, not a persistent mode. Trigger: /ponytail-help,
  "ponytail help", "what ponytail commands", "how do I use ponytail".
---

# Ponytail Help

Display this reference card when invoked. One-shot, do NOT change mode,
write flag files, or persist anything.

## Levels

| Level | Trigger | What change |
|-------|---------|-------------|
| **Lite** | `/ponytail lite` | Build what's asked, name the lazier alternative in one line. |
| **Full** | `/ponytail` | The ladder enforced: YAGNI → stdlib → native → one line → minimum. Default. |
| **Ultra** | `/ponytail ultra` | YAGNI extremist. Deletion before addition. Challenges requirements before building. |

The selected level applies only to the request that invoked Ponytail.

## Skills

| Skill | Trigger | What it does |
|-------|---------|--------------|
| **ponytail** | `/ponytail` | Lazy mode itself. Simplest solution that works. |
| **ponytail-review** | `/ponytail-review` | Over-engineering review: `L42: yagni: factory, one product. Inline.` |
| **ponytail-audit** | `/ponytail-audit` | Whole-repository audit for over-engineering and removable code. |
| **ponytail-debt** | `/ponytail-debt` | Inventory `ponytail:` comments and their upgrade triggers. |
| **ponytail-help** | `/ponytail-help` | This card. |

Use the trigger form shown above for this host.

## Scope

No persistent mode or flag is stored. Invoke Ponytail again for another
request. `/ponytail off` and "normal mode" leave the current request in normal
mode.

## More

Full docs + examples: https://github.com/DietrichGebert/ponytail
