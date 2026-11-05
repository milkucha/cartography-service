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

`mc_nbt.py`, `region.py`, `palette.py`, and `composition.py` are adapted from the local
`Archeology/tools` scripts. `mc_blocks.py` provides the block classification tables used by the
service.

## License

CC BY-NC 4.0 — see [LICENSE](./LICENSE).
