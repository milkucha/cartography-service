# Topography service: reference

Python map service for Mindcraft topography. Loads a Minecraft save, keeps a tiered world model
(hot/warm in memory, cold in SQLite), applies live block-change diffs from the Fabric plugin **or
chunk data forwarded by a Mindcraft bot**, and serves compact JSON queries to the agent.

Standard library only — Python 3.10+ is the only requirement.

## Run

With a save (the plugin or the bot can feed changes):

```powershell
python main.py --world "C:\path\to\server\world" --host 127.0.0.1 --port 8090
```

Without a save — the bot fills the map over `POST /chunk_raw`:

```powershell
python main.py --name survival1 --host 127.0.0.1 --port 8090
# ...or: $env:TOPOGRAPHY_NAME="survival1"; python main.py
```

Options (or the matching environment variable):

| Flag | Env | Default | Description |
|------|-----|---------|-------------|
| `--world` | `TOPOGRAPHY_WORLD` | `world` | World/save folder containing `region/` |
| `--name` | `TOPOGRAPHY_NAME` | — | Save-less world identity: uses `maps/<name>.db`. Does not require a save; `--world` still applies if given |
| `--host` | `TOPOGRAPHY_HOST` | `127.0.0.1` | Bind address |
| `--port` | `TOPOGRAPHY_PORT` | `8090` | HTTP port |
| `--db` | `TOPOGRAPHY_DB` | `maps/<level>-<worldid>.db` | SQLite store path. Wins over `--name`. Default is one database per world (next to `main.py`), so chunk caches and per-agent seen ledgers never mix between worlds. Falls back to `map_store.db` when there is no readable save. |
| `--token` | `TOPOGRAPHY_TOKEN` | — | Shared bearer token (`Authorization: Bearer ...`). Mandatory when `--host` is not loopback; the service refuses to start otherwise. Loopback without a token is unchanged |
| `--preload` | `TOPOGRAPHY_PRELOAD` | off | Decode every chunk at startup |
| `--decode-workers` | `TOPOGRAPHY_DECODE_WORKERS` | `2` | Worker processes that decode `POST /chunk_raw` chunks, so a bot feeding new terrain does not slow `/route` and `/perception` (decoding is CPU-bound Python). `0` decodes in the request thread |
| — | `TOPOGRAPHY_SLOW_MS` | `200` | Log requests, and store-lock waits or holds, slower than this (`topography.timing` warnings) |
| `--preload-limit` | `TOPOGRAPHY_PRELOAD_LIMIT` | `0` | Max chunks to preload (0 = all) |

DB path precedence: `--db` > `--name` > save-derived `maps/<level>-<worldid>.db` > `map_store.db`.

If no `region/` folder is found, the service starts in **bot-fed / diff-only mode**: it still accepts
`POST /chunk_raw` (bot) and `/diff` + `/chunk_snapshot` (plugin), but cannot answer queries for
unvisited areas until they arrive. `/survey` answers `{"unavailable": "no save"}` in that mode.

Chunks are otherwise loaded lazily from the save on first query, so preloading is only useful for
small worlds or benchmarks.

## HTTP API

Full request/response schemas and the diff contract:

- Repo contract: `CONTRACT.md` in the umbrella repo
- Endpoints: `/health`, `/perception`, `/overview`, `/topography`, `/area_composition`, `/shoreline`, `/recall`,
  `/chunk`, `/flat_areas`, `/bridge_span`, `/settlements`, `/landforms`, `/survey`, `/route`, `/seen_stats`, and
  `POST /diff`, `POST /chunk_snapshot`, `POST /chunk_raw`, `POST /position`, `POST /seen`.
- `terrain_y` / `terrain_materials` (per column) are the topmost *natural ground-forming* block
  (grass, dirt, sand, gravel, stone family, ores, snow, ...), scanning down past roofs, floors,
  decks, logs, leaves, plants and liquids. The whitelist lives in `terrain_rules.py` and is mirrored
  in the plugin (`tools/check_terrain_rules_sync.py` fails if they drift).

### Bot-fed ingestion (`POST /chunk_raw`)

Mindcraft forwards loaded chunks with name palettes plus packed index arrays. The service rebuilds
the Anvil-shaped root (`bot_chunk.py`), runs the same `decode_surface` / `analyze_chunk` /
`build_chunk_record` path as a save load, and stores the record with `source: "bot"`. A bot chunk is
treated exactly like a plugin `POST /chunk_snapshot`: it replaces the chunk surface and composition,
never promotes the chunk to a hotter tier, and a newer save revalidates it with later overlays
replayed. Unchanged re-forwards (two bots, or a resend after underground changes) are counted as
`unchanged` and bump nothing. Every accepted chunk also marks the chunk seen **and loaded** for the
sending agent (same store method as `POST /seen`), so per-agent gating and last-seen views work.

The heightmap is rebuilt as `WORLD_SURFACE` from the decoded blocks. A save stores
`MOTION_BLOCKING`; on columns topped by non-solid blocks the lowest counted section can therefore
differ by one. The parity test (`tests/test_bot_chunk.py`) holds exactly on the synthetic world.

Limits (400): more than 16 chunks per request, more than 64 sections per chunk, a palette over 4096
entries, a body over 4 MB after decompression, an unknown `format`, or a `data` length that does not
match `bits`. Non-Overworld dimensions are accepted and ignored. `Content-Encoding: gzip` is
supported. `/survey` needs the save's 3D sections: without a save it answers
`{"unavailable": "no save"}`.

Authentication: when `--token` / `TOPOGRAPHY_TOKEN` is set, every request needs
`Authorization: Bearer <token>` (401 otherwise). A non-loopback bind without a token is refused at
startup.

## Settlements, survey and the seen ledger

| Endpoint | Purpose |
|----------|---------|
| `GET /settlements?x=&z=&radius=96&agent=` | Detect settlements/outposts from cached surface data (radius 16..256, max 8 results, stable `id`). |
| `GET /landforms?x=&z=&radius=128&agent=` | Named landforms (cliff, canyon, hill, mountain, ridge, plateau, valley, plain, lake, river, ocean, beach, island) from `terrain_y`, terrain materials and water flags: kind, direction/distance to the nearest point, y range, representative x/z, size, cliff facing, canyon/valley depth, `partial`. Radius 16..256, max 12 results. Also returned as `landforms` in `/perception`. |
| `GET /survey?x1=&z1=&x2=&z2=&agent=` | Architectural digest of a bbox (max 160x160 blocks / 400 chunks, else 400): buildings, material palette by role, dimensions, landmarks, `summary`. Needs the save's sections; without a save it answers `{"unavailable":"no save"}`. |
| `GET /route?x=&z=&x2=&z2=&agent=[&seed=][&shore=auto|left|right][&shore_from=]` | Long-range route over every chunk the agent has seen (coarse 4-block grid A*, bounded by nodes/time/distance): waypoints marked bend / shore / shore_follow / crest / path / bridge_start / bridge_deck / bridge_end / leg / target / frontier, bridges (straight narrow built strip with walkable land at both ends, detected from columns; piers rejected), `status` complete or partial (`unexplored`, `limit`, `distance`, `water`, `blocked`), and `blocked_by` (`unexplored` / `water` / `terrain`) on partial results. A route may cross at most `MAX_WATER_RUN = 2` consecutive water cells; wider water is impassable. `shore=` lets the client follow a shoreline (`shore_follow` waypoints plus a `shore` object with side/cells/length/end); `shore_from` is the straight-line distance at the start of the current shore episode and ends it once real progress is available. `agent` is required. |
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

### Long-range overview (`GET /overview`)

`GET /overview?x=&z=&radius=&agent=&dimension=` summarises everything the agent has seen within
`radius` blocks (256..4096, default 2048) at a flat cost. It never reads, decodes or promotes a chunk:
every chunk record writes a small summary into the additive `chunk_summary` table at flush time
(`overview.summarize`: known/water columns, natural-ground min/max/sum, tree and built columns, the
dominant land class), and an existing database is backfilled once at startup. Reads go through
`StoreView` so only seen chunks appear, and a tracked agent's unloaded chunks use the summary at its
pinned `rev` (never newer than it last saw).

The response has coarse `cells` (one per 45-degree sector and distance ring, with coverage, water
fraction, land class, y range, built count and `partial`), up to 8 `features` (`high_ground`, `water`
bodies with `open`, `built` areas), `seen_chunks`, `summarised_chunks`, `window_version`, and
`trimmed` when the outermost ring was dropped to stay under 6000 bytes. `/perception` adds a `far`
list of the first overview features when `agent=` is given. The cache is a 32-entry LRU keyed by
agent, store generation, 64-block cell and radius.

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
| `bot_chunk.py` | `POST /chunk_raw` payload → Anvil-shaped root (name palettes, packed indices, WORLD_SURFACE heightmap) |
| `mc_nbt.py`, `region.py`, `palette.py` | Anvil region / NBT / palette decoders |
| `mc_blocks.py` | Block classification tables |
| `landforms.py` | Landform classifier (thresholds at top of file) |
| `route.py` | `/route` planner (costs and limits at top of file) |
| `overview.py` | `/overview`: per-chunk summaries, sectors/rings, features, cache (limits at top of file) |
| `settlements.py`, `survey.py`, `store_view.py` | Settlement detection, survey digest, per-agent gated store view |

`mc_nbt.py`, `region.py`, `palette.py`, and `composition.py` are adapted from the local
`Archeology/tools` scripts. `mc_blocks.py` provides the block classification tables used by the
service.

## License

MIT — see [LICENSE](../LICENSE).
