# LUMEN — the vault, designed

Working design for the multi-room vault. This is the reference the
implementation answers to; where the code and this document disagree, one of
them is wrong and it needs saying which.

---

## 0. The one idea

**You carry the only light.** Everything below is judged against whether it
makes that sentence matter more. A system that would work identically in a
game with room lighting is a system that does not belong here.

The single-chamber floor failed that test. A floor took six seconds, so the
lantern's fuel — the game's only clock — never got a chance to run down.
Fuel was a number that decorated a fight instead of a resource that shaped a
route. Multi-room floors exist to fix that specifically, and every decision
here is downstream of it.

---

## 1. Floor shape

A floor is a **graph of rooms on an integer grid**. Each room is a `Level` —
the existing chamber generator, unchanged in kind — and only a few are baked
at a time. The floor owns the graph; the world owns one room at a time.

### Why room-as-Level and not one big map

One big map is the obvious idea and it is wrong here, for three measured
reasons:

* A chamber bake costs **110–180 ms** and the layers run about 8 MB each. An
  eight-room floor as one image is a bake the player waits through and a
  working set that does not fit.
* Shadowcasting cost scales with wall segments in range. One room's worth of
  geometry is what the lighting was tuned against.
* The camera is bounded to the level. A single map means either an unbounded
  camera or artificial bounds — both lose the framing that makes a chamber
  read as a room.

Room-as-`Level` keeps every one of those costs constant no matter how large
the floor gets, and reuses the generator, the bake, the flow field and the
lighting exactly as they are.

The cost it introduces is the transition, and that is paid with a bake-ahead
worker: on entering a room, every unbaked neighbour is queued on the thread
that already exists for building the next floor behind the offering. The
player is in a room for twenty to forty seconds; a neighbour needs one fifth
of one of those.

### Generation

1. Room count for the depth: `6 + act + rng(0..2)`, so 7 early and up to 13
   in the last act.
2. A **critical path** from ENTRANCE to DESCENT, random-walked on the grid,
   never revisiting, biased toward turning. Length ~60% of the room count.
3. **Branches** hung off the critical path, one to three rooms deep, ending
   in dead ends.
4. Room types assigned by role (§3).
5. Guaranteed: one ENTRANCE, one DESCENT, the DESCENT at the far end.

**Critical path plus branches, not a maze.** A maze offers no decision,
because you cannot tell a detour from the way forward until you have already
walked it. A path with visible branches asks the only question that matters:
*is that dead end worth the fuel?*

---

## 2. The rule that makes it a game

**The descent opens the moment you reach it. You never have to clear a floor.**

This is the most important line in this document. If a floor must be cleared,
the map is a chore list, every run spends the same fuel, and the lantern is
back to being decoration. If the floor can be skipped, then every room is a
wager: light, health and time against embers, relics and strength.

So the floor is a **budget**, not a checklist. What you leave behind is lost
when you descend. A full clear pays a bonus, so the completionist has a
reason without the hurried player being punished.

---

## 3. Room types

| Type | Job | Wards shut |
| --- | --- | --- |
| ENTRANCE | Where you arrive. A lit brazier. Never hostile. | — |
| COMBAT | The staple. One wave early; two or three later. | yes |
| ELITE | One elite with an escort. Always drops. | yes |
| CACHE | Treasure. Embers, or a choice of two relics. | — |
| SHOP | The Ferryman. Embers spent here are embers not banked. | — |
| SHRINE | A pact: pay something real, take something real. | — |
| HEARTH | Rest. Full fuel, a little health, nothing in the room. Rare. | — |
| GAUNTLET | Optional and hard, marked as such. Large reward. | yes |
| DESCENT | The rift down. Open on arrival. | — |
| BOSS | Act boss. | yes |

### The ward

A room with living things in it **seals** as you enter: a plane of cold light
across each doorway — the vault's own light, not yours, so it reads as
something done *to* you. It hums while it holds. The last death breaks all of
them at once, with a shove.

This is what turns a room you ran through into a fight you were in.

---

## 4. Acts

Twenty floors, three acts, a boss closing each.

| Act | Floors | Boss | Identity |
| --- | --- | --- | --- |
| I — The Undercroft | 1–6 | **The Hollow Choir** (6) | Stone and teaching. Small rooms, few species. |
| II — The Cisterns | 7–13 | **The Snuffer** (13) | Standing water, long sightlines, more to dodge. |
| III — The Sealed Vault | 14–20 | **The Keeper** (20) | Light itself, turned round. |

Six, seven and seven. Acts lengthen as they go, because a later act has more
to show before its boss.

Each act carries its own species set, its own room-type weights, its own
hazard and its own palette shift. The art is procedural, so an act tint costs
almost nothing and changes everything about how a floor reads.

---

## 5. Getting stronger

Four sources, deliberately different in feel.

**Offerings** (existing, 58). Three cards between floors. A *choice*.

**The Vigil** (existing, 14 nodes). Between runs. *Permanent, and capped.*

**Relics** — new. Objects, not modifiers: a name, an icon, a rule, and a row
in the HUD. Found in caches and bought from the Ferryman. Where an offering
is chosen from three, a relic is *found in the dark* — and that should feel
different. Target ~24.

**The lantern itself** — new, and rare. Three axes, because the lantern is
the game and nothing else should be allowed to touch it:

* **REACH** — how far it throws.
* **DEPTH** — how long it lasts.
* **EDGE** — what happens at the rim. This is the interesting one: it makes
  the boundary of your own light into a weapon, which is a thing only this
  game can offer.

**Weapon mods** — one per weapon, so a run can commit to a weapon rather
than merely carry it.

**Act boons** — after each boss, a choice of three offerings strictly larger
than the draft's. The act structure needs a payoff at its seam.

---

## 6. Economy

Embers already drop and already bank. They become **spendable in the run**,
at the Ferryman, and *what you do not spend is what you bank*.

That is the whole tension in one currency: **power now, or power next time.**
Two currencies would let the player have both and decide nothing.

Ferryman stock: three slots, one paid reroll. An offering priced by rarity, a
weapon you do not carry, fuel and health, a relic, a redraw.

---

## 7. Species

Five today. Eighteen is the target, and each new one has to ask a question
the roster does not already ask. The existing five ask: pressure (Crawler),
soak (Husk), positioning (Spitter), terrain-ignoring (Wisp), flanking
(Warden).

The ones worth building are the ones that could only exist in this game:

* **something that only moves while unlit** — the purest statement of the
  premise
* **something only vulnerable while lit** — you must hold light on it
* **something that eats fuel** near you
* **something that snuffs your lantern** on contact
* **something that mimics a pickup**

and then the honest genre staples the roster needs to function: a splitter, a
healer, a charger, a burrower, a reflector, a swarm, a corpse that leaves a
hazard.

**Built: fifteen.** Crawler, Spitter, Cinder, Husk, Wisp, Splitter, Warden,
Bolter, Lurker, Carrion, Keener, Delver, Pale, Mirror, Douser - introduced one
a floor for sixteen floors, so the vault is still teaching you something new
most of the way down. `tools/bestiary_check.py` holds each of them to the
claim made for it: the lurker must actually stop when lit (measured: 305 px/s
dark, 2 px/s lit), the pale must actually shrug off a shot in the dark
(measured: 8 of 100), a splitter must leave exactly two things behind.

### The three that could only exist here

* **The lurker** moves only while it is unlit. Your light is how you see it
  and also how you stop it, and you cannot point it everywhere.
* **The pale** can only be hurt while it *is* lit. The mirror of the lurker,
  and the reason both exist: a room holding the two cannot be solved by
  pointing the lantern one way and leaving it there.
* **The douser** takes fuel rather than health, and chokes the flame for
  seconds. The only thing in the vault that attacks the resource instead of
  the body.

---

## 7a. The Keeper

The last thing in the vault, and the reason the vault is dark: it has every
lantern but yours.

For nineteen floors light is the one safe thing in the game. This fight takes
that away by turning it round - the Keeper attacks *with* light. It brands the
floor with it, sweeps the room with it, carries a ring of stolen lanterns that
shield it and fire outward, and at the end gathers every scrap in the chamber
and throws the lot at you. Its shadows are the only cover there is, which is
why its sanctum has pillars in it.

And where the Snuffer's own light shrinks as it rages, the Keeper's **grows**.
The last phase of the last fight is played in a room that is almost entirely
lit, with almost nowhere left to stand.

`tools/boss_probe.py` exists because this game has already shipped a final
boss that was easier than the one halfway up. It fights all three with the
same build and the same fixed kiting policy, and it caught three faults in
the Keeper on its first run:

* Its `die` never called `super().die`, so it sat alive at **minus thirty per
  cent health** and no fight ever ended.
* Its stolen lanterns each blocked a 0.42 rad arc. Seven of them covered
  **94% of every angle the player could shoot from** - not a shield, an
  invulnerability.
* They never expired, so they were back to ten strong by the minute mark.

| | fight | dmg/s | over the fight | hits/s |
| --- | --- | --- | --- | --- |
| The Hollow Choir, floor 6 | 53 s | 18.03 | 943 | 1.147 |
| The Snuffer, floor 13 | 86 s | 34.05 | 2750 | 1.093 |
| **The Keeper, floor 20** | **97 s** | **50.84** | **5680** | **1.043** |

Eight seeds, standard error +/-1.8, +/-3.9 and +/-7.1. The escalation is real.

## 8. Sound

Every new object makes a sound or it is not in the world. All of it
synthesised, in the existing idiom.

Wards on, holding, broken. Room cleared. The Ferryman's greeting, a purchase,
a refusal. A relic taken. A shrine's bargain struck. A hearth. The map. An
act's seam. Then the full set for each new species, and a voice for the third
boss.

---

## 9. Order of work

| | | |
| --- | --- | --- |
| 0 | Autopilot can finish a run again | **done** |
| 1 | The floor graph, as data | **done** — `lumen/floorplan.py` |
| 2 | Doors in chamber generation | **done** — `lumen/level.py` |
| 3 | Room transitions and bake-ahead | **done** — `lumen/rooms.py` |
| 4 | Wards | **done** |
| 5 | The floor map | **done** — `lumen/hud.py` |
| 6 | Caches, hearths, shrines | **done** — `lumen/fixtures.py` |
| 7 | Twenty floors and three acts | **done** (the third boss is not) |
| 8 | The Ferryman, and embers spent in a run | to do |
| 9 | Relics, lantern shaping, weapon mods | to do |
| 10 | The third boss | to do |
| 11 | Species, up to eighteen | to do |
| 12 | Sound for all of it | six in; the rest follow their systems |
| 13 | Balance | to do |

### What checks it

* `tools/floor_report.py` — ten thousand floors, every invariant, the
  distributions that say whether they are worth walking.
* `tools/room_check.py` — every layout, every size, all fifteen door
  combinations: every door reachable, every entry clear.
* `tools/playtest.py --explore` — the bot walks the whole floor graph.
* `tools/stat_probe.py` — every upgrade still moves something.
* `tools/sound_report.py` — nothing plays a sound that is not in the bank.
