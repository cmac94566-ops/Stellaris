# Stellaris 4.4.6 "Pegasus" - Curated Mechanics Baseline

This is the version-locked Overmind baseline for Stellaris `4.4.6` (checksum
`fdde`). It distills official patch mechanics, not untested community build
rankings. The structured source is [docs/meta/4.4.6.json](meta/4.4.6.json).

## Nomads

- Nomadic empires use Arkships, Waystations, Waylines, and Contracts rather than
  ordinary territorial expansion.
- The engine must not recommend normal system claiming to a Nomadic empire.
- `COLONIZE` is settlement intent only when the state confirms settlement is
  available. `BUILD_STARBASE` means Waystation-network intent for Nomads.
- Operational Reserves are a hard economic constraint. The critical stage harms
  research, unity, alloys, and job efficiency.

## Confirmed 4.4 Changes

- Planet designation bonuses increased, and native AI more strongly matches
  buildings and district zones to a colony designation.
- Native AI now targets unity across planning stages, weights ascension paths
  more highly, expands small empires more aggressively, and discourages excess
  undeveloped colonies.
- Non-primary war participants can negotiate to join or leave a war. Fully
  occupied empires gain escalating exhaustion and attrition.
- 4.4.5 made resources adjustable by galaxy setting and added Nomad automation
  options; strategy must therefore treat resource abundance as state-dependent.

## Validation Boundary

Weapon verdicts and community build tiers from 4.3 are not asserted as 4.4.6
truth. Promote them only after a recorded 4.4.6 game test and meta review.

Sources: [Patch 4.4](https://stellaris.paradoxwikis.com/Patch_4.4) and
[Patch 4.4.X](https://stellaris.paradoxwikis.com/Patch_4.4.X).