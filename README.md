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
| `--db` | `TOPOGRAPHY_DB` | `map_store.db` | SQLite cold-store path |
| `--preload` | `TOPOGRAPHY_PRELOAD` | off | Decode every chunk at startup |
| `--preload-limit` | `TOPOGRAPHY_PRELOAD_LIMIT` | `0` | Max chunks to preload (0 = all) |

If no `region/` folder is found, the service starts in **diff-only mode**: it still accepts live
changes from the plugin, but cannot answer queries for unvisited areas until they arrive.

Chunks are otherwise loaded lazily from the save on first query, so preloading is only useful for
small worlds or benchmarks.

## HTTP API

Full request/response schemas and the diff contract:

- Repo contract: [`../CONTRACT.md`](../CONTRACT.md)
- Endpoints: `/health`, `/topography`, `/area_composition`, `/shoreline`, `/recall`, `/chunk`,
  and `POST /diff`.

## Settlements, survey and the seen ledger

| Endpoint | Purpose |
|----------|---------|
| `GET /settlements?x=&z=&radius=96&agent=` | Detect settlements/outposts from cached surface data (radius 16..256, max 8 results, stable `id`). |
| `GET /survey?x1=&z1=&x2=&z2=&agent=` | Architectural digest of a bbox (max 160x160 blocks / 400 chunks, else 400): buildings, material palette by role, dimensions, landmarks, `summary`. |
| `POST /seen` `{"agent","dimension","chunks":[[cx,cz],...]}` | Record chunks an agent has seen (max 5000 per call; non-overworld => `{"accepted":0,"ignored":n}`). |
| `GET /seen_stats?agent=` | `{"agent","chunks","first_seen","last_seen"}`. |

Detection (`settlements.py`): `terrain_y` counts roofs as ground, so a structure column is a
built surface material (planks, stairs, slabs, bricks, wool, concrete, glass, cobblestone, logs)
at least 2 blocks above a local baseline (minimum ground over about +-10 blocks, water at its
surface). Columns form buildings (trees, thin strips and scatter are dropped); buildings within
32 blocks form a cluster: 3+ buildings, or 2 plus domestic indicators (bed, bell, chest,
furnace, crafting table ...), is a `settlement`, otherwise an `outpost`. Rails and torches are
never evidence. Low confidence means a weak signal, not a confirmed small or absent settlement.
`id` hashes the cluster centre rounded to 32 blocks.

`/survey` reads the REAL block sections of the save (not live diffs): the palette and special
landmarks (bell, beacon, lectern, enchanting table, brewing stand) lag behind blocks placed since
the server last saved the chunk. Building geometry uses the store.

Gating: every terrain GET (`/perception`, `/topography`, `/area_composition`, `/shoreline`,
`/recall`, `/chunk`, `/flat_areas`, `/bridge_span`, `/settlements`, `/survey`) accepts
`agent=`. With it, chunks missing from that agent's ledger are treated as missing (counted in
`missing_chunks` and `unexplored_chunks`) and their data is never returned (`/chunk` and
`/recall` give 404). Gating is implemented once in `store_view.StoreView`. Without `agent=`
behaviour is unchanged. The ledger is the `seen` SQLite table (schema v3; v2 databases migrate
in place without losing chunk rows).

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
| `settlements.py`, `survey.py`, `store_view.py` | Settlement detection, survey digest, per-agent gated store view |

`mc_nbt.py`, `region.py`, `palette.py`, and `composition.py` are adapted from the local
`Archeology/tools` scripts. `mc_blocks.py` provides the block classification tables used by the
service.

## License

CC BY-NC 4.0 — see [LICENSE](./LICENSE).
