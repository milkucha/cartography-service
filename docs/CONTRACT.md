# Cartography for Mindcraft-CE: HTTP contract

Shared interface between the service (`cartography-service`), the optional Fabric plugin, and the Mindcraft
hooks (`topography.js` and friends). Any change here must be reflected in all three. Names for the service as a
whole (master switch, URL, token, environment variables, logs) use *cartography*; the terrain layer keeps
*topography* (`$TOPOGRAPHY`, `!topography`, `/topography`, `topography_*` perception and routing settings).

Base URL: `http://127.0.0.1:8090` (override with `CARTOGRAPHY_SERVICE_URL`).

Bounds enforced by the service:

- `radius`: `1..256` (default `32`)
- line length: max `4096` samples
- request body: max `1 000 000` bytes
- coordinates: block coordinates; chunks are `16x16` (`cx = x >> 4`, `cz = z >> 4`)

## Queries (Mindcraft -> service)

### `GET /health`

```json
{
  "status": "ok",
  "service": "cartography",
  "loader_available": true,
  "region_dir": "C:/server/world/region",
  "region_files": 12,
  "hot_chunks": 0,
  "warm_chunks": 0,
  "dirty_chunks": 0
}
```

`hot_chunks` / `warm_chunks` / `dirty_chunks` come from the store stats and may vary.

### `GET /topography?x1=&z1=&x2=&z2=`

```json
{
  "from": {"x": 10, "z": 8},
  "to": {"x": 22, "z": 8},
  "sample_count": 13,
  "min_y": 63,
  "max_y": 66,
  "runs": [
    {"x1": 10, "z1": 8, "x2": 15, "z2": 8, "y": 65, "material": "minecraft:grass_block", "length": 6}
  ],
  "transitions": [{"x": 16, "z": 8, "from": "land", "to": "water"}],
  "points": [{"x": 10, "z": 8, "y": 65, "material": "minecraft:grass_block", "water": false}]
}
```

`runs` collapses consecutive columns with the same surface into a segment. `transitions`
marks land/water changes along the line.

### `GET /area_composition?x=&z=&radius=`

```json
{
  "center": {"x": 8, "z": 8},
  "radius": 6,
  "columns": 113,
  "chunks": 1,
  "missing_chunks": 0,
  "dominant_biome": "minecraft:plains",
  "biomes": {"minecraft:plains": 1},
  "surface": {"min_y": 64, "max_y": 66, "avg_y": 65, "water_fraction": 0},
  "composition": {"natural": 4096, "engineered": 12, "liquid": 0, "air": 0},
  "artificiality_score": 0.0029,
  "avg_chunk_artificiality": 0.0029,
  "top_blocks": [["minecraft:stone", 2048]],
  "strong_indicators": {"minecraft:chest": 1}
}
```

### `GET /shoreline?x=&z=&radius=`

```json
{
  "center": {"x": 10, "z": 8},
  "radius": 8,
  "water_at_center": false,
  "segments": [[[16, 0], [16, 8], [16, 16]]],
  "point_count": 3,
  "missing_chunks": 0
}
```

`segments` is a list of simplified polylines; each point is `[x, z]`. At most three nearest water
components are returned, ordered from the query center.

### `GET /recall?x=&z=`

Returns the cached summary for the chunk containing `(x, z)`, or `404` if the chunk is unknown.

```json
{
  "chunk": {"cx": 0, "cz": 0},
  "chunk_bounds": {"x1": 0, "z1": 0, "x2": 15, "z2": 15},
  "column": {"x": 8, "z": 8, "y": 65, "material": "minecraft:grass_block", "ground_y": 65, "water": false},
  "surface": {"min_y": 64, "max_y": 66, "avg_y": 65, "water_columns": 0},
  "dominant_biome": "minecraft:plains",
  "composition": {"natural": 4096, "engineered": 12},
  "artificiality_score": 0.0029,
  "strong_indicators": ["minecraft:chest"],
  "top_blocks": [["minecraft:stone", 2048]],
  "source": "save",
  "updated_at": "2026-11-05T12:00:00Z"
}
```

### `GET /chunk?cx=&cz=`

Debug endpoint: returns the raw stored chunk record, or `404`.

All query failures return `{"error": "..."}` with status `400`, `404`, or `500`.

## Diff ingestion (plugin -> service)

### `POST /diff`

Accepts a single delta object, a JSON array of deltas, or `{"deltas": [...]}`.

Response:

```json
{"accepted": 42, "rejected": 0}
```

### Delta schema

Common fields:

| Field | Type | Notes |
|-------|------|-------|
| `action` | string | `place`, `break`, `chunk_load`, or `chunk_unload` |
| `x`, `y`, `z` | int | Block coordinates |
| `cx`, `cz` | int | Chunk coordinates (optional; `x >> 4`, `z >> 4` are used) |
| `block_name` | string | Namespaced block id, e.g. `minecraft:stone` |

`place`:

```json
{"action": "place", "x": 10, "y": 64, "z": 8, "block_name": "minecraft:oak_planks"}
```

`break` additionally carries the new surface after the block is removed. The plugin computes
these by scanning the column down from the broken block:

```json
{
  "action": "break",
  "x": 10, "y": 64, "z": 8,
  "block_name": "minecraft:oak_planks",
  "new_top_y": 63, "new_top_block": "minecraft:dirt",
  "new_ground_y": 63, "new_ground_block": "minecraft:dirt"
}
```

`chunk_load` / `chunk_unload` notify the service that a chunk entered or left the server's
working set; the service currently acknowledges them without changing block data.

```json
{"action": "chunk_load", "cx": 0, "cz": 0, "x": 0, "y": -64, "z": 0}
```

Deltas with an unknown `action`, missing coordinates, or a missing `block_name` on `place` are
rejected. The service ignores non-Overworld changes on the plugin side.

## Bot-fed chunk ingestion (Mindcraft -> service)

Mindcraft forwards chunks it has loaded, so the map works with no server mod and no save access.
The payload carries block **names** (not numeric state ids) and palette indices packed the same way
Minecraft stores them.

### `POST /chunk_raw`

`Content-Encoding: gzip` is allowed. Request:

```json
{
  "agent": "Topo",
  "dimension": "minecraft:overworld",
  "format": 1,
  "chunks": [
    {
      "cx": 0, "cz": 0, "min_y": -64, "height": 384,
      "sections": [
        { "y": -4, "palette": ["minecraft:bedrock", "minecraft:deepslate"], "enc": "packed", "bits": 4, "data": "<base64>" },
        { "y": 2,  "palette": ["minecraft:stone"] },
        { "y": 4,  "palette": ["..."], "enc": "u16", "data": "<base64>" }
      ],
      "biomes": [
        { "y": 4, "palette": ["minecraft:plains"] },
        { "y": 5, "palette": ["minecraft:plains", "minecraft:river"], "enc": "packed", "bits": 1, "data": "<base64>" }
      ]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `sections[].y` | section-Y (block Y divided by 16) |
| one palette entry, no `data` | every block in the section is that block (single-value container). Sections that are entirely air may be omitted |
| `enc: "packed"` | `data` is base64 of the bit array's `Uint32Array` bytes, little-endian low-word/high-word pairs; `bits` is `bitsPerValue`; values are palette indices |
| `enc: "u16"` | `data` is base64 of 4,096 little-endian unsigned 16-bit palette indices (direct container: the bot builds a local palette) |
| `biomes` | optional; same encoding with 64 entries per section |

Names are namespaced (`minecraft:stone`). Unknown state ids are forwarded as `unknown:<id>`.
Response:

```json
{"accepted": 42, "unchanged": 3, "rejected": 0, "ignored": 0}
```

- Limits, answered with `400`: more than 16 chunks per request; more than 64 sections per chunk; a
  palette over 4,096 entries; a body over 4 MB after decompression; unknown `format`; a `data` length
  that does not match `bits`.
- A malformed chunk inside an otherwise valid request increments `rejected`; the rest still apply.
- Non-Overworld dimensions are accepted and ignored (`ignored`).
- Each accepted or unchanged chunk marks that chunk **seen and loaded** for `agent` (the same store
  effect as `POST /seen`), so seen-only gating and last-seen views work unchanged.
- If the stored surface and composition are already equal, nothing is committed and no revision or
  generation is bumped (`unchanged`): two bots sending one chunk, or a resend after underground
  changes, cost nothing.
- A bot chunk is treated exactly like a plugin `POST /chunk_snapshot`: it replaces the chunk surface
  and composition with `source: "bot"`, never promotes the chunk to a hotter tier, and a newer save
  revalidates it with later overlays replayed. Without a save there is nothing to reconcile.
- The service rebuilds `WORLD_SURFACE` from the decoded blocks. A save stores `MOTION_BLOCKING`, so
  on columns topped by non-solid blocks the lowest counted section can differ by one. Parity is
  exact on the synthetic test world (`tests/test_bot_chunk.py`).

### Start modes and authentication

| Flag / env | Behaviour |
|---|---|
| `--world` / `CARTOGRAPHY_WORLD` | Save folder (unchanged); `region/` supplies base chunks |
| `--name` / `CARTOGRAPHY_NAME` | Save-less world identity: `maps/<name>.db`. `--world` may also be given; a save then revalidates bot chunks as above |
| `--db` / `CARTOGRAPHY_DB` | Explicit database path; wins over `--name` |
| `--token` / `CARTOGRAPHY_TOKEN` | Shared bearer token, sent as `Authorization: Bearer <token>`, required on every request when set |

DB path precedence: `--db` > `--name` > `maps/<level>-<worldid>.db` > `map_store.db`. A non-loopback
`--host` without a token is refused at startup; loopback without a token keeps working.

## Automatic perception

These endpoints back the automatic `$TOPOGRAPHY` prompt block. They are additive: the existing
on-demand queries above are unchanged.

### `GET /perception`

Compact LOD summary for a square chunk window centered on the chunk containing `(x, z)`.

Query params:

| Param | Type | Default | Notes |
|-------|------|---------|-------|
| `x`, `z` | int | required | Block coordinates; center chunk is `x>>4`, `z>>4` |
| `size_chunks` | int | 16 | Side length in chunks; `1..32`. Larger is rejected. |
| `materials` | 0/1 | 0 | Include per-cell dominant material |

Window bounds for a center chunk `(ccx, ccz)` and `size`:

```text
half = size // 2
cx1 = ccx - half; cx2 = cx1 + size - 1
cz1 = ccz - half; cz2 = cz1 + size - 1
```

For `16x16`, this is 8 chunks on the negative side and 7 on the positive side. The asymmetry is
intentional.

Response (`application/json`):

```json
{
  "service": "cartography",
  "center": {"x": 0, "z": 0},
  "size_chunks": 16,
  "window": {"cx1": -8, "cz1": -8, "cx2": 7, "cz2": 7},
  "resolution": "lod",
  "lod": [
    {
      "cell_chunks": 1,
      "cells_per_side": 4,
      "cx1": -2,
      "cz1": -2,
      "heights": [64, 65, null, 66],
      "water": [0, 0, 1, 0]
    },
    {
      "cell_chunks": 2,
      "cells_per_side": 4,
      "cx1": -4,
      "cz1": -4,
      "heights": [64, 65, 67, 68],
      "water": [0, 0, 0, 0]
    },
    {
      "cell_chunks": 4,
      "cells_per_side": 4,
      "cx1": -8,
      "cz1": -8,
      "heights": [63, 65, 70, 72],
      "water": [0, 1, 1, 0]
    }
  ],
  "surface": {"min_y": 63, "max_y": 78, "avg_y": 68.4, "relief": 15},
  "water": {
    "at_center": false,
    "nearest": {"dx": 48, "dz": 0, "distance": 48, "direction": "east"}
  },
  "biome": "minecraft:plains",
  "composition": {
    "natural": 12345,
    "engineered": 120,
    "liquid": 800,
    "air": 0,
    "water_fraction": 0.06,
    "top_blocks": [["minecraft:stone", 5000], ["minecraft:grass_block", 3200]],
    "artificiality_score": 0.01
  },
  "landforms": [
    {"kind": "cliff", "x": -40, "z": 0, "y_min": 70, "y_max": 110, "distance": 40.0,
     "direction": "west", "facing": "east", "inside": false, "nearest": {"x": -40, "z": 0},
     "size": {"length": 24, "width": 8, "area": 192}, "partial": false, "salience": 56.0}
  ],
  "missing_chunks": 0,
  "updated_at": 1699999999.0,
  "window_version": 42
}
```

Notes:

- `lod` is a list of levels, smallest cell size first. Each level covers a square of
  `cells_per_side * cell_chunks` chunks starting at chunk `(cx1, cz1)`.
- LOD levels overlap on purpose: `inner = min(4, size)` chunks at 1 chunk per cell, then each ring
  doubles (`2`, `4`, ...). The service returns full levels; the Mindcraft formatter emits the
  innermost level in full and only the outer ring of each subsequent level, so the prompt has no
  duplicate cells.
- Unique ring cells in the prompt: `16x16` → 40 (16 inner + 12 + 12); `32x32` → 52
  (16 + 12 + 12 + 12). `size_chunks` should be a power-of-two multiple of `inner` (`4, 8, 16, 32`);
  other sizes are clipped to the largest aligned level.
- `heights` and `water` are row-major within a level, north-to-south then west-to-east, length
  `cells_per_side²`. The arrays in the example are abbreviated for readability.
- Each cell aggregates the columns in its chunk range: `height` is the rounded average of non-null
  surface Y (or `null` when the cell has no data); `water` is `1` when the majority of columns are
  water.
- `materials` is optional per level and present only when `materials=1`.
- Composition, biome, artificiality, relief, and nearest water aggregate over the whole window, not
  per cell. `relief = max_surface_y - min_surface_y` over all non-null columns.
- Nearest water scans all water columns in the window and returns `dx`, `dz`, `distance`, and an
  8-way compass `direction`. `at_center` is true when the center column is water.
- `top_blocks` is capped (6 entries).
- `terrain.highest`, `terrain.lowest`, and `terrain.highest_by_direction` are computed from
  `ground_y` (solid ground), excluding water columns. They provide the peak direction/distance
  without requiring a line query.
- `composition.engineered_blocks` lists the top engineered/built block names and counts in the
  window (up to 5), so small player-built structures can appear even when they are not in the
  global `top_blocks`.
- `GET /area_composition` also returns `highest_ground` and `engineered_blocks` for the queried
  radius; `!topography` is a 1D line profile, not an area/peak query.
- `landforms` (additive) lists up to 8 named landforms in the window, ranked by salience over distance;
  same item shape as `GET /landforms` (see "Landforms"). Computed from the same window records (no extra
  load), memoised per window and store generation, and gated by `agent=` like the rest. Perception only:
  Mindcraft shows it in the prompt and never writes it to memory.
- `window_version` is the store's generation counter (incremented on diffs), used for client caching.
- `far` (additive) is present when `agent=` is given: the first up to 3 `GET /overview` features as
  `{"kind","direction","distance","y"?}` (high ground, water, built), reduced for the prompt's far
  line. Absent without `agent=`, and `[]` if the overview fails; perception itself is unaffected.
- Errors: `400` bad params, `500` internal; same `{"error": "..."}` shape as existing endpoints.
- Limits: `size_chunks <= 32`, `MAX_PERCEPTION_CHUNKS = 1024`.
- The service decodes only surface-adjacent sections (down to `surface_min - 32`), so this is
  terrain/build awareness, not a full underground map.

### `POST /position` (prefetch)

Fire-and-forget position update. The service schedules `touch_window` on a background worker,
debounces duplicate centers, and returns immediately. Stale requests (a newer center superseded)
are skipped. Any failure is logged and never returned. Mindcraft sends this at most once per chunk
tile per ~2 s while perception is enabled, without awaiting the response.

Request:

```json
{"x": 120, "z": -46, "size_chunks": 16}
```

Response:

```json
{"accepted": true, "queued": 42}
```

### `GET /overview` (long-range summary of what the agent has seen)

`GET /overview?x=&z=&radius=&agent=&dimension=` answers "what is out there" over a much larger radius
than the perception window, at a flat cost. `radius` (blocks) is 256..4096, default 2048; outside that
answers 400. Non-overworld dimensions get the same `{"unavailable":"dimension"}` answer as the other
terrain endpoints.

It reads only per-chunk summaries (`chunk_summary`, written at flush time), never chunk rows, and never
loads or promotes a chunk. With `agent=` only that agent's seen chunks appear, and a tracked agent's
unloaded chunks use the summary at its pinned revision (never newer than it last saw). Without `agent=`
every chunk's greatest revision is used.

Response:

```json
{
  "center": {"x": 0, "z": 0},
  "radius": 2048,
  "seen_chunks": 812,
  "summarised_chunks": 790,
  "cells": [
    {"dir": "north", "from": 128, "to": 512, "chunks": 37, "coverage": 0.41, "partial": true,
     "land": "forest", "water": 0.12, "y": [61, 95], "built": 0}
  ],
  "features": [
    {"kind": "high_ground", "x": 6400, "z": -250, "distance": 350, "direction": "north", "y": -51},
    {"kind": "water", "x": 6040, "z": 90, "distance": 400, "direction": "west", "chunks": 12,
     "centre": {"x": 6000, "z": 90}, "open": true},
    {"kind": "built", "x": 600, "z": 600, "distance": 600, "direction": "northeast", "chunks": 4}
  ],
  "window_version": 1234
}
```

- `cells`: one entry per 45-degree sector and distance ring (`128-512`, `512-1024`, `1024-2048`,
  `2048-4096`) that has at least one summarised chunk at least 128 blocks away. `coverage` is chunks
  over the sector-ring area (capped at 1, two decimals); `partial` when below 0.5. `water` is the water
  column fraction, `y` the lowest/highest natural ground, `land` `water` (>= 0.6), `forest`
  (tree share >= 0.3), else the dominant land class; `built` counts chunks with 8+ built columns.
- `features`: at most 8, in this order: up to 2 `high_ground` (a chunk whose natural ground stands at
  least 8 blocks above the median; a second only 256+ blocks from the first), up to 3 `water` bodies
  (4-connected chunks with 128+ water columns, at least 4 chunks; `open` when a 4-neighbour has no
  summary), up to 3 `built` areas (8-connected chunks with 8+ built columns). `direction` is one of the
  eight compass sectors; `distance` is rounded blocks.
- Size cap: if the encoded response exceeds 6000 bytes, cells of the outermost ring are dropped, then
  the next, and `"trimmed": true` is set.
- A cache of 32 results is keyed by agent, store generation, 64-block cell and radius.
- Errors: 400 bad params, 500 internal; `{"error": "..."}` as elsewhere.

The per-chunk summary table (`chunk_summary`) is additive: `(cx, cz, rev)` plus known/water columns,
natural-ground `h_min`/`h_max`/`h_sum`, `trees`, `built` and the dominant land `cls`. It is written in
the same transaction as the chunk rows, pruned to the current revision plus any revision an agent is
pinned at, and backfilled once at startup for existing databases. `SCHEMA_VERSION` is unchanged.

### `GET /grid` (Phase 3, optional detailed grid)
`GET /grid?x=&z=&size_chunks=` returns one cell per chunk with `min_y`, `max_y`, `avg_y`, `water`,
`material`, and `missing`. This is the on-demand detailed expansion and reuses the perception
aggregation helpers. Not required for Phase 1/2.

## Mindcraft prompt block format (reference)

Mindcraft formats the `/perception` JSON into compact text. Reference format:

```text
TOPOGRAPHY 16x16 chunks centered at (120,-46), LOD levels 1/2/4 chunks per cell
heights (NW->SE, y):
  inner 1ch/cell: 64 65 66 67 / 65 66 66 67 / ...
  ring 2ch/cell: 64 65 66 67 / ...
  ring 4ch/cell: 63 65 70 72 / ...
water (0 land, 1 water):
  inner: 0 0 0 0 / ...
  rings: 0 1 1 0 / ...
nearest water: east, 48 blocks; center: land
settlement: you are inside one (12 buildings; centre 15 blocks south)
relief: 15; biome: plains
composition: natural 92%, engineered 1%, water 6%; top: stone, grass_block, oak_planks; artificiality 0.01
missing: 0 chunks
```

The `settlement:` line comes from a second, optional request: Mindcraft calls
`GET /settlements?x=&z=&radius=96&agent=` right after `/perception` (400 ms timeout; skipped when
`/perception` answers `unavailable`) and shows the settlement the bot is inside, else the nearest one
(`confidence >= 0.3`; below `0.5` it is marked "weak signal"). If that request fails, the block is
injected without the line. It is placed before the relief/composition lines so truncation cannot cut it.

When `/perception` carries `far` entries and `topography_far_line` is not `false`, one more line is
added: `far (seen earlier): water 400 W; high ground 350 N y=-51; built 600 NE` (at most 3 items, at
most 140 characters). It is the first line dropped when the block is over its character cap.

Rules:

- Keep under the character budget; omit lines that are empty/zero.
- Use plain names without the `minecraft:` prefix.
- If the service fails or times out, inject nothing and log a warning (do not break the prompt).

## Mindcraft settings (defaults in code)

These use `??` fallbacks so no `settings.js` edit is required. Unknown/absent settings keep the
defaults below.

| Setting | Default | Notes |
|---------|---------|-------|
| `cartography_service_url` | `http://127.0.0.1:8090` | Resolved from `CARTOGRAPHY_SERVICE_URL` env, then `settings.js`, then the default |
| `cartography_enabled` | `false` | Master switch, off by default; `true` turns on the feed, the block, the topography commands, routing and prefetch |
| `topography_perception` | `true` | Inject `$TOPOGRAPHY` into every prompt (when the master switch is on) |
| `topography_debug` | `false` | Log perception fetch/cache timing, status, and failure reasons |
| `topography_render_chunks` | `16` | Perception window side length in chunks |
| `topography_perception_timeout_ms` | `1500` | Timeout for `/perception`; on error inject nothing |
| `topography_routing` | `true` | Route `!goToCoordinates` / `!goToRememberedPlace` over `GET /route` for far targets and places with no `y` (needs `cartography_enabled`; per-profile override). On by default since 2026-11-15 |
| `topography_perception_materials` | `false` | Request per-cell dominant materials |
| `topography_far_line` | `true` | Add the `far (seen earlier): ...` line from `/perception`'s `far` field (at most 3 items, at most 140 chars). It is the first line dropped when the block is over its cap. Set it to `false` (profile or settings) to omit it |
| `topography_feed` | `bot` | Forward loaded chunks to `POST /chunk_raw` (`bot`) or only report the seen ledger (`off`). `cartography_enabled: false` disables both |
| `cartography_token` | unset | Sent as `Authorization: Bearer` on every service request when set |

`cartography_enabled`, `topography_perception`, and `topography_debug` may be overridden per
Mindcraft profile (for example, a second agent with `cartography_enabled: false`).

The formatted block is capped at **1200 characters** by default (~300 tokens; configurable with the
Mindcraft setting `topography_max_chars`). Over the cap, Mindcraft drops whole lowest-value lines first
(coarse-ring water, finer-ring water, inner water, coarse-ring heights, highest by direction, near-ring
heights, highest ground) and keeps the header, compass, inner heights, nearest water, settlement,
biome/relief, composition and missing/unexplored lines. The block
is injected into the prompt (not history), so it does not accumulate. Mindcraft caches the formatted
block per prompt/position for ~1 s so prompt retries do not re-fetch.

## Terrain data model (`terrain_y`)

Each stored chunk's `surface` has 256 entries per array (index `x%16 + (z%16)*16`): `y`/`materials`
(topmost non-air), `ground_y`/`ground_materials`, `water`, and `terrain_y`/`terrain_materials`.

`terrain_y` is the topmost **natural ground-forming** block (grass, dirt, sand, gravel, stone family,
ores, snow, ...), scanning down past roofs, floors, decks, logs, leaves, plants and liquids; under
water it is the bed, and `null` when nothing natural lies below. The whitelist is
`terrain_rules.py` in the service and `ColumnSurface` in the plugin and must stay identical
(`tools/check_terrain_rules_sync.py`). Player-placed stone-family blocks still count.

`/perception` additionally returns `terrain.highest`, `terrain.lowest`, `terrain.highest_by_direction`
and `terrain.relief`, computed from `terrain_y`.

## Live updates (plugin -> service), dimension and trust

- Only `minecraft:overworld` is stored. Every payload carries `"dimension"`; other dimensions are
  accepted and ignored (`/diff` adds `"ignored": n`). Any GET with `dimension=` other than overworld, and
  `POST /position`, answer `{"service":"cartography","unavailable":"dimension","dimension":...}`.
- `place` deltas add `old_block`. `break` deltas add `surface_y` (true column surface),
  `new_top_y/new_top_block`, `new_ground_y/new_ground_block`, `new_terrain_y/new_terrain_block`
  (may be null) and optionally `new_block`. A break below the recorded surface never lowers it.
  Deltas whose block id did not change (door toggles, fluid levels) are not sent.
- `POST /chunk_snapshot`: `{"dimension","snapshots":[{"cx","cz","surface":{y,materials,ground_y,
  ground_materials,water,terrain_y,terrain_materials},"counts"?:{class:n}}]}` -> `{"accepted",
  "rejected","ignored"}`. It replaces the chunk's surface (idempotent, self-healing). The plugin sends
  one after a quiet period (`snapshotDebounceMs`) following a change or, with `trackChunkLifecycle`
  (default true), a chunk load/generation; on unload of a changed chunk; and again when a post fails.
  The plugin no longer sends `chunk_load`/`chunk_unload` deltas (the service still accepts and ignores them).
- Only chunks whose NBT `Status` is `minecraft:full` are loaded from the save; others count as
  `missing_chunks`. Cached rows are revalidated against the region-file timestamp and reloaded when the
  save is newer; the cache is stamped with a schema version and `world_id`.

## Build-site and bridge queries

`GET /flat_areas?x=&z=&radius=64&width=9&depth=9&max_delta=1` (radius <= 128) returns up to 5
non-overlapping flat footprints: `{"center","radius","width","depth","max_delta","areas":[{"x1","z1",
"x2","z2","y_min","y_max","center_y","distance","direction","water_columns","floor_material"}],
"missing_chunks"}`. Unknown columns, engineered floors and built surfaces above the ground disqualify.

`GET /bridge_span?x=&z=&dir=north|south|east|west` (or `x2=&z2=`) returns `{"start","dir","near_shore",
"far_shore","span","water_y","water":[{"x","z","y","bed_y","depth","material"}],"depth":{min,max,avg},
"bed_y":{min,max},"longest_water_run","water_runs","deck_y","caution":[...],"missing_chunks"}`.
Known limit: an existing plank deck over water reads as land.

## Landforms

`GET /landforms?x=&z=&radius=128&agent=&dimension=` (`radius` 16..256, else 400) ->
`{"center","radius","landforms":[...],"missing_chunks","unexplored_chunks"}`, at most 12 items, ranked by
salience / (distance + 40), at most 2 per kind (3 cliffs). Item:
`{"kind","x","z","y_min","y_max","distance","direction","nearest":{"x","z"},"inside","size":{"length",
"width","area"},"partial","salience"}` plus `facing` (cliff: compass direction it drops towards, when
the slope is clear), `prominence` (hill, mountain, ridge, plateau: rise over the local base) and `depth`
(valley, canyon: rim y minus floor y).

- `kind`: `cliff | canyon | hill | mountain | ridge | plateau | valley | plain | lake | river | ocean | beach | island`.
- `distance`/`direction`/`nearest` refer to the nearest point of the landform (8-way compass, north = -z;
  `here` and `inside: true` when the query point is on it). `x`,`z` is a representative point (peak for
  hill/mountain/ridge, centre of the band or body otherwise). `y_min..y_max`: cliff bottom/top; hill,
  mountain, ridge, plateau: local base to top; valley: floor to flank height; canyon: floor to rim (`size.width` is rim to rim); beach: lowest to highest
  sand-strip terrain; island: lowest to peak terrain; water: surface level.
- `canyon`: a cut at least 24 deep with walls on both opposing sides within 20 blocks (<= ~40 wide, >= 32
  long); it takes precedence, so the same ground is never also a `valley`. `beach`: natural sand/red_sand
  (`terrain_materials`; gravel is not beach) next to a lake-sized water body (>= 192 blocks^2), low relief,
  strip <= 24 wide and >= 24 long. `island`: land of 400..20000 blocks^2 whose whole neighbour ring is
  confirmed water; a neighbour that is missing, unseen (gated) or outside the radius disqualifies it, so an
  island is never `partial`. Landforms are perception only and never stored in bot memory.
- Built from natural `terrain_y` (`ground_y` fallback) and the water flags, so roofs, trees and decks are
  not terrain. Only known chunks count: nothing is inferred across a missing/unseen chunk, and the edge
  of the known data is never a cliff or shoreline. A landform touching unknown ground has
  `"partial": true` (its size is a lower bound). `missing_chunks` / `unexplored_chunks` as elsewhere.
- Non-overworld `dimension` -> `{"unavailable": "dimension"}`. Thresholds are constants at the top of
  `landforms.py`.
- Mindcraft: `landforms: cliff 40 W y70-110; hill 120 N y96; lake 30 E` (protected line after the
  settlement line; max 4; plains only when nothing else) and the `!landforms(x, z, radius)` command.

## Routing

`GET /route?x=&z=&x2=&z2=&agent=[&seed=][&dimension=][&shore=auto|left|right][&shore_from=]` plans a long-range walk from block (`x`,`z`) to
(`x2`,`z2`). `agent` is **required** (400 without it): the plan uses only chunks that agent has reported
via `POST /seen`, through the gated store view. Unseen chunks are never read and never traversed.
Coordinates beyond +-30,000,000 or non-numeric -> 400. Non-overworld `dimension` ->
`{"unavailable":"dimension"}`.

A route may cross at most **2 consecutive water cells** (a cell is 4x4 blocks); wider water is
impassable, like a cliff, so the router never plans a long swim. Bridges are unaffected.

Response:
`{"status","reason","start":{x,z,y},"target":{x,z,y|null,"known"},"waypoints":[...],"bridges":[...],
"detours":[...],"distance","remaining","nodes","ms","unexplored_chunks","note"?,"widened"?}`.
Every non-complete result also carries `"blocked_by": "unexplored" | "water" | "terrain"`: the search
stopped at an unseen chunk, at water, or at terrain.

- `status`: `complete` (the last waypoint is the target, `kind: target`, with ground `y` from the map),
  `partial` (route as far as is known/allowed; the last waypoint is `kind: frontier`; `remaining` is the
  straight-line distance left) or `blocked` (no usable first step; `waypoints` empty).
- `reason` (non-complete): `unexplored` (the way or the target lies in chunks the agent has not seen or
  the service has no data for yet; nothing is implied about them), `blocked` (cliffs/lava/steps wall the
  way in the known area), `water` (water blocks the way; reported instead of `blocked`/`unexplored`
  when `blocked_by` is `water`), `limit` (node/time/chunk bound hit), `distance` (target beyond 1536
  blocks: routed toward it), `start_unexplored` (the start chunk is not in the seen ledger).
- Water run: a cell counts as water when at least half its known columns are water. At most 2
  consecutive water cells are enterable, so a 5-block stream can be crossed but a lake cannot; a target
  in the middle of wide water is reported as not reachable.
- Shore mode (optional): `shore=auto` lets the service pick a side when water blocks the way; `left`
  means water on the bot's left hand, `right` the other side. `shore_from` is the straight-line distance
  to the target when the current shore episode began; when the plan can make at least 32 blocks of real
  progress past it, the normal partial result is returned instead. A shore plan has
  `status: partial`, `reason: water`, `blocked_by: water`, `shore_follow` waypoints (every 16 blocks and
  the last traced cell, each with `note: "along the water's edge"`), and
  `"shore": {"side","cells","length","end"}` where `end` is `step`, `frontier`, `dead_end` or `loop`.
  Bad `shore` / `shore_from` values answer 400. Without `shore=` the old behaviour is unchanged.
- Waypoint: `{"x","y","z","kind","note"?,"bridge"?,"swim"?}`. `y` is the **ground block** (stand at
  `y+1`); on a bridge it is the **deck** `y`, not the natural `terrain_y` below. `kind`: `bend`, `shore`
  (water's edge; `swim` = blocks of water ahead when entering, else "back on dry land"), `shore_follow`,
  `crest`, `path`, `bridge_start`, `bridge_deck`, `bridge_end` (carry `bridge: "b1"`), `leg` (spacing),
  `target`, `frontier`. Waypoints are never placed in water (except `target` and `bridge_*` on the deck)
  and never on a cliff edge; positions inside a cell are jittered with a seeded RNG (`seed=`, default a
  hash of the request, so identical requests give identical routes).
- `bridges`: only those the route uses: `{"id","a":{x,y,z},"b":{x,y,z},"length","width","deck_y"}`.
  Detected directly from columns anywhere seen: a straight, connected strip of elevated built blocks
  (<= 6 wide, >= 5 long, deck at least 2 above `terrain_y`) with walkable natural ground within 2 blocks of
  deck height at **both** ends; a pier or roof does not count. Diagonal strips are not detected.
- `detours`: `[{"around":"water|cliff","cells":n}]` when the straight line would have crossed it.
- Costs (constants at the top of `route.py`): water x5, buildings x2.5, steep ground
  `+0.25` per block of in-cell relief above 2 and `+1.5` per block of height step above 1; every block of
  height gained (uphill only) adds `+2`, so a long even climb over a peak costs more than a valley round
  it; bridge x0.6, path
  x0.55 (`dirt_path`, cobblestone, mossy cobblestone, stone bricks surface blocks). In-cell relief >= 7 or a
  step >= 8 between cells is not walkable; lava is never entered. The cell the bot stands in can always be
  left (except lava), even when it rates as a cliff or every step out is >= 8. Terrain uses natural
  `terrain_y`; bridge cells use the deck `y`. Search area: the start/target box padded by 96 blocks or a
  quarter of the straight distance (at most 384); a search that runs out of cells against that edge is
  retried once with twice the pad (`"widened": true`). Work bound: 40,000 A* expansions, 1.5 s, 3,000
  chunks, 4-block cells, weighted A*.
- Freshness: the plan reads whatever the store holds now. A chunk just explored appears once the service
  has surface data for it (save-file load after the server writes the chunk, or a plugin snapshot; see
  `snapshotDebounceMs` / `trackChunkLifecycle`); until then it is reported as `unexplored`.
- Mindcraft: `!goToCoordinates` and `!goToRememberedPlace` use it for far targets (beyond ~96 blocks) or
  places with no `y`, only when `cartography_enabled` is not false; the pathfinder still walks every leg
  (`src/agent/library/route_travel.js`), replanning at each waypoint.

## Settlements, survey, seen ledger

- `GET /settlements?x=&z=&radius=96&agent=` -> `{"center","radius","settlements":[{"id","kind":
  "settlement|outpost","center","bbox","building_count","built_columns","area","tallest","distance",
  "direction","inside","confidence","evidence":[...]}],"missing_chunks","unexplored_chunks"}`.
  Low confidence means a weak signal, not a small settlement.
- `GET /survey?x1=&z1=&x2=&z2=&agent=` (max 160x160 blocks, 400 chunks) -> `{"bbox","building_count",
  "buildings":[{"bbox","footprint_area","height","roof_material","wall_materials"}],"palette":{walls,
  roof,glass,doors,fences,lighting,paths,other_built},"dimensions","landmarks","summary",
  "sampled_chunks","missing_chunks","unexplored_chunks"}`. Reads the saved blocks, not live diffs.
  Without a save it answers `{"unavailable": "no save"}` (stage 2 may read bot-fed sections instead).
- `POST /seen` `{"agent","dimension","chunks":[[cx,cz],...],"unloaded":[[cx,cz],...],"replace":false}`
  -> `{"accepted":n}` (plus `"unloaded":m` when `unloaded` was sent); max 5000 per list.
  `chunks` = chunks the agent has loaded now, `unloaded` = chunks it no longer has loaded; both count as
  seen, and a chunk in both lists ends unloaded. `unloaded` or `replace` switch the agent to the
  *tracked* protocol (frozen views, below). `replace: true` makes `chunks` the agent's complete loaded
  set (a resync). `unloaded` must be a list when present (400 otherwise). Old clients sending only
  `chunks` keep working and always see live data. In bot-fed mode this is the unload and resync
  channel; a `POST /chunk_raw` chunk is also seen and loaded for its `agent`.
  `GET /seen_stats?agent=` -> `{"agent","chunks","first_seen","last_seen"}`.
- **Gating:** every terrain GET accepts `agent=`. Chunks the agent has not reported as seen are treated
  as missing (counted in `unexplored_chunks`; `/recall` and `/chunk` answer 404). Without `agent=`
  behaviour is unchanged.
- **Last-seen map (tracked agents):** with `agent=`, a chunk the agent currently has loaded is live; a
  seen chunk it does not have loaded is returned as it was when the agent last had it loaded (the
  record carries `"frozen": true` on `/chunk`; `/perception`, `/area_composition`, `/settlements`,
  `/landforms`, `/route`, `/survey`, ... all use it, and `/survey` does not read the save for frozen
  chunks). Builds made while the agent is away therefore stay invisible until it loads the chunk
  again. Client duty: send `replace: true` on the first report, after a failed POST, after
  respawn/dimension change, on disconnect (empty) and about every 30 s; a tracked agent silent for
  120 s is treated as having unloaded everything. Service duty: per-chunk revisions with
  copy-on-write history (`chunk_history`, additive SQLite table; `seen.frozen_rev/frozen_at` and
  `chunks.rev` columns), pruned when no agent references a revision, capped at 64 MiB / 8 revisions
  per chunk (an agent pinned on an evicted revision sees the chunk as missing, never as live). A chunk
  seen while the service had no data takes the first data for it, if that arrives within 600 s of the
  unload or is read from the save with an on-disk timestamp at or before unload + 600 s; later data is
  missing for that agent. Such chunks are resolved in the background (bounded batches, retried with
  backoff until 600 s after the unload, also after a restart), not only when queried. Unload and load reports arrive up to ~2 s late, so changes in that
  window show as live (or, for a load, stay frozen) briefly. After a service restart nothing is
  loaded until the next report; rows that were loaded are frozen at the revision current at start.

## Storage

The service keeps one SQLite database per world (`maps/<level>-<worldid>.db`);
`--db` / `CARTOGRAPHY_DB` override it, and `--name` / `CARTOGRAPHY_NAME` names a save-less database
(`maps/<name>.db`). Each bot keeps `bots/<name>/memory.db` (and its other files) per
world under `bots/<name>/worlds/<world_name>/` when `world_name` / `MINDCRAFT_WORLD` is set.
