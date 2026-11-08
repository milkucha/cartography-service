# Topography Service

Python map service for Mindcraft topography. Loads a Minecraft save, keeps a tiered world model
(hot/warm in memory, cold in SQLite), applies live block-change diffs from the Fabric plugin, and
serves compact JSON queries to the agent.

Standard library only — Python 3.10+ is the only requirement.

## Run

```powershell
python main.py --world "C:\path\to\server\world" --host 127.0.0.1 --port 8090
```

Options (or the matching environment variable):

| Flag | Env | Default | Description |
|------|-----|---------|-------------|
| `--world` | `TOPOGRAPHY_WORLD` | `world` | World/save folder containing `region/` |
| `--host` | `TOPOGRAPHY_HOST` | `127.0.0.1` | Bind address |
| `--port` | `TOPOGRAPHY_PORT` | `8090` | HTTP port |
| `--db` | `TOPOGRAPHY_DB` | `maps/<level>-<worldid>.db` | SQLite store path. Default is one database per world (next to `main.py`), so chunk caches and per-agent seen ledgers never mix between worlds. Falls back to `map_store.db` when there is no readable save. |
| `--preload` | `TOPOGRAPHY_PRELOAD` | off | Decode every chunk at startup |
| `--preload-limit` | `TOPOGRAPHY_PRELOAD_LIMIT` | `0` | Max chunks to preload (0 = all) |

If no `region/` folder is found, the service starts in **diff-only mode**: it still accepts live
changes from the plugin, but cannot answer queries for unvisited areas until they arrive.

Chunks are otherwise loaded lazily from the save on first query, so preloading is only useful for
small worlds or benchmarks.

## HTTP API

Full request/response schemas and the diff contract:

- Repo contract: [`../CONTRACT.md`](../CONTRACT.md)
- Endpoints: `/health`, `/perception`, `/topography`, `/area_composition`, `/shoreline`, `/recall`,
  `/chunk`, `/flat_areas`, `/bridge_span`, `/settlements`, `/landforms`, `/survey`, `/route`, `/seen_stats`, and
  `POST /diff`, `POST /chunk_snapshot`, `POST /position`, `POST /seen`.
- `terrain_y` / `terrain_materials` (per column) are the topmost *natural ground-forming* block
  (grass, dirt, sand, gravel, stone family, ores, snow, ...), scanning down past roofs, floors,
  decks, logs, leaves, plants and liquids. The whitelist lives in `terrain_rules.py` and is mirrored
  in the plugin (`tools/check_terrain_rules_sync.py` fails if they drift).

## Settlements, survey and the seen ledger

| Endpoint | Purpose |
|----------|---------|
| `GET /settlements?x=&z=&radius=96&agent=` | Detect settlements/outposts from cached surface data (radius 16..256, max 8 results, stable `id`). |
| `GET /landforms?x=&z=&radius=128&agent=` | Named landforms (cliff, hill, mountain, ridge, plateau, valley, plain, lake, river, ocean) from `terrain_y` and water flags: kind, direction/distance to the nearest point, y range, representative x/z, size, cliff facing, `partial`. Radius 16..256, max 12 results. Also returned as `landforms` in `/perception`. |
| `GET /survey?x1=&z1=&x2=&z2=&agent=` | Architectural digest of a bbox (max 160x160 blocks / 400 chunks, else 400): buildings, material palette by role, dimensions, landmarks, `summary`. |
| `GET /route?x=&z=&x2=&z2=&agent=` | Long-range route over every chunk the agent has seen (coarse 4-block grid A*, bounded by nodes/time/distance): waypoints marked bend / shore / crest / path / bridge_start / bridge_deck / bridge_end / leg / target / frontier, bridges (straight narrow built strip with walkable land at both ends, detected from columns; piers rejected), `status` complete or partial (`unexplored`, `limit`, `distance`, `blocked`). `agent` is required. |
| `POST /seen` `{"agent","dimension","chunks":[[cx,cz],...],"unloaded":[[cx,cz],...],"replace":false}` | Record chunks an agent has loaded (`chunks`) and no longer has loaded (`unloaded`), max 5000 per list; both count as seen. `unloaded` / `replace` switch the agent to frozen views (see "Last-seen map"); `replace: true` means `chunks` is the agent's complete loaded set (heartbeat/resync). Old clients sending only `chunks` still work and stay live. Non-overworld => `{"accepted":0,"ignored":n}`. |
| `GET /seen_stats?agent=` | `{"agent","chunks","first_seen","last_seen"}`. |

Detection (`settlements.py`): a structure column is a built surface material (planks, stairs,
slabs, bricks, wool, concrete, glass, cobblestone, logs) at least 2 blocks above `terrain_y`
(rows cached before that field existed fall back to a local-minimum baseline). A built column
with water on both sides within 3 blocks (a bridge deck) is measured from the water surface. Columns form buildings (trees, thin strips and scatter are dropped); buildings within
32 blocks form a cluster: 3+ buildings, or 2 plus domestic indicators (bed, bell, chest,
furnace, crafting table ...), is a `settlement`, otherwise an `outpost`. Rails and torches are
never evidence. Low confidence means a weak signal, not a confirmed small or absent settlement.
`id` hashes the cluster centre rounded to 32 blocks.

`/survey` reads the REAL block sections of the save (not live diffs): the palette and special
landmarks (bell, beacon, lectern, enchanting table, brewing stand) lag behind blocks placed since
the server last saved the chunk. Building geometry uses the store.

Gating: every terrain GET (`/perception`, `/topography`, `/area_composition`, `/shoreline`,
`/recall`, `/chunk`, `/flat_areas`, `/bridge_span`, `/settlements`, `/landforms`, `/survey`) accepts
`agent=`. With it, chunks missing from that agent's ledger are treated as missing (counted in
`missing_chunks` and `unexplored_chunks`) and their data is never returned (`/chunk` and
`/recall` give 404). Gating is implemented once in `store_view.StoreView`. Without `agent=`
behaviour is unchanged. The ledger is the `seen` SQLite table (schema v3; v2 databases migrate
in place without losing chunk rows).

### Last-seen map

For a gated request each chunk is shown as: **live** while the agent has it loaded; **frozen** (as it
was when the agent last had it loaded, `"frozen": true` on `/chunk`) once it is not loaded and the
chunk has changed since; **unexplored** if never seen. Builds made while the bot is away therefore
do not reach `/area_composition`, `/settlements`, `/landforms`, `/route`, `/survey` (frozen chunks are
not read from the save) or `/perception` until the bot loads the chunk again.

How: each stored chunk has a `rev` (base = 1; every diff, snapshot or reload from a newer save takes
a new unique one from a persisted counter). An agent using the tracked protocol is *pinned* at the
chunk's `rev` when a chunk leaves its loaded set (`seen.frozen_rev`). Before a chunk changes, if an
agent is pinned at its current `rev`, its old stored form is saved once, zlib-compressed, in the
additive `chunk_history` table, and deleted as soon as no pin references it. Chunks nobody is pinned
on cost one dict lookup per diff. The loaded set is in memory only: after a restart nothing is loaded
until the client's next report (`replace`, every ~30 s), so rows left "loaded" are frozen at the
revision current at start; this also upgrades pre-existing `seen` rows (no data is lost or invented,
and nothing looks different until something changes).

Details: a chunk first seen when the store had no data (pin rev 0) adopts the first data arriving
within `FIRST_DATA_GRACE_S` (600 s) of the unload; later data is reported as missing for that agent,
never as live. A tracked agent silent for `LOADED_TTL_S` (120 s) is treated as having unloaded
everything. Unload reports arrive up to ~2 s late, so changes in that window still show. History is
capped at `HISTORY_MAX_BYTES` (64 MiB compressed) and `HISTORY_MAX_PER_CHUNK` (8); the oldest rows are
evicted first and an agent pinned on an evicted revision sees that chunk as missing (`/health`:
`history_evicted`) until it loads the chunk again. Frozen records are shared read-only, never put in the
hot/warm tiers, and the query caches key on `rev` so views of different agents never mix.

## Tests

```powershell
python -m unittest discover -s tests -v
```

The suite builds a synthetic coast world in a temp directory, so it needs no Minecraft server.

## Layout

| File | Purpose |
|------|---------|
| `main.py` | Entry point, argument parsing, preload, lifecycle |
| `api.py` | HTTP server and endpoints |
| `loader.py` | Save/region scanning and chunk decoding |
| `map_store.py` | Hot/warm tiers, SQLite cold tier, diff application |
| `topography.py` | Heightmaps, shoreline, composition, recall |
| `composition.py` | Block classification and indicators |
| `mc_nbt.py`, `region.py`, `palette.py` | Anvil region / NBT / palette decoders |
| `mc_blocks.py` | Block classification tables |
| `landforms.py` | Landform classifier (thresholds at top of file) |
| `route.py` | `/route` planner (costs and limits at top of file) |
| `settlements.py`, `survey.py`, `store_view.py` | Settlement detection, survey digest, per-agent gated store view |

`mc_nbt.py`, `region.py`, `palette.py`, and `composition.py` are adapted from the local
`Archeology/tools` scripts. `mc_blocks.py` provides the block classification tables used by the
service.

## License

PolyForm Noncommercial License 1.0.0 — see [LICENSE](./LICENSE). Free for noncommercial use; commercial use needs a separate licence from the author.
