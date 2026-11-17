<div>
<h1 align="center">🗺️ Cartography for Mindcraft-CE</h1>

<p align="center">A terrain map for <a href="https://github.com/mindcraft-ce/mindcraft-ce">Mindcraft CE</a> bots, built from the chunks they load.</p>
<p align="center">Spatial awareness and long-range routing from what the bot has actually seen.</p>

<p align="center">
  <a href="#getting-started">Getting Started</a> |
  <a href="#results">Results</a> |
  <a href="#architecture">Architecture</a> |
  <a href="docs/REFERENCE.md">Reference</a> |
  <a href="https://github.com/milkucha/mindcraft-ce-topography">Mindcraft fork</a>
</p>
</div>

> [!Note]
> Proof of concept: tested on one world and one model so far.

# What it is

A small local sidecar service. The bot forwards the chunks it loads; the service turns them into a terrain
map the bot can query and route over. The bot knows where the water, high ground, cliffs, flat sites and
buildings are, and it plans long trips from memory of the terrain it has seen. It uses no server mod, no
world save and no vision.

<a id="getting-started"></a>
# Getting Started

## Requirements

- Python 3.10+ (standard library only)
- The Mindcraft fork with the topography hooks: [mindcraft-ce-topography](https://github.com/milkucha/mindcraft-ce-topography)
- Minecraft Java Edition. Tested on **1.20.1**; should work on 1.18 and later (untested).

> [!Warning]
> Blocks added after 1.20.1 may be misread as non-natural ground.

## Install and Run

1. Start the service:
   ```powershell
   python main.py --name my-world          # map stored in maps/my-world.db
   ```
2. In the Mindcraft profile (or `settings.js`):
   ```json
   { "topography_enabled": true, "topography_service_url": "http://127.0.0.1:8090" }
   ```
3. Join the server and play. The map fills in as the bot loads chunks.

Routing is on by default (`topography_routing: false` turns it off). The terrain layer is called
*topography* in settings and commands.

<a id="results"></a>
# Results

Six routes in one natural world (seed 6542604540806855638), 10 runs per route per agent. **Topo** is
Mindcraft with this service; **Vanilla** is plain Mindcraft CE. Both use the same model (DeepSeek flash),
given one instruction: go to these coordinates.

| Route | Length | Topo | Vanilla |
|---|---|---|---|
| Long, Control (LC): Dry Ground | 1000 | **100%** (10/10) | 60% (6/10) |
| Long, Water 1 (LW1): Lake on the Line | 1000 | **100%** (10/10) | 0% (0/10) |
| Long, Water 2 (LW2): 332-Block Bay | 1000 | **100%** (10/10) | 80% (8/10) |
| Mountain (MT): Massif, Unseen | 1000 | **100%** (10/10) | 0% (0/10) |
| Mountain, Seen (MT-seen): Massif, Seen First | 1000 | **100%** (10/10) | 0% (0/10) |
| Ravine (RV): Ravine on the Line | 200 | **100%** (10/10) | 70% (7/10) |
| **All** | | **100%** (60/60) | **35%** (21/60) |

- **Return legs:** Topo 50/50, Vanilla 10/14.
- **Deaths:** Topo 0, Vanilla 2.
- **Tokens per arrival:** where both mostly arrived (dry ground, bay, ravine), Topo used 6–10k and Vanilla 43–61k.
- **Ravine:** Topo stayed away from the edge in 10/10 runs; Vanilla walked to it in 9/10 (neither fell).

> [!Important]
> **Limits**
> - In unseen mountains, Topo goes over the top; it only routes round terrain it has seen.
> - Topo allows short swims (up to 2 water cells; 4–14 s in water on the lake and the bay).
> - On half the mountain legs, the model had to re-issue the command near the target.
> - The bay and ravine differences are not significant at 10 runs.

Each chart shows all 20 journeys on one route (Topo purple, Vanilla orange) over the terrain, with a
height profile below.

<p align="center"><a href="docs/results/LC-cold.svg"><img src="docs/results/LC-cold.svg" width="720" alt="Long, Control (LC): Dry Ground: tracks of all 20 journeys"></a></p>

<p align="center"><a href="docs/results/LW1-cold.svg"><img src="docs/results/LW1-cold.svg" width="720" alt="Long, Water 1 (LW1): Lake on the Line: tracks of all 20 journeys"></a></p>

<p align="center"><a href="docs/results/LW2-cold.svg"><img src="docs/results/LW2-cold.svg" width="720" alt="Long, Water 2 (LW2): 332-Block Bay: tracks of all 20 journeys"></a></p>

<p align="center"><a href="docs/results/MT-cold.svg"><img src="docs/results/MT-cold.svg" width="720" alt="Mountain (MT): Massif, Unseen: tracks of all 20 journeys"></a></p>

<p align="center"><a href="docs/results/MT-seen.svg"><img src="docs/results/MT-seen.svg" width="720" alt="Mountain, Seen (MT-seen): Massif, Seen First: tracks of all 20 journeys"></a></p>

<p align="center"><a href="docs/results/RV-cold.svg"><img src="docs/results/RV-cold.svg" width="720" alt="Ravine (RV): Ravine on the Line: tracks of all 20 journeys"></a></p>

# How it works

1. **Feed:** the bot sends each chunk it loads (`POST /chunk_raw`) and its seen/unloaded chunks (`POST /seen`).
2. **Map:** the service reduces each chunk to a per-column surface record and stores it.
3. **Perception:** every prompt, the bot gets a short terrain summary of its surroundings (`GET /perception`,
   at most 1200 characters).
4. **Queries:** the model can ask for more: flat build sites, shoreline, landforms, the far overview, a
   remembered chunk.
5. **Routing:** a go-to command for a far target asks `GET /route` for waypoints over seen terrain, and
   Mindcraft's own pathfinder walks them leg by leg.

<a id="architecture"></a>
# Architecture

```
Mindcraft bot ── POST /chunk_raw, /seen, /position ──▶ cartography service ──▶ SQLite (one DB per world)
      ▲                                                  │
      └──── GET /perception, queries, /route ◀───────────┘
   (optional) world save / Fabric plugin ──▶ same ingest path
```

## Ingest

`POST /chunk_raw` carries a chunk's block palettes and packed indices, gzip-compressed, at
most 16 chunks per request. `bot_chunk.py` rebuilds the same Anvil-shaped structure a save would give. It
then runs the same decoder as the save reader (`loader.py`), so a bot-fed chunk and a save-read chunk
produce identical records (tested). Decoding runs in worker processes so feeding new terrain doesn't
slow queries. A save or the optional Fabric plugin feed the same path. Only the Overworld is stored.

## Column model

Each column keeps three heights:
- `y`: the topmost non-air block.
- `ground_y`: the topmost solid block.
- `terrain_y`: the topmost *natural* ground, found by scanning down past roofs, decks, trees and water.

`terrain_y` is what lets the map tell a hill from a house, and a lake from a bridge. The list of natural
blocks lives in `terrain_rules.py`. Composition (water, trees, built blocks, ores at the surface) is stored
per chunk.

## Store

`map_store.py` is tiered:
- **hot:** the bot's current perception window.
- **warm:** an LRU of recently queried chunks.
- **cold:** SQLite.

Writes are batched and flushed about every 2 s. Every change bumps a store generation that clients cache
on. An unchanged re-send changes nothing.

## What the bot knows

Every query takes `agent=`, and chunks that agent hasn't seen are reported as
unexplored, never filled in. A chunk the bot has left is shown as it was when last loaded. It stays that way
even if the world changes, via per-chunk revisions and copy-on-write history, until the bot sees it again.
This is the map of a player, not of the server.

## Overview

Each chunk revision also writes a tiny summary row: water, ground height range, trees, built
columns, land class. `GET /overview` reads only these summaries to describe everything seen within up to
4096 blocks (water bodies, high ground, built areas by direction) at a flat cost, however much has been
seen.

## Routing

`route.py` runs A* on a 4-block grid over the agent's seen chunks.
- **Cost:** climbing costs 2 per block of ascent, and paths and bridges are cheaper. Cliffs are impassable, and at most 2 consecutive water
  cells may be crossed.
- **Search area:** the straight-line box padded by a quarter of the trip length (96–384 blocks), widened
  once if the search hits its edge.
- **Time bound:** each search stops at 1.5 s. The answer is complete, or partial with a reason
  (`unexplored`, `water`, `terrain`, `limit`).
- **Walking:** the Mindcraft side walks each leg with the stock pathfinder and replans as it goes. It
  follows shorelines when water blocks the way, resumes after Mindcraft's own unstuck mode, and hands back
  to the model when it can't continue.

## HTTP

`api.py` is a standard-library `ThreadingHTTPServer`. It binds to loopback by default; off
loopback, a bearer token (`--token`) is mandatory. The full interface is in [docs/CONTRACT.md](docs/CONTRACT.md); options, endpoints and internals are in [docs/REFERENCE.md](docs/REFERENCE.md).

# Tests

```powershell
python -m unittest discover -s tests
```

Builds a synthetic world, so no Minecraft is needed.

# License

MIT (see [LICENSE](LICENSE)).
